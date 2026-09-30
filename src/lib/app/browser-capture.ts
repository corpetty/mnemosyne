/**
 * Recording in the browser, for a firm's server where the advisor's computer runs nothing but a
 * browser (backend api/routes/record.py). The microphone and, when shared, the call's audio
 * (screen-share audio: "Share system audio" on Windows, a tab's audio anywhere) are tapped by an
 * AudioWorklet into 16-bit mono PCM and sent as they are recorded, 100 ms at a time, one
 * WebSocket per source. A dropped connection reconnects and sends what it held back.
 */
import { connectionState } from '$lib/stores/connection.svelte.js';

export type BrowserSource = 'mic' | 'system';

// Runs on the audio thread: mixes to mono, converts to int16, posts 100 ms chunks.
const WORKLET = `
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = Math.round(sampleRate / 10);
    this.buf = new Int16Array(this.size);
    this.n = 0;
    this.port.onmessage = (e) => {
      if (e.data === 'flush' && this.n) {
        this.port.postMessage(this.buf.slice(0, this.n).buffer);
        this.n = 0;
      }
    };
  }
  process(inputs) {
    const chans = inputs[0];
    if (chans && chans.length) {
      const a = chans[0], b = chans[1];
      for (let i = 0; i < a.length; i++) {
        let v = b ? (a[i] + b[i]) / 2 : a[i];
        v = v < -1 ? -1 : v > 1 ? 1 : v;
        this.buf[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.n === this.size) {
          this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
          this.buf = new Int16Array(this.size);
          this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-tap', PcmTap);
`;

const MAX_HELD = 600; // chunks (60 s) kept while the connection is down; older ones are dropped

class SourceSocket {
  private ws: WebSocket | null = null;
  private held: ArrayBuffer[] = [];
  private finished = false;
  private delay = 500;
  /** The server has no such recording any more (stopped elsewhere) or another tab took over. */
  gone = false;

  constructor(
    private url: string,
    private onGone: (why: string) => void
  ) {
    this.open();
  }

  private open() {
    const ws = new WebSocket(this.url);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      this.delay = 500;
      while (this.held.length && ws.readyState === WebSocket.OPEN) ws.send(this.held.shift()!);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.finished) return;
      if (e.code === 4404 || e.code === 4403 || e.code === 4409 || e.code === 1000) {
        this.gone = true;
        this.onGone(e.code === 4409 ? 'Recording continued in another window' : 'The recording ended');
        return;
      }
      setTimeout(() => !this.finished && this.open(), this.delay);
      this.delay = Math.min(this.delay * 2, 8000);
    };
    this.ws = ws;
  }

  send(chunk: ArrayBuffer) {
    if (this.gone) return;
    const ws = this.ws;
    if (ws && ws.readyState === WebSocket.OPEN && !this.held.length) ws.send(chunk);
    else {
      this.held.push(chunk);
      if (this.held.length > MAX_HELD) this.held.shift();
    }
  }

  /** Send what is left and close; waits (up to 5 s) for it to leave. */
  async finish() {
    this.finished = true;
    const ws = this.ws;
    if (!ws) return;
    if (ws.readyState === WebSocket.CONNECTING) {
      await new Promise((r) => {
        ws.addEventListener('open', r, { once: true });
        ws.addEventListener('close', r, { once: true });
        setTimeout(r, 3000);
      });
    }
    if (ws.readyState === WebSocket.OPEN) {
      while (this.held.length) ws.send(this.held.shift()!);
      const until = Date.now() + 5000;
      while (ws.bufferedAmount > 0 && Date.now() < until) await new Promise((r) => setTimeout(r, 50));
      ws.close(1000);
    }
  }
}

interface Tap {
  source: BrowserSource;
  stream: MediaStream;
  node: AudioWorkletNode | null;
  socket: SourceSocket | null;
}

export interface Acquired {
  sources: BrowserSource[];
  labels: Record<string, string>;
  sampleRate: number;
}

export class BrowserCapture {
  private ctx: AudioContext | null = null;
  private taps: Tap[] = [];
  /** Something went wrong mid-recording (sharing stopped, recording ended elsewhere). */
  onProblem: (message: string) => void = () => {};

  get active() {
    return this.taps.length > 0;
  }

