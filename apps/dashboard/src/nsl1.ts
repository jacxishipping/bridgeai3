/**
 * Minimal browser codec for vercel-labs/screen-relay (`nsl1`) binary framing
 * and 16-byte big-endian input records, paired with WebCodecs VideoDecoder.
 */

export const NSL1_HEADER_SIZE = 20;
export const NSL1_VIDEO_META_SIZE = 12;
export const NSL1_INPUT_RECORD_SIZE = 16;

export const NSL1_MSG_VIDEO = 1;
export const NSL1_MSG_CURSOR = 2;
export const NSL1_MSG_ACK = 3;
export const NSL1_MSG_INPUT = 4;
export const NSL1_MSG_CONTROL = 5;

export const NSL1_FLAG_KEYFRAME = 0x0001;

export const INPUT_TYPE_POINTER_MOVE = 1;
export const INPUT_TYPE_POINTER_BUTTON = 2;
export const INPUT_TYPE_WHEEL = 3;
export const INPUT_TYPE_KEY_DOWN = 4;
export const INPUT_TYPE_KEY_UP = 5;
export const INPUT_TYPE_UNICODE = 6;

export const BUTTON_CODE_LEFT = 1;
export const BUTTON_CODE_RIGHT = 2;
export const BUTTON_CODE_MIDDLE = 3;

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

export function webCodecsSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof (window as unknown as { VideoDecoder?: unknown }).VideoDecoder === "function" &&
    typeof (window as unknown as { EncodedVideoChunk?: unknown }).EncodedVideoChunk === "function"
  );
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
  view.setUint16(6, (opts?.flags ?? 0) & 0xffff, false);
  view.setUint32(8, (opts?.generation ?? 1) >>> 0, false);
  view.setUint32(12, (opts?.seq ?? 0) >>> 0, false);
  view.setUint32(16, payload.byteLength >>> 0, false);
  if (payload.byteLength > 0) {
    out.set(payload, NSL1_HEADER_SIZE);
  }
  return out;
}

export function encodeNsl1Ack(seq: number, generation = 1): Uint8Array {
  return encodeNsl1Frame(NSL1_MSG_ACK, new Uint8Array(0), { generation, seq });
}

function writeInputRecord(
  view: DataView,
  offset: number,
  type: number,
  code: number,
  value: number,
  xNorm: number,
  yNorm: number,
  seq: number,
): void {
  const clamp01 = (v: number) => Math.max(0, Math.min(1, v));
  view.setUint8(offset, type & 0xff);
  view.setUint8(offset + 1, 0);
  view.setUint16(offset + 2, code & 0xffff, false);
  view.setInt32(offset + 4, value | 0, false);
  view.setUint16(offset + 8, Math.round(clamp01(xNorm) * 65535), false);
  view.setUint16(offset + 10, Math.round(clamp01(yNorm) * 65535), false);
  view.setUint32(offset + 12, seq >>> 0, false);
}

export function encodeNsl1Click(
  xNorm: number,
  yNorm: number,
  button: "left" | "right" | "middle" = "left",
  seq = 0,
): Uint8Array {
  const btnCode =
    button === "right" ? BUTTON_CODE_RIGHT : button === "middle" ? BUTTON_CODE_MIDDLE : BUTTON_CODE_LEFT;
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE * 3);
  const view = new DataView(payload.buffer);
  writeInputRecord(view, 0, INPUT_TYPE_POINTER_MOVE, 0, 0, xNorm, yNorm, seq);
  writeInputRecord(view, 16, INPUT_TYPE_POINTER_BUTTON, btnCode, 1, xNorm, yNorm, seq);
  writeInputRecord(view, 32, INPUT_TYPE_POINTER_BUTTON, btnCode, 0, xNorm, yNorm, seq);
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq });
}

export function encodeNsl1Text(text: string, seq = 0): Uint8Array {
  const chars = Array.from(text).slice(0, 256);
  const payload = new Uint8Array(NSL1_INPUT_RECORD_SIZE * chars.length);
  const view = new DataView(payload.buffer);
  chars.forEach((ch, idx) => {
    const cp = ch.codePointAt(0) ?? 0;
    writeInputRecord(view, idx * NSL1_INPUT_RECORD_SIZE, INPUT_TYPE_UNICODE, 0, cp, 0, 0, seq + idx);
  });
  return encodeNsl1Frame(NSL1_MSG_INPUT, payload, { seq });
}

