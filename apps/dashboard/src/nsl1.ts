/**
 * Byte-exact browser codec for vercel-labs/screen-relay (`nsl1`) binary framing,
 * dynamic H.264 SPS WebCodecs configuration, RGBA hardware cursor sprites,
 * viewer wait-time ACKs, and 16-byte big-endian input records.
 */

export const NSL1_HEADER_SIZE = 20;
export const NSL1_VIDEO_META_SIZE = 12;
export const NSL1_CURSOR_META_SIZE = 8;
export const NSL1_INPUT_RECORD_SIZE = 16;

// Exact upstream NSL1 message types (PROTOCOL.md)
export const NSL1_MSG_VIDEO = 1;
export const NSL1_MSG_ACK = 2;
export const NSL1_MSG_INPUT = 3;
export const NSL1_MSG_CONTROL_READY = 4;
export const NSL1_MSG_CONTROL = 5;
export const NSL1_MSG_CURSOR = 6;
export const NSL1_MSG_CLIPBOARD = 7;

export const NSL1_FLAG_KEYFRAME = 0x0001;

// Exact upstream 16-byte input event types (PROTOCOL.md)
export const INPUT_TYPE_KEY_DOWN = 1;
export const INPUT_TYPE_KEY_UP = 2;
export const INPUT_TYPE_POINTER_MOVE = 3;
export const INPUT_TYPE_BUTTON_DOWN = 4;
export const INPUT_TYPE_BUTTON_UP = 5;
export const INPUT_TYPE_WHEEL = 6;
export const INPUT_TYPE_RELEASE_ALL = 7;
export const INPUT_TYPE_TEXT = 8;

// Backwards-compatible aliases
export const INPUT_TYPE_POINTER_BUTTON = INPUT_TYPE_BUTTON_DOWN;
export const INPUT_TYPE_UNICODE = INPUT_TYPE_TEXT;

// Exact upstream mouse button codes (1=left, 2=middle, 3=right)
export const BUTTON_CODE_LEFT = 1;
export const BUTTON_CODE_MIDDLE = 2;
export const BUTTON_CODE_RIGHT = 3;

export interface DecodedNsl1Frame {
  version: number;
  msgType: number;
  flags: number;
  isKeyframe: boolean;
  generation: number;
  seq: number;
  payload: Uint8Array;
}

export interface DecodedNsl1Video {
  width: number;
  height: number;
  timestampUs: number;
  annexb: Uint8Array;
}

export interface DecodedNsl1Cursor {
  width: number;
  height: number;
  hotX: number;
  hotY: number;
  defaultCursor: boolean;
  pixels: Uint8ClampedArray;
}

export function webCodecsSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof (window as unknown as { VideoDecoder?: unknown }).VideoDecoder === "function" &&
    typeof (window as unknown as { EncodedVideoChunk?: unknown }).EncodedVideoChunk === "function"
  );
}

/**
 * Extracts the exact `avc1.PPCCLL` codec string from an H.264 Annex-B keyframe's SPS NAL unit
 * (supporting Constrained Baseline, Main, and High profiles up to 4K 60/120fps).
 */
export function extractH264CodecFromAnnexB(bytes: Uint8Array): string {
  for (let i = 0; i + 7 < bytes.length; i++) {
    let n = 0;
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1) {
      n = i + 3;
    } else if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 0 && bytes[i + 3] === 1) {
      n = i + 4;
    }
    if (n && n + 3 < bytes.length && (bytes[n] & 0x1f) === 7) {
      const hex = [bytes[n + 1], bytes[n + 2], bytes[n + 3]]
        .map((v) => v.toString(16).padStart(2, "0"))
        .join("");
      return `avc1.${hex}`;
    }
  }
  return "avc1.42e01f";
}

