"""Cross-OS low-latency desktop streaming adapter compatible with vercel-labs/screen-relay (`nsl1`).

Architecture & Security Invariants:
- Loopback-only engine binding: if the native C++ ``relay-engine`` binary
  (from ``vercel-labs/screen-relay``) is installed, it is ALWAYS bound to
  ``127.0.0.1:<ephemeral_port>`` and NEVER exposed on LAN/WAN.
- Outbound-only fleet model: remote agents bridge ``nsl1`` binary frames
  outbound over authenticated WSS (``/api/v1/ws/sessions/{session_id}``)
  using ``X-Device-Token`` headers; no inbound ports are opened on endpoints.
- Policy authority stays in AI Bridge: sessions start strictly View-Only
  (``/grants/watch``) and only accept ``nsl1`` binary input packets when the
  Control Plane session mode, ``desktop_enabled`` policy, emergency stop, and
  human-takeover checks all allow control.
- Zero-dependency local fallback: when ``relay-engine`` is not installed on a
  host, ``LocalNsl1Streamer`` encodes desktop frames into Annex-B H.264 access
  units via PyAV (``libx264`` zerolatency) wrapped in exact 20-byte ``NSL1``
  frames so browser WebCodecs ``VideoDecoder`` works end-to-end across
  Windows, macOS, and Linux.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import os
import platform
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("ai_bridge.screen_relay")

# ---------------------------------------------------------------------------
# nsl1 Binary Wire Protocol (vercel-labs/screen-relay)
# ---------------------------------------------------------------------------

NSL1_MAGIC = b"NSL1"
NSL1_VERSION = 1
NSL1_HEADER_STRUCT = struct.Struct(">4sBBHIII")  # 20 bytes
NSL1_HEADER_SIZE = NSL1_HEADER_STRUCT.size  # 20
NSL1_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024  # 8 MiB cap per upstream spec

# Message types (byte offset 5)
NSL1_MSG_VIDEO = 1
NSL1_MSG_CURSOR = 2
NSL1_MSG_ACK = 3
NSL1_MSG_INPUT = 4
NSL1_MSG_CONTROL = 5

# Video frame flags (uint16 offset 6)
NSL1_FLAG_KEYFRAME = 0x0001
NSL1_FLAG_CONFIG_INCLUDED = 0x0002

# Video payload sub-header prepended before Annex-B H.264 bytes when streaming
# through AI Bridge's WebCodecs bridge: width(u16), height(u16), timestamp_us(u64) = 12 bytes
NSL1_VIDEO_META_STRUCT = struct.Struct(">HHQ")
NSL1_VIDEO_META_SIZE = NSL1_VIDEO_META_STRUCT.size  # 12

# 16-byte Big-Endian Binary Input Record (POST /input & NSL1_MSG_INPUT payload)
# offset 0: type (u8)
# offset 1: reserved (u8 = 0)
# offset 2: code (u16: button id, HID/VK keycode, or wheel axis)
# offset 4: value (i32: signed wheel delta, unicode scalar, or state 1=down/0=up)
# offset 8: x_norm (u16: 0..65535 normalized desktop X)
# offset 10: y_norm (u16: 0..65535 normalized desktop Y)
# offset 12: seq (u32: caller sequence / request id)
NSL1_INPUT_RECORD_STRUCT = struct.Struct(">BBHiHHI")  # 16 bytes
NSL1_INPUT_RECORD_SIZE = NSL1_INPUT_RECORD_STRUCT.size  # 16
NSL1_MAX_INPUT_BATCH = 256

INPUT_TYPE_POINTER_MOVE = 1
INPUT_TYPE_POINTER_BUTTON = 2
INPUT_TYPE_WHEEL = 3
INPUT_TYPE_KEY_DOWN = 4
INPUT_TYPE_KEY_UP = 5
INPUT_TYPE_UNICODE = 6

BUTTON_CODE_LEFT = 1
BUTTON_CODE_RIGHT = 2
BUTTON_CODE_MIDDLE = 3

# WebSocket close codes matching upstream exclusive ownership semantics
NSL1_CLOSE_TAKEN = 4001
NSL1_CLOSE_BUSY = 4003


@dataclass(frozen=True)
class Nsl1Frame:
    """Decoded 20-byte NSL1 header + payload bytes."""

    version: int
    msg_type: int
    flags: int
    generation: int
    seq: int
    payload: bytes

    @property
    def is_keyframe(self) -> bool:
        return bool(self.flags & NSL1_FLAG_KEYFRAME)


def encode_nsl1_frame(
    msg_type: int,
    payload: bytes = b"",
    *,
    flags: int = 0,
    generation: int = 1,
    seq: int = 0,
) -> bytes:
    """Pack a 20-byte NSL1 binary header + payload."""
    if len(payload) > NSL1_MAX_PAYLOAD_BYTES:
        raise ValueError(f"NSL1 payload exceeds 8 MiB limit ({len(payload)} bytes)")
    header = NSL1_HEADER_STRUCT.pack(
        NSL1_MAGIC,
        NSL1_VERSION,
        int(msg_type) & 0xFF,
        int(flags) & 0xFFFF,
        int(generation) & 0xFFFFFFFF,
        int(seq) & 0xFFFFFFFF,
        len(payload),
    )
    return header + payload


def decode_nsl1_frame(data: bytes) -> Nsl1Frame:
    """Validate and unpack an NSL1 binary WebSocket message."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("NSL1 frame must be bytes")
    raw = bytes(data)
    if len(raw) < NSL1_HEADER_SIZE:
        raise ValueError(f"NSL1 frame too short ({len(raw)} < {NSL1_HEADER_SIZE} bytes)")
    magic, version, msg_type, flags, generation, seq, payload_len = NSL1_HEADER_STRUCT.unpack_from(raw, 0)
    if magic != NSL1_MAGIC:
        raise ValueError(f"Invalid NSL1 magic: {magic!r}")
    if version != NSL1_VERSION:
        raise ValueError(f"Unsupported NSL1 version: {version}")
    if payload_len > NSL1_MAX_PAYLOAD_BYTES:
        raise ValueError(f"NSL1 payload length exceeds 8 MiB limit: {payload_len}")
    if len(raw) - NSL1_HEADER_SIZE != payload_len:
        raise ValueError(
            f"NSL1 payload length mismatch (header={payload_len}, actual={len(raw) - NSL1_HEADER_SIZE})"
        )
    return Nsl1Frame(
        version=version,
        msg_type=msg_type,
        flags=flags,
        generation=generation,
        seq=seq,
        payload=raw[NSL1_HEADER_SIZE:],
    )


