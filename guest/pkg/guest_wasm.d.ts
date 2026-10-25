/* tslint:disable */
/* eslint-disable */
/**
 * The `ReadableStreamType` enum.
 *
 * *This API requires the following crate features to be activated: `ReadableStreamType`*
 */

export type ReadableStreamType = "bytes";

/**
 * A connection to a room's guest endpoint. Created by [`connect`].
 */
export class GuestConn {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Closes the connection with an application close (`code`, `reason`), what the peer sees
     * as the WebSocket close code (e.g. 1000). Resolves once the endpoint has shut down (up to
     * a few seconds on a bad network); safe to call more than once.
     */
    close(code: number, reason: string): Promise<any>;
    /**
     * Resolves to `{code: number, reason: string}` when the connection closes: the peer's
     * application close (e.g. 4410 "gone"), what `close()` sent, or 1006 with the error text
     * when it ended otherwise (timeout, reset, transport error). Can be awaited many times.
     */
    closed(): Promise<any>;
    /**
     * The next frame as `{tag: number, data: Uint8Array}`, or `undefined` once the stream or
     * the connection has ended (then `closed()` gives the code). One call at a time: a second
     * while one is pending rejects. Rejects on a malformed frame, and every later call too.
     */
    recv(): Promise<any>;
    /**
     * Queues one frame (`tag` 0 = text, 1 = binary) and resolves once it is written to the
     * stream (not when the peer has read it). Frames go out in call order. Rejects for a bad
     * tag, a payload over 8 MiB + 64 KiB (`frame::MAX_PAYLOAD`), or a closed connection.
     */
    send(tag: number, bytes: Uint8Array): Promise<any>;
}

export class IntoUnderlyingByteSource {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    cancel(): void;
    pull(controller: ReadableByteStreamController): Promise<any>;
    start(controller: ReadableByteStreamController): void;
    readonly autoAllocateChunkSize: number;
    readonly type: ReadableStreamType;
}

export class IntoUnderlyingSink {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    abort(reason: any): Promise<any>;
    close(): Promise<any>;
    write(chunk: any): Promise<any>;
}

export class IntoUnderlyingSource {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    cancel(): void;
    pull(controller: ReadableStreamDefaultController): Promise<any>;
}

/**
 * Connects to `endpoint_id_hex` (the room endpoint's id, hex or base32) through the one relay
 * `relay_url` (`https://host[:port]`, or `http://` for a loopback dev relay) with ALPN `alpn`
 * (`mnemosyne/guest/1`), and opens the bi stream. The stream reaches the owner's side only with
 * the first frame sent, so send the join right away.
 */
export function connect(endpoint_id_hex: string, relay_url: string, alpn: string): Promise<GuestConn>;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_guestconn_free: (a: number, b: number) => void;
    readonly __wbg_intounderlyingbytesource_free: (a: number, b: number) => void;
    readonly __wbg_intounderlyingsink_free: (a: number, b: number) => void;
    readonly __wbg_intounderlyingsource_free: (a: number, b: number) => void;
    readonly connect: (a: number, b: number, c: number, d: number, e: number, f: number) => number;
    readonly guestconn_close: (a: number, b: number, c: number, d: number) => number;
    readonly guestconn_closed: (a: number) => number;
    readonly guestconn_recv: (a: number) => number;
    readonly guestconn_send: (a: number, b: number, c: number, d: number) => number;
    readonly intounderlyingbytesource_autoAllocateChunkSize: (a: number) => number;
    readonly intounderlyingbytesource_cancel: (a: number) => void;
    readonly intounderlyingbytesource_pull: (a: number, b: number) => number;
    readonly intounderlyingbytesource_start: (a: number, b: number) => void;
    readonly intounderlyingbytesource_type: (a: number) => number;
    readonly intounderlyingsink_abort: (a: number, b: number) => number;
    readonly intounderlyingsink_close: (a: number) => number;
    readonly intounderlyingsink_write: (a: number, b: number) => number;
    readonly intounderlyingsource_cancel: (a: number) => void;
    readonly intounderlyingsource_pull: (a: number, b: number) => number;
    readonly ring_core_0_17_14__bn_mul_mont: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly __wasm_bindgen_func_elem_12752: (a: number, b: number, c: number, d: number) => void;
    readonly __wasm_bindgen_func_elem_12764: (a: number, b: number, c: number, d: number) => void;
    readonly __wasm_bindgen_func_elem_1331: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_4636: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_5759: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_12619: (a: number, b: number) => void;
    readonly __wasm_bindgen_func_elem_4414: (a: number, b: number) => void;
    readonly __wasm_bindgen_func_elem_4976: (a: number, b: number) => void;
    readonly __wasm_bindgen_func_elem_5021: (a: number, b: number) => void;
    readonly __wbindgen_export: (a: number, b: number) => number;
    readonly __wbindgen_export2: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_export3: (a: number) => void;
    readonly __wbindgen_export4: (a: number, b: number) => void;
    readonly __wbindgen_add_to_stack_pointer: (a: number) => number;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
