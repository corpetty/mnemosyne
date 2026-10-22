/* tslint:disable */
/* eslint-disable */

/**
 * A causal insertion point, returned by `WasmHashSeq.cursorAt`. `op` is the
 * string `"after"` or `"before"` — telling the caller which `WasmRun`
 * constructor to use.
 */
export class WasmCursor {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    readonly anchor: string;
    readonly extraDeps: string[];
    readonly op: string;
}

export class WasmHashSeq {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Apply a single encoded op (`EncodableOp`) from a peer.
     */
    applyOp(bytes: Uint8Array): void;
    /**
     * Apply an in-flight run to the sequence.
     */
    applyRun(run: WasmRun): void;
    /**
     * Build a `WasmCursor` for inserting at position `idx`, or undefined when
     * `idx` is out of bounds. In an empty document the cursor is
     * After(origin), so it is always run-buildable.
     */
    cursorAt(idx: number): WasmCursor | undefined;
    encode(): Uint8Array;
    /**
     * Hex ID of the node at visible position `idx`, or undefined.
     */
    idAt(idx: number): string | undefined;
    insert(idx: number, text: string): void;
    isEmpty(): boolean;
    len(): number;
    mergeEncoded(bytes: Uint8Array): void;
    constructor();
    /**
     * Current visible position of the node `id_hex`, or undefined if absent/removed.
     */
    positionOf(id_hex: string): number | undefined;
    /**
     * Remove `n` chars at visible position `idx` and return the resulting op for
     * broadcast, or undefined if nothing was removed.
     */
    removeAndEncodeOp(idx: number, n: number): Uint8Array | undefined;
    remove(idx: number, len: number): void;
    /**
     * Dump the structure as JSON for visualization, at run/node granularity.
     *
     * Each "box" is a root char, an After-run, or a Before-run (a burst whose
     * first char is constrained before its anchor). Every non-root box resolves
     * to one parent box via its anchor — which may sit mid-box, since befores can
     * anchor at interior run elements — with a `rel` of `"after"` (right child)
     * or `"before"` (left child) — i.e. the generalized binary tree of FIG. 01.
     *
     * Shape: `{ "tips": [hex...], "nodes": [
     *   { "id": hex, "kind": "root"|"run"|"before", "text": str,
     *     "parent": hex|null, "parentOffset": int|null,
     *     "rel": "after"|"before"|null, "removed": bool,
     *     "removedMask": str,
     *     "deps": [{ "box": hex, "off": int }] } ] }`
     *
     * `deps` are extra causal dependencies beyond the anchor, each resolved to
     * the box it lands in and the element offset within it (so a mid-run insert
     * depending on the run's tip points at that tip char, not just the box).
     *
     * `removed` is true only when *every* element of the run is tombstoned (so
     * the box reads as fully deleted); `removedMask` is one `'0'`/`'1'` per
     * character (per run element) giving the per-char tombstone state, so a run
     * with only some chars deleted can be rendered char-by-char.
     *
     * `parentOffset` is the anchor's element index within the parent box: a
     * `rel:"before"` child sits immediately before that char, a `rel:"after"`
     * child immediately after it.
     */
    structureJson(): string;
    text(): string;
    /**
     * The current causal tips (heads of the DAG), as hex IDs.
     */
    tips(): string[];
}