def encode_nsl1_video_frame(
    annexb_bytes: bytes,
    *,
    width: int,
    height: int,
    timestamp_us: int,
    is_keyframe: bool,
    generation: int = 1,
    seq: int = 0,
) -> bytes:
    """Pack an H.264 Annex-B access unit with 12-byte geometry/timestamp header inside NSL1."""
    meta = NSL1_VIDEO_META_STRUCT.pack(
        max(0, min(65535, int(width))),
        max(0, min(65535, int(height))),
        max(0, int(timestamp_us)),
    )
    flags = (NSL1_FLAG_KEYFRAME | NSL1_FLAG_CONFIG_INCLUDED) if is_keyframe else 0
    return encode_nsl1_frame(
        NSL1_MSG_VIDEO,
        meta + annexb_bytes,
        flags=flags,
        generation=generation,
        seq=seq,
    )


def decode_nsl1_video_payload(payload: bytes) -> tuple[int, int, int, bytes]:
    """Unpack ``(width, height, timestamp_us, annexb_bytes)`` from an NSL1 video payload."""
    if len(payload) < NSL1_VIDEO_META_SIZE:
        raise ValueError("NSL1 video payload shorter than metadata header")
    width, height, ts_us = NSL1_VIDEO_META_STRUCT.unpack_from(payload, 0)
    return width, height, ts_us, payload[NSL1_VIDEO_META_SIZE:]


