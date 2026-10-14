# Household Voice recording worker — disabled candidate

This optional Home Assistant app transports original microphone PCM from Core's public `assist_pipeline/device/capture` WebSocket API to Household Voice. It has no conversation prompt, STT engine, scheduler, speaker player, announcement destination policy or persistent recording store. The integration retains the native question, source identity, transcript, validation, outbox and playback ownership.

**Not installed, enabled or published.** The canonical Household Voice candidate now wires this backend behind the default-false `native_recording_enabled` option. Isolated stock-Core tests exercise the staged integration lifecycle, native question, registered worker WebSocket, original recording store/outbox, cancellation, backup/resume and reload. The actual Supervisor app-token path remains unproven. An amd64 image was built without a registry push, verified by digest, and tested through its extracted filesystem as UID10001. This is not a Supervisor or containerd installation test. `config.yaml` uses manual boot and `enabled: false`. It exposes no inbound port and requests no host mounts or Supervisor management API permission.

Beta app metadata targets `amd64`, the architecture covered by the image/runtime evidence. Advertising `aarch64` requires a separate image build and runtime proof.

## Authorization evidence

Official app documentation supports `homeassistant_api: true`, the platform-provided `SUPERVISOR_TOKEN`, and `ws://supervisor/core/websocket`. No user token option or hidden HA user is created. [Official communication contract](https://developers.home-assistant.io/docs/apps/communication/).

The observed installed Supervisor version was 2026.09.3. Its proxy verifies the app token and Core API permission, then opens an authenticated connection to Core. Core 2026.9.4 provisions the Supervisor system user in the administrator group. A read-only check confirmed that system administrator exists and Supervisor is healthy; no app credential was retrieved, copied or printed. This establishes source support, not a live app-token acceptance test. [Pinned Supervisor proxy](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/api/proxy.py), [pinned Core connection](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/homeassistant/api.py), [Supervisor user authorization](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/hassio/__init__.py#L165-L194).

The worker checks `auth/current_user.is_admin` before capture. Core API access is broad administrator access: application-level grants limit this worker's implementation; they cannot reduce what a compromised administrator credential could do. Keep that credential only in the app environment and never log auth messages.

## Implemented pieces

- `protocol.py`: exact-schema, connection-bound, one-use grants; bounded tombstones; replay/sequence/deadline validation; explicit end acknowledgement and barrier; injection of the existing integration `RecordingSession` rather than a copied answer validator.
- `client.py`: real outbound Core WebSocket authentication and PCM subscription. Every forwarded frame waits for backend acknowledgement. It bounds chunk size, total PCM and frame count, rejects malformed format/overflow, and never retries a recording.
- `__main__.py`: disabled-by-default Supervisor app entry point. It connects to the staged administrator-only canonical backend endpoint, accepts up to four concurrent grants, prunes completed tasks while retaining replay tombstones, and establishes fresh control connections after transient loss. It never retries capture or transfers grants/PCM between connections. Its output is fixed status categories only.
- App metadata and a candidate Dockerfile. No recording writes, permanent media URLs, room defaults, private addresses or credentials are included.

## Required backend protocol

The backend commands are wired into the canonical candidate under `custom_components/household_voice/native_recording*.py` and `recording_protocol.py`. They register only after explicit integration opt-in with one embedded Household Voice authority, native Assist tools, live actions and announcements enabled. Existing household installations are unchanged by this package.

1. Worker authenticates through Supervisor, confirms administrator status, and subscribes with `household_voice/recording_worker/connect`, `protocol_version: 2`. The backend binds the returned random `connection_id` to that exact server-side connection object, not just a user ID or worker-supplied string. A second administrator socket cannot reuse its grants.
2. The integration calls native `assist_satellite.ask_question` using an owned HA Context. It observes the registered source's exact owned RESPONDING event after old-run cancellation. It then issues a grant on the subscribed worker connection. The worker does not call the question service.
3. Each grant names one operation, registered source and question Context, with a random one-use ID, short TTL and fixed Core timeout of 60 seconds. Backend monotonic time is authoritative. The integration's overall deadline must expire before Core's capture timeout, preventing a timeout-shaped `end` from being accepted as a complete recording.
4. The worker sends `household_voice/recording_worker/frame` with `{connection_id, grant_id, seq, kind, payload}`. Sequence starts at one and is contiguous. Kinds are ACK, audio, end and barrier. The backend validates each frame and returns exactly its accepted sequence before the worker forwards another. The barrier references the acknowledged end sequence.
5. The integration independently observes public source/recording events, enforces capture ACK before Listening, and obtains the result from its exact native question task. After the wire barrier **and its own real event-drain barrier**, it reads the validator revision and consumes once without yielding. The wire barrier alone does not prove event-bus completion.
6. Cancel is a backend event bound to the same grant/connection. Closing the capture WebSocket removes its subscription; it does not prove the microphone stopped. Disconnect, timeout, supersession or source changes wipe pending PCM and invalidate grants, while the backend retains any unresolved source hold. There is no automatic replay after reconnect.

Grant bookkeeping is bounded to four active PCM sessions and 128 total operations per connection. The backend preflights capacity before beginning a question and emits the exact event `{type: "rotate", connection_id: "..."}` at its 120-operation threshold only after successful consume, durable source release, removal of the operation, and zero remaining operations or unresolved source holds. The worker drains already acknowledged local captures and closes that control before fresh authentication/registration. The wire barrier ACK alone never triggers rotation. Tombstones disappear only with the old connection.

For unload/backup, the backend instead emits `{type: "unavailable", connection_id: "..."}`. The worker cancels active capture sockets, discards the epoch, then retries only fresh control with backoff. This event makes no microphone or source-release claim. `GrantBook.expire()` must be scheduled by the backend deadline timer; timeout wipes content without silently releasing ownership. Do not recover a canceled physical question by guessing from an IDLE state.

## Test evidence

Run from this workspace:

```text
python voice-replacement/ha-integration/tests/test_recording_session.py
python voice-replacement/recording-worker/tests/test_worker.py
```

The worker transport/startup/control suites have 55 tests, including actual loopback WebSockets and a held end ACK proving that a barrier cannot overtake unacknowledged data. A 140-session run rotates at 120, preserves original PCM/transcript, retains bounded replay tombstones and drains completed capture tasks. Further cases cover all handshake socket-loss phases, reload/busy availability errors, capped backoff, fatal authorization, cancellation, unload, and old/completed grant refusal. Startup tests cover stalled reads/writes and malformed acknowledgements without payload logging. Tests can use the public combined repository, an explicit `HOUSEHOLD_VOICE_RECORDING_VALIDATOR` development path, or a checksum-verified canonical fixture in the portable export. This is a test dependency; the runtime never imports it.

The opt-in actual-Core fixture runs in stock HA9.4 and10.1 containers with synthetic STT and hardware only. It uses genuine native question services, public owned-Context events, administrator authentication, Core capture, and a real HA backend WebSocket handler. A second administrator socket is rejected for using another connection's grant. Original PCM passes through the worker, ordered frame ACKs, end/barrier and the reused validator. This is stronger than a mocked capture response; it remains a fixture backend, not deployed production wiring.

For the isolated fixture, mount `ha_recording_transport_smoke.py`, `tests/ha_recording_worker_checks.py`, the canonical `recording_session.py`, and `recording_worker/` beside the test runner, then set `RECORDING_WORKER_PROOF=1`. The command is `python /work/tests/ha_runtime_smoke.py`. No household config, token, microphone, service credentials or PVC is mounted.

## Review gates before rollout

The canonical candidate includes authenticated registration, public-event lifecycle, bounded cancellation/deadlines, shared durable unknown-source holds, original-upload receipt recovery, and owned shutdown/backup gates. Keep integration native recording and this app disabled until actual Supervisor app-token authorization is checked without capture, and an actual Supervisor installation is tested while capture remains disabled. A later approved physical test must prove prompt/capture handshake timing, original announcement playback, chime, phone transcript and audiobook resume through the existing transport. Synthetic prompt gating does not prove a real satellite will wait for capture ACK.

The [image build receipt](tests/evidence/image-build.json) records the exact built version and source hashes, pinned Python base, dependency versions, image/archive digests, verified layers, and six installed application files. Check its version and hashes before using it; a historical build does not prove changed source. The isolated helper runs disabled/missing-token/malformed-options startup before adding a test-only validator, then executes loopback suites as UID/GID10001 with no supplementary groups, all capabilities dropped and no-new-privileges. Application files are owned by root and are not writable by that user. The helper requires chroot and UID/GID-switch capabilities only before entering the isolated filesystem. No credential, recording or household configuration enters the build. Earlier 0.0.1 and 0.0.2 receipts are preserved as historical evidence.

## Startup and public packaging

HTTP/WebSocket connection setup, the complete authentication/admin check, and control registration each have a five-second budget. Socket close is bounded to two seconds. These are network bounds; recording still uses its backend-issued deadline. Transient control timeouts/transport loss and fixed registration availability codes `unknown_command`, `backend_unavailable`, or `previous_connection_busy` close the old socket and retry fresh control with 1, 2, 4, 8, 16, then at most 30 seconds between attempts. Authentication/admin/proxy denial, malformed acknowledgements and generic registration rejection stop. No code retries a capture. The backend must send the exact protocol-v2 result: `{id: 2, type: "result", success: true, result: {connection_id: "..."}}`.

Create a public repository overlay in a **new** directory with:

```text
python scripts/export_candidate.py --destination <new-directory> --validator-source <canonical-recording_session.py>
```

The recipe verifies current runtime source against the image receipt, checks the canonical test-validator hash, copies an explicit source/documentation allowlist, and records every exported file hash. It emits `repository.yaml` and a `recording-worker/` app directory for the existing Household Voice public project. It performs no publication. The test-only canonical fixture uses the same reviewed LF byte form as the image test; provenance also records the original source hash. `.dockerignore` excludes tests, fixtures, caches and unrelated files from normal app image builds.

In the exported `recording-worker` directory run `python -m unittest discover -s tests -p "test_*.py" -v`. Three packaging tests cover portable execution without the private workspace layout, preservation of existing destinations, and rejection of source changed after the reviewed image build. Keep Enabled off and manual boot while production integration and Supervisor authorization remain pending.

The worker transport is separate from the shared assistant's prompt, schedules and players; the staged canonical integration owns its registration. No pipeline assignments or stock controller/firmware behavior were changed by this package. [Build and validation instructions](BUILDING.md) cover local Dockerfile and optional build-only CI checks.

The app has no manual integration entry-ID setting. Canonical registration selects exactly one loaded Household Voice entry automatically and refuses missing/ambiguous ownership. The per-directory `.gitattributes` preserves reviewed worker source bytes so exported image-receipt checks remain valid on Windows and Linux; it does not change the parent integration's line-ending convention.