export class WasmHashWeb {
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Apply a peer's 0xDE delta message. Idempotent; unknown objects are
     * opened from the frame's (kind, origin). The upstream clock is not
     * touched: it changes only when the upstream states it (a frontier
     * frame, a snapshot) or when we send. Returns the number of nodes
     * NEWLY delivered — 0 for an echo of our own ops or a replay, so
     * the caller can skip re-rendering when nothing changed.
     */
    applyDelta(bytes: Uint8Array): number;
    /**
     * The upstream's 0xC1 frontier frame becomes its clock; the next
     * `takeDeltas` is then the exact catch-up delta. Returns the number
     * of objects named.
     */
    applyFrontier(bytes: Uint8Array): number;
    /**
     * Deliver a remote enveloped op: `obj_id ‖ encoded node`.
     */
    applyTo(obj_hex: string, op_bytes: Uint8Array): void;
    /**
     * Build a 0xAF artifact frame (tag ‖ canonical artifact bytes) for a
     * value id this replica holds bytes for — the once-per-upload push.
     */
    artifactFrame(id_hex: string): Uint8Array;
    /**
     * Open (idempotently) the kv at `origin`; returns the object id.
     */
    createKv(origin_hex: string): string;
    /**
     * Open (idempotently) the seq at `origin`; returns the object id.
     */
    createSeq(origin_hex: string): string;
    static decode(bytes: Uint8Array): WasmHashWeb;
    del(obj_hex: string, key: string): void;
    /**
     * Our frontier as one 0xC1 frame: per object the tips we hold. A
     * peer sets our clock from it and answers with a delta instead of a
     * snapshot (the hello / reconnect path).
     */
    encodeFrontier(): Uint8Array;
    /**
     * The ops-only wire snapshot (empty artifact section): what clients
     * send and receive on hello/resync — artifact bytes ride 0xAF pushes
     * or lazy content-addressed fetches instead.
     */
    encodeOps(): Uint8Array;
    /**
     * Canonical whole-store snapshot.
     */
    encode(): Uint8Array;
    /**
     * Would these artifact bytes be news to this replica? (False for our
     * own 0xAF push echoed back by the relay — the caller skips the
     * re-render.) Never news for an identity-form artifact: its id
     * already carries the bytes.
     */
    hasArtifactBytes(bytes: Uint8Array): boolean;
    /**
     * The live put-op ids for a key (hex, id-ordered) — the anchoring
     * input for op-id-welded composition.
     */
    headIds(obj_hex: string, key: string): string[];
    isKv(obj_hex: string): boolean;
    isSeq(obj_hex: string): boolean;
    /**
     * Live string keys of a kv, as a JSON array (id-ordered; display
     * ordering is the app's concern). Keys whose artifact bytes are
     * unknown are skipped.
     */
    keys(obj_hex: string): string;
    /**
     * `object_id(KIND_KV ‖ origin)` as hex.
     */
    static kvId(origin_hex: string): string;
    /**
     * Every kv object as JSON `[{"obj":hex,"origin":hex}]`, obj-sorted.
     * The app's orphan scan: pages are kvs with a title key — enumerate,
     * check placement, surface the unplaced (PLACEMENT_SPEC.md open
     * thread 4's poor-man's D4 surface).
     */
    kvObjects(): string;
    /**
     * Like `markRange`, but with a CLOSED end: the end anchor is
     * `After(last covered element)`, so text typed immediately after the
     * region deterministically lands OUTSIDE it. This is what typed-markup
     * conversions want (`Before(next)` ends would race the tombstoned
     * delimiter's ghost in sibling id order — nondeterministic growth).
     */
    markRangeClosed(obj_hex: string, start: number, end: number, kind: string, value: string): string;
    /**
     * Mark visible range `[start, end)` with `kind`/`value` (both strings;
     * the artifacts register store-wide). Expanding-end anchors: typing at
     * the edges grows the region (MARKS.md anchor table). Returns the mark
     * op id.
     */
    markRange(obj_hex: string, start: number, end: number, kind: string, value: string): string;
    /**
     * The rendered document as coalesced marked spans. JSON:
     * `[{"text": s, "marks": [{"kind": s, "values": [s...]}]}]` — marks
     * whose live values are all unmark tombstones are omitted; multiple
     * values on one kind = an MVR conflict, surfaced whole.
     */
    markedSpans(obj_hex: string): string;
    /**
     * Merge a peer snapshot: union of knowledge. The snapshot is also
     * the sender's clock, so the upstream clock becomes it: the next
     * `takeDeltas` is exactly what we hold that the sender lacks.
     */
    mergeEncoded(bytes: Uint8Array): void;
    constructor();
    objectCount(): number;
    /**
     * Envelopes orphaned on unknown object ids (waiting for an open).
     */
    orphanCount(): number;
    /**
     * The payload id (hex) of the atom at visible position `idx`, or
     * undefined for plain characters / out of range.
     */
    payloadAt(obj_hex: string, idx: number): string | undefined;
    /**
     * Insert an atom at visible position `idx` whose payload is a raw id
     * (an embed: another object's origin, by app convention). Renders as
     * U+FFFC in `text()`; read back with `payloadAt`.
     * Author a `Place` op on any object (seq or kv): claim the link atom
     * `placed_at_hex` (an element id in some container) as this object's
     * containment placement, superseding the placement heads this replica
     * sees. Pass the tombstone id (`tombstoneId()`) to detach/delete.
     * Returns the place op's id. PLACEMENT_SPEC.md.
     */
    placeAt(obj_hex: string, placed_at_hex: string): string;
    /**
     * The containment register read (PLACEMENT_SPEC.md): JSON
     * `{"empty":bool,"conflicted":bool,"heads":[hex],"chain":[hex]}`.
     * `empty` = no Place op ever (the legacy presence rule applies);
     * `chain` = placed_at values in descending agreement order — the read
     * layer tries entries in order, skipping dead/unresolvable atoms;
     * a tombstone entry means detached-by-intent.
     */
    placementOf(obj_hex: string): string;
    /**
     * Store raw artifact bytes (the 0xAF frame payload); returns the
     * content-addressed value id.
     */
    provideArtifactBytes(bytes: Uint8Array): string;
    /**
     * Register raw bytes as a content-addressed value artifact; returns
     * its value id (hex). Identical bytes dedupe to the same id.
     */
    provideBytes(bytes: Uint8Array): string;
    /**
     * `obj[key] = <raw id>` — a link / origin reference; the value is an id,
     * not an artifact. Returns the put op id (hex).
     */
    putRef(obj_hex: string, key: string, ref_hex: string): string;
    /**
     * `obj[key] = value` (both strings). Returns the put op id (hex) — the
     * composition convention's input when a child anchors at this op.
     */
    putString(obj_hex: string, key: string, value: string): string;
    /**
     * Read a key. JSON:
     * `{"kind":"absent"}` |
     * `{"kind":"one","values":[v]}` |
     * `{"kind":"conflict","values":[v...]}` — every head surfaced (MVR).
     * Each v: `{"type":"string","value":s}` | `{"type":"ref","id":hex}` |
     * `{"type":"pending","id":hex}` (artifact bytes not yet known) |
     * `{"type":"deleted"}`.
     */
    readKey(obj_hex: string, key: string): string;
    /**
     * The bytes of a `Bytes` artifact, or undefined (unknown id, or a
     * different value kind).
     */
    resolveBytes(id_hex: string): Uint8Array | undefined;
    /**
     * Element id (hex) at visible position `idx` of a seq object.
     */
    seqIdAt(obj_hex: string, idx: number): string | undefined;
    /**
     * `object_id(KIND_SEQ ‖ origin)` as hex.
     */
    static seqId(origin_hex: string): string;
    seqInsertRef(obj_hex: string, idx: number, ref_hex: string): string;
    /**
     * Move the element at visible `from` to sit at visible slot
     * `to_slot` (0..=len, interpreted on the current rendering): a Move
     * op superseding the placement heads this replica sees.
     */
    seqMove(obj_hex: string, from: number, to_slot: number): void;
    /**
     * Current visible position of element `id` in a seq object, or
     * undefined if absent/removed. Ids are stable under concurrent
     * edits — this is the anchor for caret/selection preservation.
     */
    seqPositionOf(obj_hex: string, id_hex: string): number | undefined;
    /**
     * The delta for the upstream — everything we hold outside its
     * causal closure — as one 0xDE delta message; empty when there is
     * nothing (APP_NOTES #8). Our own clock then becomes the upstream's
     * ("last sent").
     */
    takeDeltas(): Uint8Array;
    /**
     * Drain the ids (hex) of small value artifacts minted locally since
     * the last drain — the strings behind new titles, mark kinds/values,
     * languages. Push each with `artifactFrame` alongside the delta: ops
     * alone leave a live peer holding unresolved refs until resync.
     */
    takeNewArtifacts(): string[];
    textInsert(obj_hex: string, idx: number, text: string): void;
    textLen(obj_hex: string): number;
    textRemove(obj_hex: string, idx: number, len: number): void;
    text(obj_hex: string): string;
    /**
     * The tombstone value id (a derived constant): the `placed_at` for
     * detach, the mark value for unmark.
     */
    static tombstoneId(): string;
    /**
     * Remove `kind` formatting over `[start, end)` — a tombstone-valued
     * mark (partial unmark works: the overwritten mark keeps applying
     * outside the range).
     */
    unmarkRange(obj_hex: string, start: number, end: number, kind: string): string;
}