  /** Ask for the microphone and, with `call`, for the call's audio. Must run straight from the
   * click that starts recording: browsers only open the share dialog after a click. */
  async acquire(opts: { mic: boolean; micId: string | null; call: boolean }): Promise<Acquired> {
    await this.release();
    const media = navigator.mediaDevices;
    if (!media?.getUserMedia) throw new Error('This browser cannot record here (it needs an https:// address)');
    const taps: Tap[] = [];
    const labels: Record<string, string> = {};
    try {
      if (opts.mic) {
        const stream = await media.getUserMedia({
          audio: opts.micId ? { deviceId: { exact: opts.micId } } : true
        });
        labels.mic = stream.getAudioTracks()[0]?.label || 'Microphone';
        taps.push({ source: 'mic', stream, node: null, socket: null });
      }
      if (opts.call) {
        const stream = await media.getDisplayMedia({
          video: true, // required to be asked; stopped at once below
          audio: { suppressLocalAudioPlayback: false } as MediaTrackConstraints,
          // @ts-expect-error: Chrome's options for sharing the whole system's audio
          systemAudio: 'include',
          selfBrowserSurface: 'exclude'
        });
        for (const t of stream.getVideoTracks()) t.stop();
        const audio = stream.getAudioTracks()[0];
        if (!audio) {
          for (const t of stream.getTracks()) t.stop();
          throw new Error(
            'No call audio was shared. On Windows choose "Entire screen" and tick "Share system audio"; for a call in a browser tab, choose that tab and tick "Share tab audio".'
          );
        }
        labels.system = 'Call audio';
        taps.push({ source: 'system', stream, node: null, socket: null });
      }
    } catch (e) {
      for (const tap of taps) for (const t of tap.stream.getTracks()) t.stop();
      if (e instanceof DOMException && e.name === 'NotAllowedError') {
        throw new Error(opts.call && taps.length ? 'Sharing the call audio was cancelled' : 'The browser was not allowed to use the microphone');
      }
      throw e;
    }
    if (!taps.length) throw new Error('Choose the microphone, the call audio, or both');
    this.ctx = new AudioContext();
    const blob = new Blob([WORKLET], { type: 'text/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await this.ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    this.taps = taps;
    return { sources: taps.map((t) => t.source), labels, sampleRate: this.ctx.sampleRate };
  }

  /** Start sending to the backend's recording `recordingId`. */
  start(recordingId: string) {
    const ctx = this.ctx!;
    const base = connectionState.url.replace(/^http/, 'ws');
    const token = connectionState.token ? `?token=${encodeURIComponent(connectionState.token)}` : '';
    // A silent sink keeps the audio graph running without playing anything back.
    const sink = ctx.createGain();
    sink.gain.value = 0;
    sink.connect(ctx.destination);
    for (const tap of this.taps) {
      const socket = new SourceSocket(`${base}/api/record/${recordingId}/${tap.source}${token}`, (why) =>
        this.onProblem(why)
      );
      const node = new AudioWorkletNode(ctx, 'pcm-tap');
      node.port.onmessage = (e) => socket.send(e.data as ArrayBuffer);
      ctx.createMediaStreamSource(tap.stream).connect(node);
      node.connect(sink);
      tap.node = node;
      tap.socket = socket;
      tap.stream.getAudioTracks()[0]?.addEventListener('ended', () => {
        if (this.taps.includes(tap)) {
          this.onProblem(tap.source === 'system' ? 'The call audio is no longer shared' : 'The microphone stopped');
        }
      });
    }
    void ctx.resume();
  }

  /** Stop recording here: send the last audio, close the connections, free the devices. */
  async stop() {
    const taps = this.taps;
    for (const tap of taps) tap.node?.port.postMessage('flush');
    await new Promise((r) => setTimeout(r, 150)); // the flushed chunks arrive
    for (const tap of taps) tap.node?.port.close();
    await Promise.all(taps.map((t) => t.socket?.finish()));
    await this.release();
  }

  private async release() {
    for (const tap of this.taps) for (const t of tap.stream.getTracks()) t.stop();
    this.taps = [];
    const ctx = this.ctx;
    this.ctx = null;
    await ctx?.close().catch(() => {});
  }
}

export const browserCapture = new BrowserCapture();

/** The microphones this browser knows (names only after the microphone was allowed once). */
export async function listMicrophones(): Promise<{ id: string; label: string }[]> {
  try {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all
      .filter((d) => d.kind === 'audioinput' && d.deviceId)
      .map((d, i) => ({ id: d.deviceId, label: d.label || `Microphone ${i + 1}` }));
  } catch {
    return [];
  }
}
