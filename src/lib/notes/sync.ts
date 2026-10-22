// Frames between a WasmHashWeb and the relay (/ws/notes/{id}), pure: no sockets here.
//
// 0xDE frames are deltas (`takeDeltas`), 0xAF frames carry small value artifacts (mark kinds
// and values longer than 14 UTF-8 bytes: link URLs, comment ids): a peer that gets the delta
// without them holds unresolved ids until a resync, so every drain sends the artifacts first.
//
// QUEUE 39 (hashseq): the upstream clock only moves when the upstream states it or when we
// send, so a delta received from the relay would ship back on the next drain. The fix, per
// the phase 6 design: drain before applying a remote delta, apply it, then
// `applyFrontier(encodeFrontier())`, which says "the relay now holds everything we hold".
import { WasmHashWeb } from './pkg/notes_wasm.js';

export const DELTA_TAG = 0xde;
export const ARTIFACT_TAG = 0xaf;

/** The frames to send: new artifacts first, then the delta (if any). */
export function drain(web: WasmHashWeb): Uint8Array[] {
  const out: Uint8Array[] = [];
  for (const id of web.takeNewArtifacts()) out.push(web.artifactFrame(id));
  const delta = web.takeDeltas();
  if (delta.length) out.push(delta);
  return out;
}

export interface Applied {
  /** Frames drained before applying, to send to the relay. */
  send: Uint8Array[];
  /** Nodes newly delivered (0 for an echo or a replay) or 1 for new artifact bytes. */
  delivered: number;
}

/** Apply a frame from the relay. */
export function applyRemote(web: WasmHashWeb, frame: Uint8Array): Applied {
  if (frame.length === 0) return { send: [], delivered: 0 };
  if (frame[0] === ARTIFACT_TAG) {
    // hasArtifactBytes answers "already stored?" (its doc comment says the opposite).
    const bytes = frame.subarray(1);
    if (web.hasArtifactBytes(bytes)) return { send: [], delivered: 0 };
    web.provideArtifactBytes(bytes);
    return { send: [], delivered: 1 };
  }
  if (frame[0] !== DELTA_TAG) throw new Error(`unexpected notes frame tag 0x${frame[0].toString(16)}`);
  const send = drain(web);
  const delivered = web.applyDelta(frame);
  web.applyFrontier(web.encodeFrontier());
  return { send, delivered };
}

/**
 * Open a store from the relay's hello: the folded snapshot (empty = none) then the unfolded
 * frames in order. The relay holds all of it, so the upstream clock is set to our frontier
 * (a decoded snapshot would otherwise re-ship everything on the first drain).
 */
export function openFromHello(snapshot: Uint8Array | null | undefined, frames: Iterable<Uint8Array> = []): WasmHashWeb {
  const web = snapshot && snapshot.length ? WasmHashWeb.decode(snapshot) : new WasmHashWeb();
  web.applyFrontier(web.encodeFrontier());
  for (const frame of frames) {
    if (frame.length === 0) continue;
    if (frame[0] === ARTIFACT_TAG) web.provideArtifactBytes(frame.subarray(1));
    else if (frame[0] === DELTA_TAG) web.applyDelta(frame);
    else throw new Error(`unexpected notes frame tag 0x${frame[0].toString(16)}`);
  }
  web.applyFrontier(web.encodeFrontier());
  return web;
}