def _norm_to_u16(v: float) -> int:
    clamped = max(0.0, min(1.0, float(v)))
    return int(round(clamped * 65535.0))


def _u16_to_norm(v: int) -> float:
    return max(0.0, min(1.0, float(v) / 65535.0))


def encode_input_record(
    event_type: int,
    *,
    code: int = 0,
    value: int = 0,
    x_norm: float = 0.0,
    y_norm: float = 0.0,
    seq: int = 0,
) -> bytes:
    """Encode a single 16-byte big-endian screen-relay binary input record."""
    return NSL1_INPUT_RECORD_STRUCT.pack(
        int(event_type) & 0xFF,
        0,  # reserved
        int(code) & 0xFFFF,
        max(-2147483648, min(2147483647, int(value))),
        _norm_to_u16(x_norm),
        _norm_to_u16(y_norm),
        int(seq) & 0xFFFFFFFF,
    )


def encode_input_batch(records: list[bytes]) -> bytes:
    """Concatenate 1..256 16-byte binary input records for HTTP POST /input or NSL1_MSG_INPUT."""
    if not records:
        raise ValueError("Input batch must contain at least one 16-byte record")
    if len(records) > NSL1_MAX_INPUT_BATCH:
        raise ValueError(f"Input batch exceeds {NSL1_MAX_INPUT_BATCH} records")
    for rec in records:
        if len(rec) != NSL1_INPUT_RECORD_SIZE:
            raise ValueError(f"Each input record must be {NSL1_INPUT_RECORD_SIZE} bytes (got {len(rec)})")
    return b"".join(records)


def decode_input_records(data: bytes) -> list[dict[str, Any]]:
    """Decode 1..256 consecutive 16-byte binary input records into structured dicts."""
    if not data or len(data) % NSL1_INPUT_RECORD_SIZE != 0:
        raise ValueError(
            f"Binary input payload must be a non-empty multiple of {NSL1_INPUT_RECORD_SIZE} bytes"
        )
    count = len(data) // NSL1_INPUT_RECORD_SIZE
    if count > NSL1_MAX_INPUT_BATCH:
        raise ValueError(f"Binary input batch exceeds {NSL1_MAX_INPUT_BATCH} records")
    out: list[dict[str, Any]] = []
    for i in range(count):
        etype, _res, code, val, x_u16, y_u16, seq = NSL1_INPUT_RECORD_STRUCT.unpack_from(
            data, i * NSL1_INPUT_RECORD_SIZE
        )
        out.append(
            {
                "type": etype,
                "code": code,
                "value": val,
                "x_norm": _u16_to_norm(x_u16),
                "y_norm": _u16_to_norm(y_u16),
                "seq": seq,
            }
        )
    return out