export function decodeNsl1Packet(buffer: ArrayBuffer): DecodedNsl1Frame | null {
  if (buffer.byteLength < NSL1_HEADER_SIZE) return null;
  const view = new DataView(buffer);
  // Magic "NSL1" = 0x4E 0x53 0x4C 0x31
  if (
    view.getUint8(0) !== 0x4e ||
    view.getUint8(1) !== 0x53 ||
    view.getUint8(2) !== 0x4c ||
    view.getUint8(3) !== 0x31
  ) {
    return null;
  }
  const version = view.getUint8(4);
  if (version !== 1) return null;
  const msgType = view.getUint8(5);
  const flags = view.getUint16(6, false);
  const generation = view.getUint32(8, false);
  const seq = view.getUint32(12, false);
  const payloadLen = view.getUint32(16, false);
  if (buffer.byteLength - NSL1_HEADER_SIZE !== payloadLen) return null;
  return {
    version,
    msgType,
    flags,
    isKeyframe: (flags & NSL1_FLAG_KEYFRAME) !== 0,
    generation,
    seq,
    payload: new Uint8Array(buffer, NSL1_HEADER_SIZE, payloadLen),
  };
}

export function decodeNsl1VideoPayload(payload: Uint8Array): DecodedNsl1Video | null {
  if (payload.byteLength <= NSL1_VIDEO_META_SIZE) return null;
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const width = view.getUint16(0, false);
  const height = view.getUint16(2, false);
  const hi = view.getUint32(4, false);
  const lo = view.getUint32(8, false);
  const timestampUs = hi * 4294967296 + lo;
  return {
    width,
    height,
    timestampUs,
    annexb: payload.subarray(NSL1_VIDEO_META_SIZE),
  };
}

export function decodeNsl1CursorPayload(payload: Uint8Array): DecodedNsl1Cursor | null {
  if (payload.byteLength < NSL1_CURSOR_META_SIZE) return null;
  const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
  const width = view.getUint16(0, false);
  const height = view.getUint16(2, false);
  const hotX = view.getUint16(4, false);
  const hotY = view.getUint16(6, false);
  const defaultCursor = width === 0 && height === 0 && hotX === 0 && hotY === 0;
  if (width > 256 || height > 256 || payload.byteLength !== NSL1_CURSOR_META_SIZE + width * height * 4) {
    return null;
  }
  return {
    width,
    height,
    hotX,
    hotY,
    defaultCursor,
    pixels: new Uint8ClampedArray(
      payload.buffer,
      payload.byteOffset + NSL1_CURSOR_META_SIZE,
      width * height * 4,
    ),
  };
}

export function encodeNsl1Frame(
  msgType: number,
  payload: Uint8Array = new Uint8Array(0),
  opts?: { flags?: number; generation?: number; seq?: number },
): Uint8Array {
  const out = new Uint8Array(NSL1_HEADER_SIZE + payload.byteLength);
  const view = new DataView(out.buffer);
  out[0] = 0x4e; // 'N'
  out[1] = 0x53; // 'S'
  out[2] = 0x4c; // 'L'
  out[3] = 0x31; // '1'
  view.setUint8(4, 1);
  view.setUint8(5, msgType & 0xff);
  const cleanFlags = msgType === NSL1_MSG_VIDEO ? (opts?.flags ?? 0) & NSL1_FLAG_KEYFRAME : 0;
  view.setUint16(6, cleanFlags, false);
  view.setUint32(8, Math.max(1, (opts?.generation ?? 1) >>> 0), false);
  view.setUint32(12, (opts?.seq ?? 0) >>> 0, false);
  view.setUint32(16, payload.byteLength >>> 0, false);
  if (payload.byteLength > 0) {
    out.set(payload, NSL1_HEADER_SIZE);
  }
  return out;
}