/**
 * An in-flight Run: a typing burst coalesced into a single op.
 *
 * Build one from a cursor (`newAfter`/`newBefore`), `extend` it as the user
 * types, then `seq.applyRun(run)`. Broadcast `run.encodeOp()` to peers.
 */
export class WasmRun {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    /**
     * Serialize this run as an `EncodableOp::Run` for broadcast to peers.
     */
    encodeOp(): Uint8Array;
    /**
     * Append a character to the run (continuation of the typing burst).
     */
    extend(ch: string): void;
    isEmpty(): boolean;
    len(): number;
    /**
     * Build an InsertAfter-rooted Run (first char lands immediately after `anchor`).
     *
     * `extraDeps` are the run's pins beyond the anchor. The anchor is a
     * ref already, so an entry naming it is dropped here: nodes are
     * stored normalized (`pins = refs ∖ named`) and a run built with the
     * anchor pinned would apply locally yet be rejected by every peer
     * (`RedundantDep`) — silent divergence.
     */
    static newAfter(anchor_hex: string, pins_hex: string[], first: string): WasmRun;
    /**
     * Build an InsertBefore-rooted Run (first char is constrained to land
     * immediately before `anchor`). `extraDeps` as for `newAfter`: the
     * anchor is stripped if named.
     */
    static newBefore(anchor_hex: string, pins_hex: string[], first: string): WasmRun;
    text(): string;
}