def json_input_to_nsl1_records(event: dict[str, Any], *, seq: int = 0) -> bytes:
    """Convert a Dashboard JSON input event into 16-byte screen-relay binary records."""
    action = str(event.get("action") or event.get("type") or "").strip().lower()
    nx = float(event.get("x", 0.0))
    ny = float(event.get("y", 0.0))
    btn_str = str(event.get("button") or "left").strip().lower()
    btn_code = (
        BUTTON_CODE_RIGHT
        if (action == "right_click" or btn_str == "right")
        else (BUTTON_CODE_MIDDLE if (action == "middle_click" or btn_str == "middle") else BUTTON_CODE_LEFT)
    )
    records: list[bytes] = []
    if action in ("move", "mouse_move", "mousemove"):
        records.append(encode_input_record(INPUT_TYPE_POINTER_MOVE, x_norm=nx, y_norm=ny, seq=seq))
    elif action in ("click", "right_click", "middle_click", "double_click"):
        clicks = 2 if action == "double_click" else max(1, int(event.get("clicks", 1)))
        records.append(encode_input_record(INPUT_TYPE_POINTER_MOVE, x_norm=nx, y_norm=ny, seq=seq))
        for _ in range(clicks):
            records.append(
                encode_input_record(INPUT_TYPE_POINTER_BUTTON, code=btn_code, value=1, x_norm=nx, y_norm=ny, seq=seq)
            )
            records.append(
                encode_input_record(INPUT_TYPE_POINTER_BUTTON, code=btn_code, value=0, x_norm=nx, y_norm=ny, seq=seq)
            )
    elif action == "mousedown":
        records.append(
            encode_input_record(INPUT_TYPE_POINTER_BUTTON, code=btn_code, value=1, x_norm=nx, y_norm=ny, seq=seq)
        )
    elif action == "mouseup":
        records.append(
            encode_input_record(INPUT_TYPE_POINTER_BUTTON, code=btn_code, value=0, x_norm=nx, y_norm=ny, seq=seq)
        )
    elif action in ("scroll", "wheel", "mouse_scroll"):
        delta = int(event.get("delta", -120))
        records.append(encode_input_record(INPUT_TYPE_WHEEL, code=0, value=delta, x_norm=nx, y_norm=ny, seq=seq))
    elif action in ("type", "type_text"):
        text = str(event.get("text") or "")[:NSL1_MAX_INPUT_BATCH]
        for ch in text:
            records.append(encode_input_record(INPUT_TYPE_UNICODE, value=ord(ch), seq=seq))
    elif action in ("key_down", "keydown"):
        code = int(event.get("code") or 0)
        records.append(encode_input_record(INPUT_TYPE_KEY_DOWN, code=code, value=1, seq=seq))
    elif action in ("key_up", "keyup"):
        code = int(event.get("code") or 0)
        records.append(encode_input_record(INPUT_TYPE_KEY_UP, code=code, value=0, seq=seq))
    else:
        raise ValueError(f"Unsupported screen-relay input action: {action!r}")
    return encode_input_batch(records)


# ---------------------------------------------------------------------------
# Binary Discovery & Platform Honesty
# ---------------------------------------------------------------------------


def _platform_capture_backend() -> str:
    sys_os = platform.system()
    if sys_os == "Windows":
        return "dxgi_desktop_duplication"
    if sys_os == "Darwin":
        return "screencapturekit"
    if sys_os == "Linux":
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland_wlr_screencopy"
        return "x11_xdamage_shm"
    return "unknown"


def find_screen_relay_binary() -> str | None:
    """Locate the native ``relay-engine`` (or ``screen-relay``) executable if installed."""
    override = os.environ.get("AI_BRIDGE_SCREEN_RELAY_BIN", "").strip()
    if override and Path(override).is_file():
        return str(Path(override).resolve())

    exe_names = (
        ["relay-engine.exe", "screen-relay.exe"]
        if sys.platform == "win32"
        else ["relay-engine", "screen-relay"]
    )
    search_dirs = [
        Path.home() / ".ai-bridge" / "bin",
        Path(__file__).resolve().parents[2] / "bin",
    ]
    for d in search_dirs:
        for name in exe_names:
            cand = d / name
            if cand.is_file():
                return str(cand)
    for name in exe_names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _pyav_h264_available() -> bool:
    try:
        import av  # noqa: F401

        return True
    except ImportError:
        return False


def screen_relay_available() -> bool:
    """True when either native ``relay-engine`` or the PyAV H.264 NSL1 fallback is ready."""
    return bool(find_screen_relay_binary() or _pyav_h264_available())


def describe_screen_relay_status() -> dict[str, Any]:
    """Honest host status for ``screen-relay`` (WebCodecs H.264 over ``nsl1``)."""
    bin_path = find_screen_relay_binary()
    pyav_ok = _pyav_h264_available()
    mode = "relay_engine" if bin_path else ("pyav_h264_nsl1" if pyav_ok else "unavailable")
    return {
        "available": bool(bin_path or pyav_ok),
        "mode": mode,
        "protocol": "nsl1",
        "codec": "H264",
        "webcodecs": True,
        "native_binary_installed": bool(bin_path),
        "native_binary_path": bin_path or "",
        "pyav_fallback_available": pyav_ok,
        "platform": platform.system(),
        "capture_backend": _platform_capture_backend(),
        "loopback_only": True,
        "max_fps": 120 if bin_path else 60,
    }


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Loopback-Only Native `relay-engine` Process Manager
# ---------------------------------------------------------------------------