export function encodeNsl1Ack(seq: number, generation = 1, viewerWaitMs?: number): Uint8Array {
  if (viewerWaitMs === undefined) {
    return encodeNsl1Frame(NSL1_MSG_ACK, new Uint8Array(0), { generation, seq });
  }
  const payload = new Uint8Array(4);
  const waitUs = Number.isFinite(viewerWaitMs)
    ? Math.min(0xffffffff, Math.max(0, Math.round(viewerWaitMs * 1000)))
    : 0;
  new DataView(payload.buffer).setUint32(0, waitUs >>> 0, false);
  return encodeNsl1Frame(NSL1_MSG_ACK, payload, { generation, seq });
}

function writeInputRecord(
  view: DataView,
  offset: number,
  type: number,
  code: number,
  value: number,
  xNorm: number,
  yNorm: number,
  buttons = 0,
  modifiers = 0,
): void {
  const clamp01 = (v: number) => Math.max(0, Math.min(1, v));
  const zeroMasks = type === INPUT_TYPE_RELEASE_ALL || type === INPUT_TYPE_TEXT;
  view.setUint8(offset, type & 0xff);
  view.setUint8(offset + 1, 0);
  view.setUint16(offset + 2, code & 0xffff, false);
  view.setInt32(offset + 4, value | 0, false);
  view.setUint16(offset + 8, zeroMasks ? 0 : Math.round(clamp01(xNorm) * 65535), false);
  view.setUint16(offset + 10, zeroMasks ? 0 : Math.round(clamp01(yNorm) * 65535), false);
  view.setUint16(offset + 12, zeroMasks ? 0 : buttons & 0x1f, false);
  view.setUint16(offset + 14, zeroMasks ? 0 : modifiers & 0xff, false);
}

export function encodeNsl1PointerMove(xNorm: number, yNorm: number, buttons = 0, modifiers = 0): Uint8Array {
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE);
  writeInputRecord(new DataView(payload.buffer), 0, INPUT_TYPE_POINTER_MOVE, 0, 0, xNorm, yNorm, buttons, modifiers);
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1MouseButton(
  xNorm: number,
  yNorm: number,
  button: "left" | "middle" | "right",
  down: boolean,
  buttons = 0,
  modifiers = 0,
): Uint8Array {
  const btnCode =
    button === "right" ? BUTTON_CODE_RIGHT : button === "middle" ? BUTTON_CODE_MIDDLE : BUTTON_CODE_LEFT;
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE);
  writeInputRecord(
    new DataView(payload.buffer),
    0,
    down ? INPUT_TYPE_BUTTON_DOWN : INPUT_TYPE_BUTTON_UP,
    btnCode,
    0,
    xNorm,
    yNorm,
    buttons,
    modifiers,
  );
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1Wheel(
  xNorm: number,
  yNorm: number,
  deltaPx: number,
  axis: "vertical" | "horizontal" = "vertical",
): Uint8Array {
  const rawNotches = Math.round(deltaPx / 100);
  const notches = rawNotches === 0 ? (deltaPx >= 0 ? 1 : -1) : rawNotches;
  const clampedDelta = Math.max(-1200, Math.min(1200, notches * 120));
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE);
  writeInputRecord(
    new DataView(payload.buffer),
    0,
    INPUT_TYPE_WHEEL,
    axis === "horizontal" ? 1 : 0,
    clampedDelta,
    xNorm,
    yNorm,
  );
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1Key(hidUsage: number, down: boolean, modifiers = 0): Uint8Array {
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE);
  writeInputRecord(
    new DataView(payload.buffer),
    0,
    down ? INPUT_TYPE_KEY_DOWN : INPUT_TYPE_KEY_UP,
    Math.max(0, Math.min(231, hidUsage | 0)),
    0,
    0,
    0,
    0,
    modifiers,
  );
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1ReleaseAll(): Uint8Array {
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE);
  writeInputRecord(new DataView(payload.buffer), 0, INPUT_TYPE_RELEASE_ALL, 0, 0, 0, 0);
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1Click(
  xNorm: number,
  yNorm: number,
  button: "left" | "right" | "middle" = "left",
  seq = 0,
): Uint8Array {
  void seq;
  const btnCode =
    button === "right" ? BUTTON_CODE_RIGHT : button === "middle" ? BUTTON_CODE_MIDDLE : BUTTON_CODE_LEFT;
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE * 3);
  const view = new DataView(payload.buffer);
  writeInputRecord(view, 0, INPUT_TYPE_POINTER_MOVE, 0, 0, xNorm, yNorm);
  writeInputRecord(view, 16, INPUT_TYPE_BUTTON_DOWN, btnCode, 0, xNorm, yNorm);
  writeInputRecord(view, 32, INPUT_TYPE_BUTTON_UP, btnCode, 0, xNorm, yNorm);
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