/**
 * Markdown to the client's `NotesModel` (JSON; see `notes_core::client`): the text with
 * U+001F between table cells, a kind per line, inline ranges in code points. Never fails:
 * anything is markdown.
 */
export function parseMarkdown(markdown: string): string;

/**
 * A `NotesModel` (JSON) to canonical markdown, the same rendering the server's fold gives.
 * Throws a message when the JSON is not a model.
 */
export function renderMarkdown(model_json: string): string;

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_wasmcursor_free: (a: number, b: number) => void;
    readonly __wbg_wasmhashseq_free: (a: number, b: number) => void;
    readonly __wbg_wasmhashweb_free: (a: number, b: number) => void;
    readonly __wbg_wasmrun_free: (a: number, b: number) => void;
    readonly parseMarkdown: (a: number, b: number, c: number) => void;
    readonly renderMarkdown: (a: number, b: number, c: number) => void;
    readonly wasmcursor_anchor: (a: number, b: number) => void;
    readonly wasmcursor_extraDeps: (a: number, b: number) => void;
    readonly wasmcursor_op: (a: number, b: number) => void;
    readonly wasmhashseq_applyOp: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashseq_applyRun: (a: number, b: number) => void;
    readonly wasmhashseq_cursorAt: (a: number, b: number) => number;
    readonly wasmhashseq_encode: (a: number, b: number) => void;
    readonly wasmhashseq_idAt: (a: number, b: number, c: number) => void;
    readonly wasmhashseq_insert: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashseq_isEmpty: (a: number) => number;
    readonly wasmhashseq_len: (a: number) => number;
    readonly wasmhashseq_mergeEncoded: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashseq_new: () => number;
    readonly wasmhashseq_positionOf: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashseq_remove: (a: number, b: number, c: number) => void;
    readonly wasmhashseq_removeAndEncodeOp: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashseq_structureJson: (a: number, b: number) => void;
    readonly wasmhashseq_text: (a: number, b: number) => void;
    readonly wasmhashseq_tips: (a: number, b: number) => void;
    readonly wasmhashweb_applyDelta: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_applyFrontier: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_applyTo: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_artifactFrame: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_createKv: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_createSeq: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_decode: (a: number, b: number, c: number) => void;
    readonly wasmhashweb_del: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_encode: (a: number, b: number) => void;
    readonly wasmhashweb_encodeFrontier: (a: number, b: number) => void;
    readonly wasmhashweb_encodeOps: (a: number, b: number) => void;
    readonly wasmhashweb_hasArtifactBytes: (a: number, b: number, c: number) => number;
    readonly wasmhashweb_headIds: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_isKv: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_isSeq: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_keys: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_kvId: (a: number, b: number, c: number) => void;
    readonly wasmhashweb_kvObjects: (a: number, b: number) => void;
    readonly wasmhashweb_markRange: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number, j: number) => void;
    readonly wasmhashweb_markRangeClosed: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number, i: number, j: number) => void;
    readonly wasmhashweb_markedSpans: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_mergeEncoded: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_new: () => number;
    readonly wasmhashweb_objectCount: (a: number) => number;
    readonly wasmhashweb_orphanCount: (a: number) => number;
    readonly wasmhashweb_payloadAt: (a: number, b: number, c: number, d: number, e: number) => void;
    readonly wasmhashweb_placeAt: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_placementOf: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_provideArtifactBytes: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_provideBytes: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_putRef: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number) => void;
    readonly wasmhashweb_putString: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number) => void;
    readonly wasmhashweb_readKey: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_resolveBytes: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_seqId: (a: number, b: number, c: number) => void;
    readonly wasmhashweb_seqIdAt: (a: number, b: number, c: number, d: number, e: number) => void;
    readonly wasmhashweb_seqInsertRef: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => void;
    readonly wasmhashweb_seqMove: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_seqPositionOf: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_takeDeltas: (a: number, b: number) => void;
    readonly wasmhashweb_takeNewArtifacts: (a: number, b: number) => void;
    readonly wasmhashweb_text: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_textInsert: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => void;
    readonly wasmhashweb_textLen: (a: number, b: number, c: number, d: number) => void;
    readonly wasmhashweb_textRemove: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly wasmhashweb_tombstoneId: (a: number) => void;
    readonly wasmhashweb_unmarkRange: (a: number, b: number, c: number, d: number, e: number, f: number, g: number, h: number) => void;
    readonly wasmrun_encodeOp: (a: number, b: number) => void;
    readonly wasmrun_extend: (a: number, b: number, c: number, d: number) => void;
    readonly wasmrun_isEmpty: (a: number) => number;
    readonly wasmrun_len: (a: number) => number;
    readonly wasmrun_newAfter: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => void;
    readonly wasmrun_newBefore: (a: number, b: number, c: number, d: number, e: number, f: number, g: number) => void;
    readonly wasmrun_text: (a: number, b: number) => void;
    readonly __wbindgen_export: (a: number, b: number) => number;
    readonly __wbindgen_export2: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_add_to_stack_pointer: (a: number) => number;
    readonly __wbindgen_export3: (a: number, b: number, c: number) => void;
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