class ScreenRelayEngine:
    """Manages a local ``relay-engine`` subprocess strictly bound to ``127.0.0.1``."""

    def __init__(
        self,
        *,
        binary_path: str | None = None,
        port: int | None = None,
        fps: int = 60,
        bitrate_kbps: int = 4500,
    ) -> None:
        self.binary_path = binary_path or find_screen_relay_binary()
        self.host = "127.0.0.1"  # Invariant: NEVER bind 0.0.0.0
        self.port = int(port) if port else _pick_free_loopback_port()
        self.fps = max(1, min(120, int(fps)))
        self.bitrate_kbps = max(300, min(25000, int(bitrate_kbps)))
        self._admin_token = secrets.token_urlsafe(24)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_stream_url(self) -> str:
        return f"ws://{self.host}:{self.port}/stream"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, timeout_seconds: float = 5.0) -> bool:
        if not self.binary_path:
            return False
        with self._lock:
            if self.running:
                return True
            cmd = [
                self.binary_path,
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--fps",
                str(self.fps),
            ]
            env = {
                **os.environ,
                "SCREEN_RELAY_HOST": self.host,
                "SCREEN_RELAY_PORT": str(self.port),
                "SCREEN_RELAY_ADMIN_TOKEN": self._admin_token,
            }
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            if not self.running:
                return False
            try:
                r = httpx.get(f"{self.base_url}/capabilities", timeout=0.5)
                if r.status_code == 200:
                    return True
            except Exception:
                time.sleep(0.1)
        return self.running

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._admin_token}"}

    def get_capabilities(self) -> dict[str, Any]:
        resp = httpx.get(f"{self.base_url}/capabilities", headers=self._auth_headers(), timeout=3.0)
        resp.raise_for_status()
        return resp.json()

    def get_screenshot_png(self) -> bytes:
        resp = httpx.get(f"{self.base_url}/screenshot.png", headers=self._auth_headers(), timeout=5.0)
        resp.raise_for_status()
        return resp.content

    def mint_watch_grant(self, *, connections: int = 1) -> dict[str, Any]:
        """Mint a strictly View-Only watch grant on the loopback engine."""
        resp = httpx.post(
            f"{self.base_url}/grants/watch",
            headers=self._auth_headers(),
            json={"connections": max(1, int(connections))},
            timeout=3.0,
        )
        resp.raise_for_status()
        return resp.json()

    def mint_control_grant(self, *, connections: int = 1) -> dict[str, Any]:
        """Mint a single-connection control grant (only called after AI Bridge policy check)."""
        resp = httpx.post(
            f"{self.base_url}/grants/control",
            headers=self._auth_headers(),
            json={"connections": max(1, int(connections))},
            timeout=3.0,
        )
        resp.raise_for_status()
        return resp.json()

    def post_input_records(self, raw_records: bytes) -> bool:
        """Forward 1..256 16-byte binary input records to the loopback engine."""
        if not raw_records or len(raw_records) % NSL1_INPUT_RECORD_SIZE != 0:
            raise ValueError("Invalid binary input batch")
        resp = httpx.post(
            f"{self.base_url}/input",
            headers={**self._auth_headers(), "Content-Type": "application/octet-stream"},
            content=raw_records,
            timeout=2.0,
        )
        return resp.status_code in (200, 202, 204)


# ---------------------------------------------------------------------------
# Active Relay Session Registry (Local + Outbound Agent Bridge)
# ---------------------------------------------------------------------------

_RELAY_SESSIONS: dict[str, "LocalNsl1Streamer"] = {}
_RELAY_LOCK = threading.Lock()