export function encodeNsl1Text(text: string, seq = 0): Uint8Array {
  void seq;
  const scalars = Array.from(text)
    .map((ch) => ch.codePointAt(0) ?? 0)
    .filter((cp) => cp >= 1 && cp <= 0x10ffff && (cp < 0xd800 || cp > 0xdfff))
    .slice(0, 256);
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE * scalars.length);
  const view = new DataView(payload.buffer);
  scalars.forEach((cp, idx) => {
    writeInputRecord(view, idx * NSL1_INPUT_RECORD_SIZE, INPUT_TYPE_TEXT, 0, cp, 0, 0);
  });
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq: 0 });
}

// Complete KeyboardEvent.code -> USB HID Usage ID table matching vercel-labs/screen-relay
const DOM_CODE_TO_HID: Record<string, number> = (() => {
  const map: Record<string, number> = {};
  for (let i = 0; i < 26; i++) map[`Key${String.fromCharCode(65 + i)}`] = 4 + i;
  const digits = ["Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "Digit0"];
  digits.forEach((d, idx) => {
    map[d] = 30 + idx;
  });
  const row40 = [
    "Enter",
    "Escape",
    "Backspace",
    "Tab",
    "Space",
    "Minus",
    "Equal",
    "BracketLeft",
    "BracketRight",
    "Backslash",
  ];
  row40.forEach((k, idx) => {
    map[k] = 40 + idx;
  });
  const row51 = ["Semicolon", "Quote", "Backquote", "Comma", "Period", "Slash", "CapsLock"];
  row51.forEach((k, idx) => {
    map[k] = 51 + idx;
  });
  for (let i = 1; i <= 12; i++) map[`F${i}`] = 57 + i;
  const row70 = [
    "PrintScreen",
    "ScrollLock",
    "Pause",
    "Insert",
    "Home",
    "PageUp",
    "Delete",
    "End",
    "PageDown",
    "ArrowRight",
    "ArrowLeft",
    "ArrowDown",
    "ArrowUp",
    "NumLock",
  ];
  row70.forEach((k, idx) => {
    map[k] = 70 + idx;
  });
  const row84 = [
    "NumpadDivide",
    "NumpadMultiply",
    "NumpadSubtract",
    "NumpadAdd",
    "NumpadEnter",
    "Numpad1",
    "Numpad2",
    "Numpad3",
    "Numpad4",
    "Numpad5",
    "Numpad6",
    "Numpad7",
    "Numpad8",
    "Numpad9",
    "Numpad0",
    "NumpadDecimal",
  ];
  row84.forEach((k, idx) => {
    map[k] = 84 + idx;
  });
  map.IntlBackslash = 100;
  map.ContextMenu = 101;
  for (let i = 13; i <= 24; i++) map[`F${i}`] = 91 + i;
  const mods = [
    "ControlLeft",
    "ShiftLeft",
    "AltLeft",
    "MetaLeft",
    "ControlRight",
    "ShiftRight",
    "AltRight",
    "MetaRight",
  ];
  mods.forEach((k, idx) => {
    map[k] = 224 + idx;
  });
  return map;
})();

export function domCodeToHidUsage(code: string): number | null {
  const found = DOM_CODE_TO_HID[code];
  return typeof found === "number" ? found : null;
}
