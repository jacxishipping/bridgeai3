"""Cross-OS low-latency desktop streaming adapter compatible with vercel-labs/screen-relay (`nsl1`).

Architecture & Security Invariants:
- Loopback-only engine binding: when the native C++ ``relay-engine`` binary
  (from ``vercel-labs/screen-relay``) is installed, it is ALWAYS bound to
  ``127.0.0.1:<ephemeral_port>`` with a CSPRNG 64-hex ``--token-file`` (0600)
  and NEVER exposed on LAN/WAN.
- Outbound-only fleet model: remote agents bridge ``nsl1`` binary frames
  outbound over authenticated WSS (``/api/v1/ws/sessions/{session_id}``)
  using ``X-Device-Token`` headers, TLS verification, and resilient reconnect.
- Policy authority stays in AI Bridge: sessions start strictly View-Only
  and only accept ``nsl1`` binary input packets when the Control Plane session
  mode, ``desktop_enabled`` policy, emergency stop, and human-takeover checks
  all allow control.
- Warm local fallback: when ``relay-engine`` is absent or headless on a host,
  ``LocalNsl1Streamer`` encodes desktop frames into Annex-B H.264 access units
  via PyAV (``libx264`` zerolatency) wrapped in byte-exact 20-byte ``NSL1``
  packets so browser WebCodecs ``VideoDecoder`` works across Windows, macOS,
  and Linux without interruption.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("ai_bridge.screen_relay")

# ---------------------------------------------------------------------------
# Byte-Exact nsl1 Binary Wire Protocol (vercel-labs/screen-relay PROTOCOL.md)
# ---------------------------------------------------------------------------

NSL1_MAGIC = b"NSL1"
NSL1_VERSION = 1
NSL1_SUBPROTOCOL = "nsl1"
NSL1_HEADER_STRUCT = struct.Struct(">4sBBHIII")  # 20 bytes: magic(4), ver(1), type(1), flags(2), gen(4), seq(4), len(4)
NSL1_HEADER_SIZE = NSL1_HEADER_STRUCT.size  # 20
NSL1_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 + 12  # 8 MiB access unit + 12B video header cap per upstream spec

# Upstream NSL1 message types (offset 5)
NSL1_MSG_VIDEO = 1           # Server -> /stream: width(u16), height(u16), ts_us(u64), H.264 Annex-B
NSL1_MSG_ACK = 2             # Client -> /stream: optional wait_us(u32), echoes generation + seq
NSL1_MSG_INPUT = 3           # Client -> /stream or /control: 16-byte input record(s)
NSL1_MSG_CONTROL_READY = 4   # Server -> /control: width(u16), height(u16)
NSL1_MSG_INPUT_RESPONSE = 5  # Server -> /control: status(u8); also used by AI Bridge fallback as keyframe control hint
NSL1_MSG_CONTROL = 5         # Backwards-compatible alias for control/keyframe request frames
NSL1_MSG_CURSOR = 6          # Server -> /stream: width(u16), height(u16), hot_x(u16), hot_y(u16), RGBA bytes
NSL1_MSG_CLIPBOARD = 7       # Server -> /stream?clipboard=1: empty payload clipboard-changed notice

# Video frame flags (uint16 offset 6) — upstream parseHeader strictly requires flags <= 1 for video and 0 otherwise
NSL1_FLAG_KEYFRAME = 0x0001

# Video payload sub-header: width(u16), height(u16), timestamp_us(u64) = 12 bytes
NSL1_VIDEO_META_STRUCT = struct.Struct(">HHQ")
NSL1_VIDEO_META_SIZE = NSL1_VIDEO_META_STRUCT.size  # 12

# Cursor payload sub-header: width(u16), height(u16), hotspot_x(u16), hotspot_y(u16) = 8 bytes
NSL1_CURSOR_META_STRUCT = struct.Struct(">HHHH")
NSL1_CURSOR_META_SIZE = NSL1_CURSOR_META_STRUCT.size  # 8

# ACK optional viewer wait payload: wait_us(u32) = 4 bytes
NSL1_ACK_WAIT_STRUCT = struct.Struct(">I")

# 16-byte Big-Endian Binary Input Record (vercel-labs/screen-relay PROTOCOL.md)
# offset 0:  type (u8: 1..8)
# offset 1:  reserved (u8 = 0)
# offset 2:  code (u16: HID usage 0..231, button 1..3, or wheel axis 0=vert/1=horiz)
# offset 4:  value (i32: signed wheel delta multiple of 120 in -1200..1200, or Unicode scalar)
# offset 8:  x (u16: 0..65535 normalized desktop X)
# offset 10: y (u16: 0..65535 normalized desktop Y)
# offset 12: buttons (u16: low 5 bits only; must be 0 for releaseAll & text)
# offset 14: modifiers (u16: low 8 bits only; must be 0 for releaseAll & text)
NSL1_INPUT_RECORD_STRUCT = struct.Struct(">BBHiHHHH")  # 16 bytes
NSL1_INPUT_RECORD_SIZE = NSL1_INPUT_RECORD_STRUCT.size  # 16
NSL1_MAX_INPUT_BATCH = 256

# Exact upstream input event types
INPUT_TYPE_KEY_DOWN = 1
INPUT_TYPE_KEY_UP = 2
INPUT_TYPE_POINTER_MOVE = 3
INPUT_TYPE_BUTTON_DOWN = 4
INPUT_TYPE_BUTTON_UP = 5
INPUT_TYPE_WHEEL = 6
INPUT_TYPE_RELEASE_ALL = 7
INPUT_TYPE_TEXT = 8

# Backwards-compatible aliases
INPUT_TYPE_POINTER_BUTTON = INPUT_TYPE_BUTTON_DOWN
INPUT_TYPE_UNICODE = INPUT_TYPE_TEXT

# Exact upstream mouse button codes (1=left, 2=middle, 3=right)
BUTTON_CODE_LEFT = 1
BUTTON_CODE_MIDDLE = 2
BUTTON_CODE_RIGHT = 3

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
    gen = max(1, int(generation) & 0xFFFFFFFF)
    clean_flags = (int(flags) & NSL1_FLAG_KEYFRAME) if int(msg_type) == NSL1_MSG_VIDEO else 0
    header = NSL1_HEADER_STRUCT.pack(
        NSL1_MAGIC,
        NSL1_VERSION,
        int(msg_type) & 0xFF,
        clean_flags,
        gen,
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
    seq: int = 1,
) -> bytes:
    """Pack an H.264 Annex-B access unit with 12-byte geometry/timestamp header inside NSL1."""
    meta = NSL1_VIDEO_META_STRUCT.pack(
        max(1, min(65535, int(width))),
        max(1, min(65535, int(height))),
        max(0, int(timestamp_us)),
    )
    flags = NSL1_FLAG_KEYFRAME if is_keyframe else 0
    return encode_nsl1_frame(
        NSL1_MSG_VIDEO,
        meta + annexb_bytes,
        flags=flags,
        generation=generation,
        seq=max(1, int(seq)),
    )


def decode_nsl1_video_payload(payload: bytes) -> tuple[int, int, int, bytes]:
    """Unpack ``(width, height, timestamp_us, annexb_bytes)`` from an NSL1 video payload."""
    if len(payload) < NSL1_VIDEO_META_SIZE:
        raise ValueError("NSL1 video payload shorter than metadata header")
    width, height, ts_us = NSL1_VIDEO_META_STRUCT.unpack_from(payload, 0)
    return width, height, ts_us, payload[NSL1_VIDEO_META_SIZE:]


def encode_nsl1_ack(seq: int, *, generation: int = 1, viewer_wait_us: int | None = None) -> bytes:
    """Encode an upstream NSL1 viewer ACK (20B or 24B with viewer wait microseconds)."""
    payload = b""
    if viewer_wait_us is not None:
        clamped = max(0, min(0xFFFFFFFF, int(viewer_wait_us)))
        payload = NSL1_ACK_WAIT_STRUCT.pack(clamped)
    return encode_nsl1_frame(NSL1_MSG_ACK, payload, generation=generation, seq=seq)


def decode_nsl1_cursor_payload(payload: bytes) -> dict[str, Any]:
    """Decode an NSL1 cursor message (type 6): 8 zero bytes = default cursor, else RGBA sprite."""
    if len(payload) < NSL1_CURSOR_META_SIZE:
        raise ValueError("NSL1 cursor payload shorter than 8-byte header")
    w, h, hot_x, hot_y = NSL1_CURSOR_META_STRUCT.unpack_from(payload, 0)
    rgba = payload[NSL1_CURSOR_META_SIZE:]
    if w > 256 or h > 256 or len(rgba) != w * h * 4:
        raise ValueError("Invalid NSL1 cursor dimensions or RGBA byte length")
    return {
        "width": w,
        "height": h,
        "hot_x": hot_x,
        "hot_y": hot_y,
        "default_cursor": (w == 0 and h == 0 and hot_x == 0 and hot_y == 0),
        "rgba": rgba,
    }


def extract_h264_codec_string(annexb: bytes) -> str:
    """Extract WebCodecs ``avc1.PPCCLL`` codec string from H.264 Annex-B SPS NAL unit."""
    n = len(annexb)
    i = 0
    while i + 7 < n:
        nal_offset = 0
        if annexb[i] == 0 and annexb[i + 1] == 0 and annexb[i + 2] == 1:
            nal_offset = i + 3
        elif annexb[i] == 0 and annexb[i + 1] == 0 and annexb[i + 2] == 0 and annexb[i + 3] == 1:
            nal_offset = i + 4
        if nal_offset and nal_offset + 3 < n and (annexb[nal_offset] & 0x1F) == 7:
            p, c, lvl = annexb[nal_offset + 1], annexb[nal_offset + 2], annexb[nal_offset + 3]
            return f"avc1.{p:02x}{c:02x}{lvl:02x}"
        i += 1
    return "avc1.42e01f"


def _norm_to_u16(v: float) -> int:
    clamped = max(0.0, min(1.0, float(v)))
    return int(round(clamped * 65535.0))


def _u16_to_norm(v: int) -> float:
    return max(0.0, min(1.0, float(v) / 65535.0))


def _quantize_wheel_delta(delta: int) -> int:
    """Normalize arbitrary scroll deltas to upstream screen-relay multiples of 120 in [-1200, 1200]."""
    if delta == 0:
        return -120
    notches = int(round(float(delta) / 120.0))
    if notches == 0:
        notches = 1 if delta > 0 else -1
    return max(-1200, min(1200, notches * 120))


def encode_input_record(
    event_type: int,
    *,
    code: int = 0,
    value: int = 0,
    x_norm: float = 0.0,
    y_norm: float = 0.0,
    buttons: int = 0,
    modifiers: int = 0,
    seq: int = 0,  # accepted for caller compatibility; unused in wire record per upstream spec
) -> bytes:
    """Encode a single 16-byte big-endian screen-relay binary input record."""
    _ = seq
    etype = int(event_type) & 0xFF
    if etype in (INPUT_TYPE_RELEASE_ALL, INPUT_TYPE_TEXT):
        btn_mask = 0
        mod_mask = 0
        x_u16 = 0
        y_u16 = 0
    else:
        btn_mask = int(buttons) & 0x001F
        mod_mask = int(modifiers) & 0x00FF
        x_u16 = _norm_to_u16(x_norm)
        y_u16 = _norm_to_u16(y_norm)
    clean_val = int(value)
    if etype == INPUT_TYPE_WHEEL:
        clean_val = _quantize_wheel_delta(clean_val)
    return NSL1_INPUT_RECORD_STRUCT.pack(
        etype,
        0,  # reserved
        int(code) & 0xFFFF,
        max(-2147483648, min(2147483647, clean_val)),
        x_u16,
        y_u16,
        btn_mask,
        mod_mask,
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
        etype, _res, code, val, x_u16, y_u16, buttons, modifiers = NSL1_INPUT_RECORD_STRUCT.unpack_from(
            data, i * NSL1_INPUT_RECORD_SIZE
        )
        out.append(
            {
                "type": etype,
                "code": code,
                "value": val,
                "x_norm": _u16_to_norm(x_u16),
                "y_norm": _u16_to_norm(y_u16),
                "buttons": buttons & 0x1F,
                "modifiers": modifiers & 0xFF,
            }
        )
    return out


def json_input_to_nsl1_records(event: dict[str, Any], *, seq: int = 0) -> bytes:
    """Convert a Dashboard JSON input event into 16-byte screen-relay binary records."""
    _ = seq
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
    if action in ("move", "mouse_move", "mousemove", "pointermove"):
        records.append(encode_input_record(INPUT_TYPE_POINTER_MOVE, x_norm=nx, y_norm=ny))
    elif action in ("click", "right_click", "middle_click", "double_click"):
        clicks = 2 if action == "double_click" else max(1, int(event.get("clicks", 1)))
        records.append(encode_input_record(INPUT_TYPE_POINTER_MOVE, x_norm=nx, y_norm=ny))
        for _ in range(clicks):
            records.append(encode_input_record(INPUT_TYPE_BUTTON_DOWN, code=btn_code, x_norm=nx, y_norm=ny))
            records.append(encode_input_record(INPUT_TYPE_BUTTON_UP, code=btn_code, x_norm=nx, y_norm=ny))
    elif action in ("mousedown", "buttondown"):
        records.append(encode_input_record(INPUT_TYPE_BUTTON_DOWN, code=btn_code, x_norm=nx, y_norm=ny))
    elif action in ("mouseup", "buttonup"):
        records.append(encode_input_record(INPUT_TYPE_BUTTON_UP, code=btn_code, x_norm=nx, y_norm=ny))
    elif action in ("scroll", "wheel", "mouse_scroll"):
        axis = 1 if str(event.get("axis") or "vertical").lower().startswith("h") else 0
        delta = _quantize_wheel_delta(int(event.get("delta", -120)))
        records.append(encode_input_record(INPUT_TYPE_WHEEL, code=axis, value=delta, x_norm=nx, y_norm=ny))
    elif action in ("type", "type_text", "text"):
        text = str(event.get("text") or "")[:NSL1_MAX_INPUT_BATCH]
        for ch in text:
            cp = ord(ch)
            if 1 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                records.append(encode_input_record(INPUT_TYPE_TEXT, value=cp))
    elif action in ("key_down", "keydown"):
        code = max(0, min(231, int(event.get("code") or 0)))
        records.append(encode_input_record(INPUT_TYPE_KEY_DOWN, code=code))
    elif action in ("key_up", "keyup"):
        code = max(0, min(231, int(event.get("code") or 0)))
        records.append(encode_input_record(INPUT_TYPE_KEY_UP, code=code))
    elif action in ("release_all", "releaseall"):
        records.append(encode_input_record(INPUT_TYPE_RELEASE_ALL))
    else:
        raise ValueError(f"Unsupported screen-relay input action: {action!r}")
    return encode_input_batch(records)


# ---------------------------------------------------------------------------
# HID Keycode Translation for Fallback Local Execution
# ---------------------------------------------------------------------------

_HID_SPECIAL_KEYS: dict[int, str] = {
    40: "enter",
    41: "escape",
    42: "backspace",
    43: "tab",
    44: "space",
    45: "-",
    46: "=",
    47: "[",
    48: "]",
    49: "\\",
    51: ";",
    52: "'",
    53: "`",
    54: ",",
    55: ".",
    56: "/",
    57: "capslock",
    70: "printscreen",
    73: "insert",
    74: "home",
    75: "pageup",
    76: "delete",
    77: "end",
    78: "pagedown",
    79: "right",
    80: "left",
    81: "down",
    82: "up",
    224: "ctrl",
    225: "shift",
    226: "alt",
    227: "win",
    228: "ctrl",
    229: "shift",
    230: "alt",
    231: "win",
}


def hid_usage_to_key_name(usage: int) -> str | None:
    """Convert USB HID keyboard usage ID (4..231) to AI Bridge key token."""
    if 4 <= usage <= 29:
        return chr(ord("a") + (usage - 4))
    if 30 <= usage <= 38:
        return chr(ord("1") + (usage - 30))
    if usage == 39:
        return "0"
    if 58 <= usage <= 69:
        return f"f{usage - 57}"
    if 104 <= usage <= 115:
        return f"f{usage - 91}"
    return _HID_SPECIAL_KEYS.get(usage)


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
    """Locate the native ``relay-engine`` executable if installed."""
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
        Path("/usr/local/lib/relay"),
        Path("/usr/local/bin"),
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


def _write_private_token_file(directory: Path, token_hex: str) -> Path:
    """Write a 64-lowercase-hex token file with owner-only permissions as required by relay-engine."""
    token_path = directory / "session.token"
    if os.name != "nt":
        fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token_hex + "\n")
        os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
    else:
        token_path.write_text(token_hex + "\n", encoding="ascii")
        try:
            # Restrict Windows ACL to current user so relay-engine's privateTokenFile() check passes
            user = os.environ.get("USERNAME") or ""
            if user:
                subprocess.run(
                    ["icacls", str(token_path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                )
        except Exception:
            pass
    return token_path


def _ensure_viewer_stub_dir(runtime_dir: Path, binary_path: str) -> Path:
    """Provide ``index.html`` and ``viewer.js`` required by ``relay-engine --viewer-dir``."""
    bin_p = Path(binary_path).resolve()
    for cand in (bin_p.parent / "viewer", bin_p.parent.parent / "lib" / "relay" / "viewer"):
        if (cand / "index.html").is_file() and (cand / "viewer.js").is_file():
            return cand
    viewer_dir = runtime_dir / "viewer"
    viewer_dir.mkdir(parents=True, exist_ok=True)
    idx = viewer_dir / "index.html"
    js = viewer_dir / "viewer.js"
    if not idx.is_file():
        idx.write_text("<!doctype html><title>AI Bridge Relay Loopback</title>\n", encoding="utf-8")
    if not js.is_file():
        js.write_text("// loopback headless viewer stub\n", encoding="utf-8")
    return viewer_dir


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
        output_name: str | None = None,
        backend: str | None = None,
    ) -> None:
        self.binary_path = binary_path or find_screen_relay_binary()
        self.host = "127.0.0.1"  # Invariant: NEVER bind 0.0.0.0
        self.port = int(port) if port else _pick_free_loopback_port()
        self.fps = max(10, min(120, int(fps)))
        self.bitrate_kbps = max(1000, min(40000, int(bitrate_kbps)))
        self.output_name = (output_name or "").strip()
        self.backend = (backend or "").strip()
        self._admin_token = secrets.token_hex(32)  # Exact 64 lowercase hex characters
        self._runtime_dir: Path | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._admin_token

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_stream_url(self) -> str:
        return f"ws://{self.host}:{self.port}/stream"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def check_backend(self, *, timeout_seconds: float = 3.0) -> bool:
        """Run ``relay-engine --check`` to verify display capture/input availability before streaming."""
        if not self.binary_path:
            return False
        cmd = [self.binary_path, "--check"]
        if self.backend:
            cmd.extend(["--backend", self.backend])
        if self.output_name:
            cmd.extend(["--output", self.output_name])
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False

    def start(self, *, timeout_seconds: float = 5.0) -> bool:
        if not self.binary_path:
            return False
        with self._lock:
            if self.running:
                return True
            runtime_dir = Path(tempfile.mkdtemp(prefix="aibridge-relay-"))
            if os.name != "nt":
                os.chmod(runtime_dir, 0o700)
            self._runtime_dir = runtime_dir
            token_file = _write_private_token_file(runtime_dir, self._admin_token)
            viewer_dir = _ensure_viewer_stub_dir(runtime_dir, self.binary_path)

            bps_start = max(1_500_000, min(8_000_000, self.bitrate_kbps * 1000))
            bps_min = min(1_500_000, bps_start)
            bps_max = max(8_000_000, bps_start)

            cmd = [
                self.binary_path,
                "--bind",
                self.host,
                "--port",
                str(self.port),
                "--fps",
                str(self.fps),
                "--bitrate-min",
                str(bps_min),
                "--bitrate-start",
                str(bps_start),
                "--bitrate-max",
                str(bps_max),
                "--token-file",
                str(token_file),
                "--viewer-dir",
                str(viewer_dir),
            ]
            if self.backend:
                cmd.extend(["--backend", self.backend])
            if self.output_name:
                cmd.extend(["--output", self.output_name])

            popen_kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                self._proc = subprocess.Popen(cmd, **popen_kwargs)
            except Exception as exc:
                logger.warning("Failed to spawn relay-engine at %s: %s", self.binary_path, exc)
                self._cleanup_runtime_dir()
                return False

        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline:
            if not self.running:
                self._cleanup_runtime_dir()
                return False
            try:
                r = httpx.get(f"{self.base_url}/capabilities", headers=self._auth_headers(), timeout=0.5)
                if r.status_code == 200:
                    return True
            except Exception:
                time.sleep(0.08)
        if not self.running:
            self._cleanup_runtime_dir()
        return self.running

    def _cleanup_runtime_dir(self) -> None:
        rdir = self._runtime_dir
        self._runtime_dir = None
        if rdir is not None:
            shutil.rmtree(rdir, ignore_errors=True)

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
        self._cleanup_runtime_dir()

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

    def get_clipboard(self) -> str:
        resp = httpx.get(f"{self.base_url}/clipboard", headers=self._auth_headers(), timeout=2.0)
        resp.raise_for_status()
        return resp.text

    def set_clipboard(self, text: str) -> bool:
        resp = httpx.post(
            f"{self.base_url}/clipboard",
            headers={**self._auth_headers(), "Content-Type": "text/plain; charset=utf-8"},
            content=text.encode("utf-8"),
            timeout=2.0,
        )
        return resp.status_code in (200, 202, 204)

    def mint_watch_grant(self, *, ttl: int = 300) -> dict[str, Any]:
        """Mint a strictly View-Only watch grant on the loopback engine."""
        clamped_ttl = max(1, min(3600, int(ttl)))
        resp = httpx.post(
            f"{self.base_url}/grants/watch?ttl={clamped_ttl}&connections=1",
            headers=self._auth_headers(),
            timeout=3.0,
        )
        resp.raise_for_status()
        return resp.json()

    def mint_control_grant(self, *, ttl: int = 300) -> dict[str, Any]:
        """Mint a single-connection control grant (only called after AI Bridge policy check)."""
        clamped_ttl = max(1, min(3600, int(ttl)))
        resp = httpx.post(
            f"{self.base_url}/grants/control?ttl={clamped_ttl}&connections=1",
            headers=self._auth_headers(),
            timeout=3.0,
        )
        resp.raise_for_status()
        return resp.json()

    def post_input_records(self, raw_records: bytes) -> bool:
        """Forward 1..256 16-byte binary input records to the loopback engine HTTP /input endpoint."""
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
        elif etype == INPUT_TYPE_BUTTON_DOWN:
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
        elif etype == INPUT_TYPE_TEXT:
            cp = int(rec["value"])
            if 0 < cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                perform_approved_ui_action("type_text", text=chr(cp), approval_granted=True)
                executed += 1
        elif etype == INPUT_TYPE_KEY_DOWN:
            key_name = hid_usage_to_key_name(int(rec["code"]))
            if key_name:
                perform_approved_ui_action("press_key", key=key_name, approval_granted=True)
                executed += 1
    return executed


class LocalNsl1Streamer:
    """Streams live desktop Annex-B H.264 frames wrapped in 20-byte NSL1 packets.

    Primary encoder: spawns ``relay-engine`` on ``127.0.0.1:<ephemeral>`` and bridges
    its ``/stream?take=1&clipboard=1`` ``nsl1`` WebSocket frames zero-copy.
    Warm fallback: encodes desktop frames via PyAV ``libx264`` when ``relay-engine``
    is absent or headless.
    """

    def __init__(
        self,
        session_id: str,
        *,
        fps: int = 60,
        scale: float = 1.0,
        bitrate_kbps: int = 4500,
        monitor_index: int = 0,
        on_binary_frame: Callable[[bytes], Any] | None = None,
        control_url: str | None = None,
        device_token: str | None = None,
    ) -> None:
        self.session_id = session_id
        has_native = bool(find_screen_relay_binary())
        self.fps = max(10 if has_native else 1, min(120 if has_native else 60, int(fps)))
        self.scale = max(0.25, min(1.0, float(scale)))
        self.bitrate_kbps = max(300, min(40000, int(bitrate_kbps)))
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
        self._last_viewer_ack_mono = 0.0
        self._backend = "relay_engine" if has_native else "pyav_h264_nsl1"
        self._engine: ScreenRelayEngine | None = None
        self._engine_ws: Any = None
        self._engine_loop: asyncio.AbstractEventLoop | None = None
        self._latest_keyframe_packet: bytes | None = None

    def switch_monitor(self, monitor_index: int) -> int:
        self.monitor_index = max(0, int(monitor_index))
        self._force_keyframe.set()
        return self.monitor_index

    def request_keyframe(self) -> None:
        self._force_keyframe.set()
        # If native relay-engine is active and we cached its latest IDR, re-emit immediately for instant viewer join
        cached = self._latest_keyframe_packet
        if cached and self._on_binary_frame is not None and self._engine is not None and self._engine.running:
            try:
                self._on_binary_frame(cached)
            except Exception:
                pass

    def note_ack(self, seq: int, *, generation: int = 1, raw_ack: bytes | None = None) -> None:
        self._last_ack_seq = max(self._last_ack_seq, int(seq))
        self._last_viewer_ack_mono = time.monotonic()
        eng_ws = self._engine_ws
        eng_loop = self._engine_loop
        if eng_ws is not None and eng_loop is not None:
            pkt = raw_ack if raw_ack else encode_nsl1_ack(seq, generation=generation)
            try:
                asyncio.run_coroutine_threadsafe(eng_ws.send(pkt), eng_loop)
            except Exception:
                pass

    def handle_binary_input(self, raw_records: bytes) -> int:
        if not raw_records or len(raw_records) % NSL1_INPUT_RECORD_SIZE != 0:
            raise ValueError("Invalid binary input payload")
        count = len(raw_records) // NSL1_INPUT_RECORD_SIZE
        if count > NSL1_MAX_INPUT_BATCH:
            raise ValueError("Binary input batch exceeds 256 records")
        eng_ws = self._engine_ws
        eng_loop = self._engine_loop
        if eng_ws is not None and eng_loop is not None:
            # Upstream /stream expects one 16-byte record per NSL1_MSG_INPUT packet with seq=0
            for idx in range(count):
                rec = raw_records[idx * NSL1_INPUT_RECORD_SIZE : (idx + 1) * NSL1_INPUT_RECORD_SIZE]
                pkt = encode_nsl1_frame(NSL1_MSG_INPUT, rec, generation=max(1, self._generation), seq=0)
                asyncio.run_coroutine_threadsafe(eng_ws.send(pkt), eng_loop)
            return count
        if self._engine is not None and self._engine.running:
            self._engine.post_input_records(raw_records)
            return count
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

    def _try_start_native_engine(self) -> ScreenRelayEngine | None:
        bin_path = find_screen_relay_binary()
        if not bin_path:
            return None
        eng = ScreenRelayEngine(
            binary_path=bin_path,
            fps=max(10, min(120, self.fps)),
            bitrate_kbps=self.bitrate_kbps,
        )
        if eng.start(timeout_seconds=4.5):
            self._engine = eng
            self._backend = "relay_engine"
            return eng
        eng.stop()
        logger.info("Native relay-engine unavailable on this display; falling back to PyAV H.264 NSL1.")
        self._backend = "pyav_h264_nsl1"
        return None

    def _run_entry(self) -> None:
        try:
            if self._control_url and self._device_token:
                asyncio.run(self._run_outbound_ws_loop())
            else:
                if self._try_start_native_engine() is not None:
                    try:
                        asyncio.run(self._run_local_native_engine_bridge())
                        return
                    except Exception as exc:
                        logger.warning("Local native relay-engine bridge exited (%s); using PyAV fallback", exc)
                        if self._engine is not None:
                            self._engine.stop()
                            self._engine = None
                        self._backend = "pyav_h264_nsl1"
                self._run_local_encode_loop(emit_fn=self._on_binary_frame)
        except Exception as exc:
            logger.warning("screen_relay session %s ended with error: %s", self.session_id, exc)
        finally:
            self._engine_ws = None
            self._engine_loop = None
            if self._engine is not None:
                self._engine.stop()
                self._engine = None
            with _RELAY_LOCK:
                if _RELAY_SESSIONS.get(self.session_id) is self:
                    _RELAY_SESSIONS.pop(self.session_id, None)

    async def _run_local_native_engine_bridge(self) -> None:
        """Pump packets from loopback ``relay-engine`` into ``self._on_binary_frame``."""
        import websockets

        assert self._engine is not None
        self._engine_loop = asyncio.get_running_loop()
        stream_url = f"{self._engine.ws_stream_url}?take=1&clipboard=1"
        subprotocols = [NSL1_SUBPROTOCOL, f"bearer.{self._engine.token}"]
        async with websockets.connect(
            stream_url,
            subprotocols=subprotocols,  # type: ignore[arg-type]
            max_size=NSL1_MAX_PAYLOAD_BYTES + 64,
        ) as eng_ws:
            self._engine_ws = eng_ws
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(eng_ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                pkt = bytes(raw)
                try:
                    frame = decode_nsl1_frame(pkt)
                except ValueError:
                    continue
                self._generation = frame.generation
                if frame.msg_type == NSL1_MSG_VIDEO:
                    self._seq = frame.seq
                    self._frames_sent += 1
                    if len(frame.payload) >= NSL1_VIDEO_META_SIZE:
                        w, h, _ts, _nal = decode_nsl1_video_payload(frame.payload)
                        self._width, self._height = w, h
                    if frame.is_keyframe:
                        self._latest_keyframe_packet = pkt
                    # Auto-ACK when no browser viewer has ACKed in the past 1.5s so relay-engine's 3s window stays alive
                    if time.monotonic() - self._last_viewer_ack_mono > 1.5:
                        await eng_ws.send(encode_nsl1_ack(frame.seq, generation=frame.generation))
                if self._on_binary_frame is not None:
                    self._on_binary_frame(pkt)

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

        eff_fps = max(1, min(60, self.fps))
        ctx = codec_ctx_holder.get("ctx")
        if ctx is None or codec_ctx_holder.get("size") != (w, h):
            ctx = av.CodecContext.create("libx264", "w")
            ctx.width = w
            ctx.height = h
            ctx.pix_fmt = "yuv420p"
            ctx.time_base = fractions.Fraction(1, eff_fps)
            ctx.bit_rate = min(8_000_000, max(1_500_000, self.bitrate_kbps * 1000))
            ctx.options = {
                "preset": "ultrafast",
                "tune": "zerolatency",
                "profile": "baseline",
                "repeat-headers": "1",
                "keyint": str(max(15, eff_fps * 2)),
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
        pkt = encode_nsl1_video_frame(
            annexb,
            width=w,
            height=h,
            timestamp_us=int(time.monotonic() * 1_000_000),
            is_keyframe=is_key,
            generation=self._generation,
            seq=self._seq,
        )
        if is_key:
            self._latest_keyframe_packet = pkt
        return pkt

    def _run_local_encode_loop(self, *, emit_fn: Callable[[bytes], Any] | None) -> None:
        codec_holder: dict[str, Any] = {}
        eff_fps = max(1, min(60, self.fps))
        interval = 1.0 / float(eff_fps)
        while not self._stop.is_set():
            t0 = time.monotonic()
            if self._seq - self._last_ack_seq > 20 and self._last_ack_seq > 0 and (t0 - self._last_viewer_ack_mono) < 2.5:
                time.sleep(0.015)
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

    def _build_outbound_ssl_context(self, ws_url: str) -> ssl.SSLContext | None:
        if not ws_url.startswith("wss://"):
            return None
        ca_bundle = os.environ.get("AI_BRIDGE_CA_BUNDLE", "").strip()
        ctx = ssl.create_default_context(cafile=ca_bundle if ca_bundle and Path(ca_bundle).is_file() else None)
        client_cert = os.environ.get("AI_BRIDGE_CLIENT_CERT", "").strip()
        client_key = os.environ.get("AI_BRIDGE_CLIENT_KEY", "").strip()
        if client_cert and client_key and Path(client_cert).is_file() and Path(client_key).is_file():
            ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
        return ctx

    async def _run_outbound_ws_loop(self) -> None:
        """Connect outbound to Control Plane WSS with auto-reconnect and bridge NSL1 frames."""
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Outbound screen-relay tunnel requires the 'websockets' package.") from exc

        ws_base = self._control_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/api/v1/ws/sessions/{self.session_id}"
        headers = {"X-Device-Token": self._device_token}
        ssl_ctx = self._build_outbound_ssl_context(url)

        native_eng = await asyncio.to_thread(self._try_start_native_engine)
        codec_holder: dict[str, Any] = {}
        backoff = 0.5

        while not self._stop.is_set():
            try:
                self._force_keyframe.set()
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ssl=ssl_ctx,
                    max_size=NSL1_MAX_PAYLOAD_BYTES + 64,
                    ping_interval=20,
                    ping_timeout=20,
                ) as cp_ws:
                    backoff = 0.5
                    if native_eng is not None and native_eng.running:
                        await self._bridge_native_to_outbound(native_eng, cp_ws)
                    else:
                        await self._stream_pyav_to_outbound(cp_ws, codec_holder)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("Outbound screen_relay WS dropped (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(8.0, backoff * 2.0)

    async def _bridge_native_to_outbound(self, native_eng: ScreenRelayEngine, cp_ws: Any) -> None:
        """Zero-copy bidirectional bridge between local ``relay-engine`` and Control Plane WSS."""
        import websockets

        self._engine_loop = asyncio.get_running_loop()
        stream_url = f"{native_eng.ws_stream_url}?take=1&clipboard=1"
        subprotocols = [NSL1_SUBPROTOCOL, f"bearer.{native_eng.token}"]

        async with websockets.connect(
            stream_url,
            subprotocols=subprotocols,  # type: ignore[arg-type]
            max_size=NSL1_MAX_PAYLOAD_BYTES + 64,
        ) as eng_ws:
            self._engine_ws = eng_ws

            async def _pump_engine_to_cp() -> None:
                while not self._stop.is_set():
                    raw = await eng_ws.recv()
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    pkt = bytes(raw)
                    try:
                        frame = decode_nsl1_frame(pkt)
                    except ValueError:
                        continue
                    self._generation = frame.generation
                    if frame.msg_type == NSL1_MSG_VIDEO:
                        self._seq = frame.seq
                        self._frames_sent += 1
                        if frame.is_keyframe:
                            self._latest_keyframe_packet = pkt
                        if time.monotonic() - self._last_viewer_ack_mono > 1.5:
                            await eng_ws.send(encode_nsl1_ack(frame.seq, generation=frame.generation))
                    await cp_ws.send(pkt)

            async def _pump_cp_to_engine() -> None:
                while not self._stop.is_set():
                    raw = await cp_ws.recv()
                    if isinstance(raw, (bytes, bytearray)):
                        try:
                            frame = decode_nsl1_frame(bytes(raw))
                        except ValueError:
                            continue
                        if frame.msg_type == NSL1_MSG_ACK:
                            self._last_ack_seq = max(self._last_ack_seq, frame.seq)
                            self._last_viewer_ack_mono = time.monotonic()
                            await eng_ws.send(bytes(raw))
                        elif frame.msg_type == NSL1_MSG_INPUT:
                            count = len(frame.payload) // NSL1_INPUT_RECORD_SIZE
                            for idx in range(count):
                                rec = frame.payload[idx * NSL1_INPUT_RECORD_SIZE : (idx + 1) * NSL1_INPUT_RECORD_SIZE]
                                await eng_ws.send(
                                    encode_nsl1_frame(NSL1_MSG_INPUT, rec, generation=max(1, self._generation), seq=0)
                                )
                        elif frame.msg_type == NSL1_MSG_CONTROL:
                            if self._latest_keyframe_packet:
                                await cp_ws.send(self._latest_keyframe_packet)
                    elif isinstance(raw, str):
                        try:
                            msg = json.loads(raw)
                            mtype = str(msg.get("type") or "").lower()
                            if mtype == "session_terminated":
                                self._stop.set()
                                return
                            if mtype == "request_keyframe" and self._latest_keyframe_packet:
                                await cp_ws.send(self._latest_keyframe_packet)
                            elif mtype == "clipboard_set" and isinstance(msg.get("text"), str):
                                await asyncio.to_thread(native_eng.set_clipboard, msg["text"])
                        except Exception:
                            pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(_pump_engine_to_cp()), asyncio.create_task(_pump_cp_to_engine())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and not self._stop.is_set():
                    raise exc

    async def _stream_pyav_to_outbound(self, cp_ws: Any, codec_holder: dict[str, Any]) -> None:
        """Fallback PyAV H.264 outbound streamer when native ``relay-engine`` is not present."""
        eff_fps = max(1, min(60, self.fps))
        interval = 1.0 / float(eff_fps)
        conn_closed = asyncio.Event()

        async def _recv_loop() -> None:
            while not self._stop.is_set():
                try:
                    raw = await cp_ws.recv()
                except Exception:
                    conn_closed.set()
                    return
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        frame = decode_nsl1_frame(bytes(raw))
                    except ValueError:
                        continue
                    if frame.msg_type == NSL1_MSG_ACK:
                        self.note_ack(frame.seq, generation=frame.generation)
                    elif frame.msg_type == NSL1_MSG_INPUT:
                        await asyncio.to_thread(self.handle_binary_input, frame.payload)
                    elif frame.msg_type == NSL1_MSG_CONTROL:
                        self.request_keyframe()
                elif isinstance(raw, str):
                    try:
                        msg = json.loads(raw)
                        mtype = str(msg.get("type") or "").lower()
                        if mtype == "session_terminated":
                            self._stop.set()
                            conn_closed.set()
                            return
                        if mtype in ("switch_monitor", "set_monitor") and "monitor_index" in msg:
                            self.switch_monitor(int(msg["monitor_index"]))
                        elif mtype == "request_keyframe":
                            self.request_keyframe()
                    except Exception:
                        pass

        recv_task = asyncio.create_task(_recv_loop())
        try:
            while not self._stop.is_set() and not conn_closed.is_set():
                t0 = time.monotonic()
                if (
                    self._seq - self._last_ack_seq > 20
                    and self._last_ack_seq > 0
                    and (t0 - self._last_viewer_ack_mono) < 2.5
                ):
                    await asyncio.sleep(0.015)
                    continue
                try:
                    packet = await asyncio.to_thread(self._encode_frame_loop_iter, codec_holder)
                except Exception:
                    await asyncio.sleep(0.25)
                    continue
                if packet:
                    await cp_ws.send(packet)
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
            if conn_closed.is_set() and not self._stop.is_set():
                raise ConnectionError("Control Plane WebSocket closed")
        finally:
            recv_task.cancel()


def start_relay_session(
    session_id: str,
    *,
    fps: int = 60,
    scale: float = 1.0,
    bitrate_kbps: int = 4500,
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
        "backend": streamer._backend,
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
