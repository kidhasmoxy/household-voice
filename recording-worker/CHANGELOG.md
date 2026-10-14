# 0.0.3 — unpublished candidate

- Protocol v2 adds successful-disposal rotation and unload/backup unavailability events, each bound to the current connection.
- Reconnects only the control plane with fresh authentication/registration/claims and capped backoff. Captures, PCM, grants and operations are never retried or carried across epochs.
- Keeps replay tombstones until disconnect while pruning completed capture tasks. Backend rotation before the hard 128-operation cap permits indefinite use with bounded bookkeeping.
- Authentication/admin/proxy denial and malformed/protocol registration failures stop; only named availability errors and transient transport loss retry.
- Adds 18 control tests with real loopback PCM sockets, 140 successful operations, reload/handshake loss, cancellation, unload and replay refusal.
- Preserves historical 0.0.2 image evidence. Remains disabled, manual-boot, unpublished and uninstalled.
- Beta app metadata advertises only the tested amd64 architecture; aarch64 awaits its own build/runtime proof.
- Documents the staged canonical opt-in backend and adds local Dockerfile/build-only CI validation without publication or deployment. Per-directory Git attributes preserve reviewed image-source bytes.

# 0.0.2 — unpublished candidate

Bound WebSocket connection setup, complete authentication/admin checks, and control registration to five seconds each, with a two-second socket-close bound. Reject malformed registration results before accepting grants or creating capture clients; preserve caller cancellation and fixed metadata-only output without retry.

Added checksum-verified test-validator lookup for a public combined repository or portable export, an explicit public export recipe, and a Docker context allowlist. The hardened runtime is covered by 37 worker tests plus three portable-package checks. The previous 0.0.1 image receipt remains historical evidence.

# 0.0.1 — unpublished candidate

Added the disabled outbound capture worker, connection-bound grant protocol and original PCM transport. Reuses the integration answer validator. Includes actual WebSocket tests and stock-Core synthetic proof; production backend and Supervisor app-token acceptance remain pending.

Pinned the public multiarch Python base, included the MIT license, and made application ownership explicit. The local-only amd64 image passed hash/layer/allowlist checks and 25 loopback tests through its isolated filesystem as UID10001 with all capabilities dropped. Supervisor installation and real microphone/audio acceptance remain pending.