def _execute_local_nsl1_input(
    records: list[dict[str, Any]],
    *,
    screen_width: int = 1920,
    screen_height: int = 1080,
) -> int:
    """Execute decoded 16-byte NSL1 input records locally through AI Bridge's input layer."""
    from .tools.desktop import perform_approved_ui_action

    executed = 0
    for rec in records:
        etype = rec["type"]
        px = int(round(rec["x_norm"] * max(1, screen_width)))
        py = int(round(rec["y_norm"] * max(1, screen_height)))
        if etype == INPUT_TYPE_POINTER_MOVE:
            perform_approved_ui_action("move_mouse", x=px, y=py, approval_granted=True)
            executed += 1
        elif etype == INPUT_TYPE_POINTER_BUTTON:
            # Click on button-down -> button-up pair, or direct click for single down
            if rec["value"] == 1:
                act = (
                    "right_click"
                    if rec["code"] == BUTTON_CODE_RIGHT
                    else ("middle_click" if rec["code"] == BUTTON_CODE_MIDDLE else "click")
                )
                perform_approved_ui_action(act, x=px, y=py, approval_granted=True)
                executed += 1
        elif etype == INPUT_TYPE_WHEEL:
            perform_approved_ui_action("scroll", x=px, y=py, delta=int(rec["value"]), approval_granted=True)
            executed += 1
        elif etype == INPUT_TYPE_UNICODE:
            cp = int(rec["value"])
            if 0 < cp <= 0x10FFFF:
                perform_approved_ui_action("type_text", text=chr(cp), approval_granted=True)
                executed += 1
        elif etype == INPUT_TYPE_KEY_DOWN:
            # Map basic ASCII keycodes when present
            code = int(rec["code"])
            if 32 <= code <= 126:
                perform_approved_ui_action("press_key", key=chr(code).lower(), approval_granted=True)
                executed += 1
    return executed


