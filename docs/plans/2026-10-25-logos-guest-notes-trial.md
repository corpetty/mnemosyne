# Trial: guest notes over Logos

Status: written trial, nothing run, nothing built. Date: 2026-10-25.
Follows "Phase 9: guest notes" in `2026-10-12-hashseq-collaboration.md`, which relays sealed notes
frames through a room on a publish host. This trial asks whether the Logos stack could be that relay
instead, so nobody has to run an internet-facing host. The sibling trial for publications is
`2026-10-23-logos-publishing-trial.md`; its findings about storage hold here too.

Source for every module name and call below: the Logos Module Atlas (plugin `0.1.202610171040`,
Basecamp `0.3.2`, catalog index 2026-10-17): `delivery_module` 0.3.2 at logos-delivery-module
`c25b786` and `storage_module` 3.0.2 at logos-storage-module `7548f41`, with the messaging and storage
stack docs. Anything not in a contract or stack doc is marked **unverified**.

## 1. What Logos offers that fits

**delivery_module 0.3.2** (C++, default catalog, no dependencies; optional `libp2p_module`,
`liblogos_rln_module`):

- `createNode(cfg)`, `start()`, `stop()`, `send(contentTopic, payload: bstr)` → request id,
  `subscribe(contentTopic)`, event `messageReceived(messageHash, contentTopic, payload, source,
  timestamp)` with `source` `live` or `history` (Store backfill at start).
- **Reliable channels**: `channelCreate(channelId, contentTopic, senderId)`, `channelSend`,
  `channelClose`, event `channelMessageReceived(channelId, senderId, payload, timestamp)`; SDS loops
  keep a channel's messages in order and fill gaps. That is the job our room's seq, log and `since`
  do today.
- **Store**: `storeQuery(jsonQuery, peerAddr, timeoutMs)` against a store service peer (kernel API,
  marked unstable); backfill on start is on by default (`backfillEnabled`).
- Modes `Core` (relay) and `Edge` (light node); NAT handled by the node (`nat: any`, upnp, pmp,
  extip); presets `logos.test` (cluster 2, RLN on, stable fleet), `logos.dev` (cluster 3, RLN off,
  "may break"), `twn` / `status.prod` accepted.
- Mix routing (`anonymityLevel` `Preferred`/`Required`) can hide who sends to whom.

Payloads are opaque bytes, so our sealed envelopes (`services/guest_format.py`) travel as they are:
the end-to-end encryption, the key in the link's fragment and the bridge's rules do not change.

**storage_module 3.0.2** (bundled in Basecamp): `uploadUrl`/`uploadInit…Finalize` → CID,
`downloadToUrl`, `exists`, `remove`, `block-ttl` 30 days. A fit for the sealed final snapshot of an
ended room (read-only until expiry), not for the live part.

## 2. What is missing for us

- **No browser entry.** Every module is a C++/Rust plugin inside a Logos Core host (Basecamp or
  logoscore). The atlas has no WebSocket, HTTP or wasm entry for Delivery or Storage (searches for
  "websocket" and "http" find only an EVM RPC proxy). A guest with only a link cannot join through
  the official modules; they would need Basecamp, which defeats guest notes.
- **RLN for every sender.** On `logos.test` every sender needs a funded, active RLN membership on the
  LEZ testnet (`liblogos_rln_module`, `liblogos_lez_rln_module`); without one a node starts and
  relays but its sends fail. Guests cannot be asked for that. `logos.dev` has RLN off but is
  documented as unstable.
- **No Python or HTTP way in.** The owner's side needs a headless Logos Core host as a sidecar,
  managed like `services/link.py` manages `mnemosyne-link`, or a small module of our own.
- **Unverified:** the network's message size cap (our frames can be up to 2 MiB, so chunking is
  likely), Store retention and who runs store peers for the test fleets, delivery latency for live
  typing, and whether the fleets accept browser connections at all.

## 3. The browser question, beyond the atlas

Logos Delivery is the Waku protocol family (relay, filter, lightpush, store; the `twn` and
`status.prod` preset names). Waku has a browser SDK (js-waku, `@waku/sdk`) whose light client talks to
service nodes over secure WebSockets. If the Logos fleets accept it, the guest page could subscribe
(filter) and publish (lightpush) on the meeting's content topic directly, and read history from Store,
with no host of ours. None of this is in the atlas: it is the trial's main question.

## 4. Manual trial

Needs Basecamp 0.3.2 with delivery_module on one machine (the owner), and a plain browser (the guest).
No Mnemosyne code changes.

1. **Owner node.** `createNode({"mode":"Core","preset":"logos.dev"})` then `start()`; note
   `getConnectionStatus()`. Repeat with `logos.test` and see whether `send` fails without an RLN
   membership. Teaches: which preset a product could use today.
2. **Topic and payload.** `subscribe("/mnemosyne/1/guest-<room>/proto")`; `send` a sealed NOTES
   envelope produced by `backend/scripts/make_guest_vectors.py` (fixed key, so the browser can check
   it). Try 1 KB, 64 KB, 256 KB and 2 MB payloads. Teaches: the size cap and whether chunking is
   needed.
3. **Browser guest with js-waku.** A scratch page (outside the repo) using `@waku/sdk` light node,
   pointed at the same cluster's bootstrap/service nodes: filter-subscribe the topic, decode the
   vector with guest/src/format.ts, lightpush a reply. Teaches: whether the fleets accept browser
   WebSocket connections, whether lightpush from a browser works without RLN, and the latency of
   one keystroke's frame.
4. **Reliable channel.** Replace send/subscribe with `channelCreate`/`channelSend` on the owner and
   watch `channelMessageReceived` on a second Basecamp; drop the network for a minute. Teaches:
   whether SDS gap-filling can replace our room log and `since`.
5. **Late joiner.** Start a second browser after twenty messages; read history through Store
   (`storeQuery` on the owner side; js-waku's store client in the browser). Teaches: whether Store
   can replace our host's snapshot and log, and how long it keeps messages.
6. **After the meeting.** Upload the sealed final snapshot with `uploadUrl`; fetch it on a second
   Basecamp with `downloadToUrl`; then `remove`. Teaches: whether Storage can carry the read-only
   period (for Basecamp readers only; browsers still cannot fetch a CID).

## 5. Making it a product feature

If steps 3 and 5 work: (a) a headless Logos Core sidecar with delivery_module on the owner's machine,
driven by the bridge as a third transport next to the publish host room and iroh (phase 10); (b) the
guest page gains a js-waku transport (topic and cluster in the link, never the key), behind the same
`GuestDoc` interface; (c) chunking for large frames; (d) Store or a final snapshot for late joiners.
If step 3 fails, Logos stays a power-user option (guests in Basecamp) until a browser entry exists.

Questions for David:

1. Do the `logos.test` / `logos.dev` fleets accept browser clients (js-waku over secure WebSockets)?
   Is a browser entry planned for Logos Delivery?
2. How are RLN memberships meant to work for one-off web users: can a node with a membership relay
   their messages (lightpush), or is there a free tier for light clients?
3. What is the message size cap on the fleets, and the recommended way to send larger payloads?
4. Who runs Store peers for the test fleets, how long do they keep messages, and is `storeQuery`
   (marked unstable) the intended API?
5. Is there a supported headless way to run delivery_module outside Basecamp (logoscore CLI, a
   local socket)?
6. Are reliable channels (SDS) meant for many writers on one topic, as a shared notes room has?

## 6. Results

Not run yet.