class LocalNsl1Streamer:
    """Streams live desktop Annex-B H.264 frames wrapped in 20-byte NSL1 packets.

    Supports both:
    - In-process delivery to a Control Plane async callback (``device_id="local"``)
    - Outbound WebSocket delivery to ``WS /api/v1/ws/sessions/{session_id}`` (remote agents)
    """

    def __init__(
        self,
        session_id: str,
        *,
        fps: int = 30,
        scale: float = 0.85,
        bitrate_kbps: int = 3500,
        monitor_index: int = 0,
        on_binary_frame: Callable[[bytes], Any] | None = None,
        control_url: str | None = None,
        device_token: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.fps = max(1, min(60, int(fps)))
        self.scale = max(0.25, min(1.0, float(scale)))
        self.bitrate_kbps = max(300, min(20000, int(bitrate_kbps)))
        self.monitor_index = max(0, int(monitor_index))
        self._on_binary_frame = on_binary_frame
        self._control_url = (control_url or "").strip().rstrip("/")
        self._device_token = (device_token or "").strip()
        self._stop = threading.Event()
        self._force_keyframe = threading.Event()
        self._force_keyframe.set()
        self._thread: threading.Thread | None = None
        self._generation = 1
        self._seq = 0
        self._width = 1280
        self._height = 720
        self._frames_sent = 0
        self._last_ack_seq = 0
        self._backend = "pyav_h264_nsl1"
        self._engine: ScreenRelayEngine | None = None

    def switch_monitor(self, monitor_index: int) -> int:
        self.monitor_index = max(0, int(monitor_index))
        self._force_keyframe.set()
        return self.monitor_index

    def request_keyframe(self) -> None:
        self._force_keyframe.set()

    def note_ack(self, seq: int) -> None:
        self._last_ack_seq = max(self._last_ack_seq, int(seq))

    def handle_binary_input(self, raw_records: bytes) -> int:
        if self._engine is not None and self._engine.running:
            self._engine.post_input_records(raw_records)
            return len(raw_records) // NSL1_INPUT_RECORD_SIZE
        recs = decode_input_records(raw_records)
        return _execute_local_nsl1_input(recs, screen_width=self._width, screen_height=self._height)

    def telemetry(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "transport": "screen_relay",
            "protocol": "nsl1",
            "backend": self._backend,
            "width": self._width,
            "height": self._height,
            "fps_target": self.fps,
            "bitrate_kbps_target": self.bitrate_kbps,
            "monitor_index": self.monitor_index,
            "frames_sent": self._frames_sent,
            "last_ack_seq": self._last_ack_seq,
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_entry,
            name=f"screen-relay-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._engine is not None:
            self._engine.stop()

    def _run_entry(self) -> None:
        try:
            if self._control_url and self._device_token:
                asyncio.run(self._run_outbound_ws_loop())
            else:
                self._run_local_encode_loop(emit_fn=self._on_binary_frame)
        except Exception as exc:
            logger.warning("screen_relay session %s ended with error: %s", self.session_id, exc)
        finally:
            if self._engine is not None:
                self._engine.stop()
            with _RELAY_LOCK:
                if _RELAY_SESSIONS.get(self.session_id) is self:
                    _RELAY_SESSIONS.pop(self.session_id, None)

    def _encode_frame_loop_iter(self, codec_ctx_holder: dict[str, Any]) -> bytes | None:
        import fractions

        import av

        from .webrtc_stream import grab_screen

        img = grab_screen(self.scale, self.monitor_index)
        w = max(64, int(img.width) & ~1)
        h = max(64, int(img.height) & ~1)
        if w != img.width or h != img.height:
            img = img.resize((w, h))
        self._width = w
        self._height = h

        ctx = codec_ctx_holder.get("ctx")
        if ctx is None or codec_ctx_holder.get("size") != (w, h):
            ctx = av.CodecContext.create("libx264", "w")
            ctx.width = w
            ctx.height = h
            ctx.pix_fmt = "yuv420p"
            ctx.time_base = fractions.Fraction(1, self.fps)
            ctx.bit_rate = self.bitrate_kbps * 1000
            ctx.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "profile": "baseline",
                "repeat-headers": "1",
                "keyint": str(max(15, self.fps * 2)),
            }
            ctx.open()
            codec_ctx_holder["ctx"] = ctx
            codec_ctx_holder["size"] = (w, h)
            self._force_keyframe.set()

        vframe = av.VideoFrame.from_image(img).reformat(width=w, height=h, format="yuv420p")
        if self._force_keyframe.is_set():
            vframe.pict_type = getattr(getattr(av.video.frame, "PictureType", None), "I", 1)
            self._force_keyframe.clear()
        vframe.pts = self._seq
        packets = ctx.encode(vframe)
        if not packets:
            return None
        annexb = b"".join(bytes(p) for p in packets)
        is_key = any(bool(getattr(p, "is_keyframe", False)) for p in packets)
        self._seq += 1
        self._frames_sent += 1
        return encode_nsl1_video_frame(
            annexb,
            width=w,
            height=h,
            timestamp_us=int(time.monotonic() * 1_000_000),
            is_keyframe=is_key,
            generation=self._generation,
            seq=self._seq,
        )

    def _run_local_encode_loop(self, *, emit_fn: Callable[[bytes], Any] | None) -> None:
        codec_holder: dict[str, Any] = {}
        interval = 1.0 / float(self.fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            # Simple ACK backpressure: pause if >15 unacknowledged frames queued
            if self._seq - self._last_ack_seq > 15 and self._last_ack_seq > 0:
                time.sleep(0.02)
                continue
            try:
                packet = self._encode_frame_loop_iter(codec_holder)
                if packet and emit_fn is not None:
                    emit_fn(packet)
            except Exception as exc:
                logger.debug("screen_relay encode tick failed: %s", exc)
                time.sleep(0.25)
                continue
            elapsed = time.monotonic() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    async def _run_outbound_ws_loop(self) -> None:
        """Connect outbound to Control Plane WS and stream NSL1 binary video frames."""
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Outbound screen-relay tunnel requires the 'websockets' package.") from exc

        ws_base = self._control_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/api/v1/ws/sessions/{self.session_id}"
        headers = {"X-Device-Token": self._device_token}
        codec_holder: dict[str, Any] = {}
        interval = 1.0 / float(self.fps)

        async with websockets.connect(url, additional_headers=headers, max_size=NSL1_MAX_PAYLOAD_BYTES + 64) as ws:
            async def _recv_loop() -> None:
                while not self._stop.is_set():
                    try:
                        raw = await ws.recv()
                    except Exception:
                        self._stop.set()
                        return
                    if isinstance(raw, (bytes, bytearray)):
                        try:
                            frame = decode_nsl1_frame(bytes(raw))
                        except ValueError:
                            continue
                        if frame.msg_type == NSL1_MSG_ACK:
                            self.note_ack(frame.seq)
                        elif frame.msg_type == NSL1_MSG_INPUT:
                            await asyncio.to_thread(self.handle_binary_input, frame.payload)
                        elif frame.msg_type == NSL1_MSG_CONTROL:
                            self.request_keyframe()
                    elif isinstance(raw, str):
                        try:
                            import json

                            msg = json.loads(raw)
                            mtype = str(msg.get("type") or "").lower()
                            if mtype == "session_terminated":
                                self._stop.set()
                                return
                            if mtype in ("switch_monitor", "set_monitor") and "monitor_index" in msg:
                                self.switch_monitor(int(msg["monitor_index"]))
                            elif mtype == "request_keyframe":
                                self.request_keyframe()
                        except Exception:
                            pass

            recv_task = asyncio.create_task(_recv_loop())
            try:
                while not self._stop.is_set():
                    t0 = time.monotonic()
                    try:
                        packet = await asyncio.to_thread(self._encode_frame_loop_iter, codec_holder)
                    except Exception:
                        await asyncio.sleep(0.25)
                        continue
                    if packet:
                        await ws.send(packet)
                    elapsed = time.monotonic() - t0
                    if elapsed < interval:
                        await asyncio.sleep(interval - elapsed)
            finally:
                recv_task.cancel()


def start_relay_session(
    session_id: str,
    *,
    fps: int = 30,
    scale: float = 0.85,
    bitrate_kbps: int = 3500,
    monitor_index: int = 0,
    on_binary_frame: Callable[[bytes], Any] | None = None,
    control_url: str | None = None,
    device_token: str | None = None,
) -> dict[str, Any]:
    """Start a managed NSL1 screen-relay stream for ``session_id``."""
    with _RELAY_LOCK:
        old = _RELAY_SESSIONS.pop(session_id, None)
    if old is not None:
        old.stop()

    streamer = LocalNsl1Streamer(
        session_id,
        fps=fps,
        scale=scale,
        bitrate_kbps=bitrate_kbps,
        monitor_index=monitor_index,
        on_binary_frame=on_binary_frame,
        control_url=control_url,
        device_token=device_token,
    )
    with _RELAY_LOCK:
        _RELAY_SESSIONS[session_id] = streamer
    streamer.start()
    return {
        "session_id": session_id,
        "status": "started",
        "transport": "screen_relay",
        "protocol": "nsl1",
        "codec": "H264",
        "fps": streamer.fps,
        "bitrate_kbps": streamer.bitrate_kbps,
        "monitor_index": streamer.monitor_index,
    }


def get_relay_session(session_id: str) -> LocalNsl1Streamer | None:
    with _RELAY_LOCK:
        return _RELAY_SESSIONS.get(session_id)


def stop_relay_session(session_id: str) -> dict[str, Any]:
    with _RELAY_LOCK:
        streamer = _RELAY_SESSIONS.pop(session_id, None)
    if streamer is not None:
        streamer.stop()
        return {"session_id": session_id, "status": "stopped"}
    return {"session_id": session_id, "status": "not_running"}


def _cleanup_all_relays() -> None:
    with _RELAY_LOCK:
        items = list(_RELAY_SESSIONS.values())
        _RELAY_SESSIONS.clear()
    for s in items:
        try:
            s.stop()
        except Exception:
            pass


atexit.register(_cleanup_all_relays)

