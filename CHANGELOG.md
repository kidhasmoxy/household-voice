# Changelog

## 0.3.26

- Add opt-in fast commands with strict native device intents, shared durable timers and ringing-first Stop.
- Preserve the existing shared AI agent, request identity, conversation context and follow-up choices for other requests.
- Keep Core local preference off to prevent native satellite timers bypassing household speaker routing.

## 0.3.25

- Replace assistant-specific speaker settings with Room speakers: choose a room and its default speaker, including rooms without assistants.
- All assistants inherit their room default for music, alarms and timers. Preserve explicit music destinations and existing group isolation.
- Migrate unambiguous legacy preferences by room; conflicting preferences require a choice. Missing saved speakers never silently fall back.


## 0.3.24

Music routing now honors an explicit room or registered speaker name from an
authenticated app request without requiring a voice satellite. With no explicit
destination, the requesting device's room supplies the default speaker. Missing
room context asks which room; it does not imply a music-provider failure. Named
speakers take precedence over room defaults. Group routing and content filters
continue to apply.

## 0.3.23

Music requests addressed to a grouped speaker now use its current Music Assistant
group leader. Play, pause, resume and queue controls retain the requested room's
profile and operate on that group without joining or unjoining speakers. Group
membership changes during a request invalidate the pending action. Alarm and
announcement routing retain their existing rules.

## 0.3.22

Optional Apple Music web search finds share links using the existing Brave search
account, verifies their identity and availability through the configured Music
Assistant profile, then uses existing room routing and playback controls. Named
music constraints and content filters still apply; unavailable or irrelevant web
results fall back to the normal catalog. Books and sleep stories keep their
existing path. The search shares the configured knowledge-search budget/cache.

An independent direct Sonos experiment is off by default and limited to idle,
ungrouped speakers with empty finite MA queues, without filtering, shuffle or
endless playback. Native queue follow-up controls remain experimental.

## 0.3.21

Optional **Music content** settings exclude explicit tracks from Household Voice
suggestions and playback, with separate control over unrated tracks. Album,
playlist and artist requests dispatch only allowed tracks, and playback checks
fresh metadata before starting. Resume/skip/queue changes check the full finite
queue. Radio and unfiltered autoplay are blocked while filtering. Settings changes
invalidate saved choices; audiobook progress behavior is unchanged.

Ambiguous music and audiobook requests return numbered catalog choices. A later
reply selects a saved choice while retaining the original content, artist,
shuffle and destination constraints; an explicit room or music-shuffle change
is supported. Choices expire after two minutes and are bound to the source and
user, with one playback attempt and fresh catalog/configuration validation.
Track and book edition labels and explicit availability are preserved. Native
Assist guidance maps ordinal, artist and edition replies to these choices.

## 0.3.20

Shared Assist exposes one stop tool with no mode arguments. Every stop request
first dismisses currently ringing household alarms and timers; only when none
are ringing does it stop media on the requesting assistant's paired output.
The separate media-stop tool is no longer exposed in shared mode. Future alarms
and counting-down timers remain untouched; uncertain or changing ringing state
never falls through to media. Internal media execution remains for compatibility.

## 0.3.19

Alarms on a paired Sonos speaker detach that speaker from its group before
playing. Group membership is restored after ringing only while the observed
group and playback remain unchanged. Durable intent and uncertain-outcome guards
prevent replay after interrupted grouping or audio. Room ducking uses native
Sonos per-speaker volume even when speakers are grouped across rooms, preserving
other rooms and deliberate manual volume changes.

## 0.3.18

Shared-assistant settings now show only the active native Assist controls.
Legacy conversation routing, external replies, dedicated STT recording and
alignment setup remain available for legacy installations, without cluttering
the shared-assistant menu. Hidden compatibility values are preserved on save.
Alarm volume and fallback speakers can be edited without creating a legacy room
mapping. Recovery and mapping removal are grouped under Maintenance.

## 0.3.17

"Broadcast" and "announcement" now use native Home Assistant sentence triggers
to start the existing original-voice recording workflow without an LLM preamble.
The recording system alone asks "What's the announcement?" and captures the
answer. Source verification, cancellation and durable delivery guards remain.

Default announcement speakers are visible in the Household Voice panel, with
Announcement speakers first in settings and saved selections prefilled. Delivery
capacity follows the configured destinations so the fifth and later speakers do
not wait for earlier playback to finish. This provides concurrent dispatch, not
a guarantee of acoustic synchronization across different speaker providers.

## 0.3.16

Shared-pipeline original recordings use an explicit, durable, versioned standard
Music Assistant announcement contract. Microphone provenance is independent of
whether a speaker advertises native, AirPlay or other output protocols. The
existing Music Assistant renderer, chime handling and configured group behavior
are retained; no separate Music Assistant fork is required.

Older native handoffs keep their verified-native policy. Missing, corrupt or
inconsistent contract/source/receipt evidence cannot downgrade them. New contracts
are part of immutable handoff identity and resolved by the embedded authority,
not trusted from caller metadata. Exact spoken queue ownership, uncertain-output
holds, final source/destination checks and no-replay rules remain.

Enabling native microphone capture no longer changes unrelated alarm/EchoMuse
playback policy. Original conversation audio stays on the satellite. Shared
pipeline migration still requires proving the existing recording app's supported
Supervisor-token registration before changing pipeline assignments. Device
pipeline preferences can be saved while a satellite is offline; physical capture
and hearing remain separate acceptance tests.

Install ZIP metadata is explicit so Windows and Linux builds produce identical
bytes for the same reviewed sources. No worker runtime/image update is required.

## 0.3.15

Valid native microphones no longer depend on their music speaker pairing.
Recording still respects persisted microphone/native/room-audio uncertainty.
Frozen announcement destinations are checked before the follow-up question,
capture and room focus, and again before transport actions. The native recording
preview requires a verified isolated native destination, so grouped, linked,
multiple-output and unknown routes are refused in that mode. Existing legacy
configurations retain standard Music Assistant routing, including existing
physical speaker groups. Neither mode adds group changes or automatic retries.
Durable capture receipts keep admitted native recordings under the stricter
policy even after native recording is disabled; unclear provenance is refused.
Paired MA alarm clips use the configured mode's destination checks before ducking.

Spoken-content pause/resume requires the exact destination's queue. Repeated
client checks do not provide atomic server topology ownership, and provider
completion remains a workflow result rather than acoustic confirmation.

Current-playback tools suppress retained song/artist/album metadata while idle
or unknown, avoiding presenting the last item as currently playing. Paused
content remains visible with a non-playing state. No playback is stopped or
cleared by this presentation change.

A paired-speaker media-control tool handles next/previous music tracks, volume
and mute. It resolves the listening assistant's preferred MA output rather than
using a generic area-wide intent. Volume changes made during wake-word ducking
update the journaled normal baseline while retaining the active duck lease.
Spoken/unknown skips, grouped mutations, protected announcement outputs and
uncertain volume ownership remain refused. These controls require updating the
shared prompt and curating competing native tools before activation.
Final admission rechecks raw player topology, the pinned registry/provider
identity, and retained announcement holds after awaited journal writes. A proven
refusal preserves a pending journal without dispatching the requested mutation.

## 0.3.14

Opt-in native recorded-announcement backend reuses the existing recording store,
durable outbox and playback guards. An optional amd64 Recording Worker app carries
original PCM through the public Core capture API; no extra conversation agent,
STT engine or scheduler is added. HA registries generate source bindings. Setup,
reload, backup, unknown-source recovery and shutdown use owned lifecycle checks.
Unknown recording holds survive changing recording modes. The admin sidebar adds
dropdown-based recording recovery. Both integration and app options default off;
Supervisor authorization and physical room acceptance remain deployment gates.

Media tools can report current content, including grouped speakers, without
changing playback. Explicit shuffle/repeat edits retain profile, source and
spoken-content safeguards before each dispatch; uncertain changes are not replayed
or rolled back. Omitted forecast locations now use configured HA home coordinates;
explicit invalid or stale place identifiers never fall back to home.

Alarms now preserve an explicit date or resolve today/tomorrow in the requested
timezone. Existing alarm clock changes use guarded rescheduling, preserving the
assistant, stored date/recurrence and disabled state, with revision and bound
clarification checks. The manager adds an optional date picker and clock editing.
Actual Home Assistant CI covers the date, DST, replay and rescheduling contracts.

Development preview of native Assist tool contribution. An off-by-default setting
contributes capability tools through Home Assistant's standard LLM platform,
reusing the existing API objects, executors, caches and locks. A read-only source
context tool resolves the requesting device from HA's registry, and sourced family
knowledge becomes a tool. No conversation agent or router is added.

Media play tools expose explicit exact or broad selection, so genre requests can
use the native agent's tool arguments without relying on transcript parsing.
Named albums, soundtrack shuffle and spoken-content constraints remain explicit.
Native media and recipe tools resolve the current HA device area automatically,
reuse verified Music Assistant profiles, and invalidate playback on area changes.
Ringing-first stop dismisses the complete ringing set atomically; explicit
alarm/timer stops cannot fall through to music. Expiring schedule-choice tokens
require a later request and the same device/user, arguments and candidate revisions.
The shared-tool preview has an administrator options switch and capability
guidance rendered from available tools. Native cross-tool references are renamed
without changing legacy API objects.

Legacy router behavior is retained during staging. This preview does not migrate
pipelines, remove duplicate selected APIs, or claim acoustic stop/one-pipeline
original-recording parity. Real-Core fixtures exercise the canonical integration
and owned question/capture gate with synthetic hardware and STT. Body-only audio
from a one-request announcement is not implemented in this native path.

## 0.3.13

Removes integration-added media response, lookup and dispatch deadlines. Slow
valid requests can complete instead of failing to satisfy a latency target.
Caller cancellation and native transport errors remain; uncertain actions are
never automatically replayed. A slow provider can still hold the existing media
serialization lock until completion, disconnect or caller cancellation.

Structured English music requests preserve title, artist, album, room and shuffle
across typed albums, artist clauses, polite wording and reordered modifiers.
Unsupported constraints and combined home commands remain with the configured
agent rather than being dropped. Room identities are still resolved from trusted
configuration before any playback.

Supported Household Voice media fallback responses now use request-bound tool
receipts. A model's unverified claim does not establish playback. This guard
does not cover every native Assist action; semantic follow-ups and full playback
acceptance remain beta work. No model subscription or controller is added.

## 0.3.12

Soundtrack requests resolve albums using soundtrack/cast metadata, while preserving
edition ambiguity and excluding spoken excerpts. Simple room-qualified requests
and explicit shuffle run through the guarded direct route, including brief speech
fillers. Shuffled albums use Music Assistant's native play option so album loading
does not silently switch shuffle off. URI, configured user, and active queue are
revalidated before native playback.

Music play requests have a 4.5-second foreground tool budget, including waiting
for another media call. A provider timeout before an action is reported promptly;
once an action has been sent, the result is uncertain and never automatically
retried. This is a response bound, not a guarantee that a provider starts audible
music in five seconds. Microphone, speech recognition, and a separate LLM route
are outside this tool budget. Books retain their existing preparation allowance.

## 0.3.10

Bare exact artist names use the first available artist in the provider-ranked
catalog results, so common names shared by multiple artists do not cause a
needless clarification. This is a recommendation rather than a claim of global
uniqueness. Explicit media selections and competing fuzzy matches keep stricter
selection. The configured profile and exact provider URI remain revalidated.

## 0.3.9

Repairs artist selection in the direct music route. An unqualified music name
can select its unique artist even when the catalog contains a same-named album.
Explicit media-type requests and genuinely competing artist matches retain
their existing safeguards. This fixes the artist/album collision found during
the v0.3.8 live beta test, without restoring a language-model round trip.

## 0.3.8

Genre fallback starts a verified seed track with radio mode off, then enables
Music Assistant's native autoplay. Refill runs in Music Assistant after startup;
the voice flow no longer waits for a generated radio playlist. Simple named music
requests also use the guarded catalog path directly, removing the language-model
round trip for artist names and ordinary spelling variations.

Autoplay is disabled before finite replacements and spoken-content resume. Queue
identity and configuration are checked around native commands. Music Assistant
2.10.6 can still finish an already-running refill after disable; its public API
offers no atomic cancellation barrier. No queue-clearing or deletion workaround
is used. Autoplay follows the user's existing Music Assistant continuation mode;
it is not a guarantee that every later track matches the requested genre.

The performance target is actual playback within five seconds. Provider startup,
catalog misses, and the speech pipeline can still exceed it; this release does
not equate a quick acknowledgement with audible playback or impose a false
five-second success claim.

## 0.3.7

Clear genre/mood music commands use the existing trusted Music Assistant path
directly and speak the selected collection name without a language-model round
trip. Ambiguous names and compound requests remain with the configured agent.

All validated replacement playback requests receive the bounded 65-second
preparation allowance after a maximum 35-second lookup period. The earlier
mix-only allowance missed slow artist/playlist preparation. Playback uncertainty
is explicitly distinguished from a missing catalog match; uncertain commands are
not automatically retried.

## 0.3.6

Repairs transitions out of Music Assistant dynamic endless queues. Queue mode is
read from the configured Music Assistant instance before replacing media, so
manual shuffle/repeat preparation does not reject an otherwise valid request.
Finite replacement retains ordered artist and spoken-content playback. The
adapter does not retry a failed or uncertain playback action.

Catalog lookup and normal actions retain the 35-second tool budget. Only a
validated generated mix may receive a separate 65-second playback allowance,
for at most 100 seconds total. This accommodates slow provider preparation;
it does not make cold mix generation fast or add automatic retries.

## 0.3.5

Music requests prefer a relevant playlist and return its name for a brief spoken
acknowledgement. If no suitable playlist is found, the adapter tries a generated
genre mix using fresh genre metadata and a provider that supports similar tracks;
otherwise it can use a matching continuous station. It asks when the catalog
cannot support a reliable choice. Provider support and metadata remain required
for generated mixes; no paid fallback or new subscription is added.

Artist requests explicitly resolve up to 25 top tracks through a provider
authorized by the configured Music Assistant profile. Ordinary music names allow
high-confidence punctuation and spelling matches while close alternatives still
ask for clarification. Audiobook and sleep-story selection remains exact and
preserves provider-managed progress.

Adds bounded Music Assistant lookups and a 35-second media-tool deadline without
automatically replaying an uncertain playback action. This bounds the tool, not
the complete speech or language-model pipeline. Compatibility fixtures exercise
the new client calls in actual HA runtimes with synthetic music services.

## 0.3.4

Repairs Music Assistant catalog handling for nested artist/album metadata with
name-based IDs, while keeping executable catalog URIs strictly validated.
Broad music selection uses the router's authenticated original request even when
the installed Home Assistant LLM context omits the original prompt.

Hides finished timers from the default management view, with an optional history
toggle. Room controls use dropdowns; assistant selection follows its Home
Assistant area. Pairing shows friendly room/speaker names, an obvious settings
link and the existing automatic room choice. Advanced external-reply provisioning
is collapsed so it does not obscure ordinary pairing.

Includes an optional, pinned-source ESPHome native voice lifecycle overlay under
`esphome/`. It waits for announcement activation before reporting playback
completion, preventing the previous two-second timeout from reopening the
microphone during a delayed reply. The overlay requires a separate firmware
compile/upload; HACS does not install firmware. Acoustic playback, interruption
and follow-up acceptance remain device tests.

## 0.3.3

Music requests accept current Home Assistant area names and aliases while
preserving verified source devices and configured playback destinations.
Ambiguous aliases ask for clarification instead of choosing another room.
Broad requests such as "play some jazz" can choose relevant available music
playlists or stations from fresh Music Assistant results. Named music, books,
and sleep-story requests retain their exact-title and ambiguity checks.

Native room ducking recognizes a new turn after a brief idle transition. The
activity watchdog restarts for the new turn, and a manually changed volume
becomes its new ducking baseline. Speakers still owned by the preceding duck
remain quiet through the handoff without a volume bounce. Changes during the
same turn, protected announcements, and uncertain writes remain preserved.

Adds actual Home Assistant media-tool validation to the compatibility matrix.
These fixtures use synthetic services and do not establish physical playback,
wake recognition, or recovery from a device stuck in its listening state.

## 0.3.2

Keeps compound household status requests such as "What are my active timers and
alarms?" out of the public-knowledge lane so they reach the shared schedule tools.

## 0.3.1

Adds optional room ducking for stock voice assistants. Conversation audio stays
on the assistant; paired speakers continue handling music, books, announcements
and shared alarms/timers. Home Assistant's authenticated satellite state holds
the room volume reduction through listening, processing and responding, then
releases it when the assistant finishes. This does not require the EchoMuse
companion or custom Voice PE routing firmware. Wake detection and native barge-in
remain device capabilities. This beta does not cancel a paired Music Assistant
announcement when the satellite hears its wake word.

## 0.3.0

Adds Home Assistant area discovery, optional assistant speaker overrides, and
assistant-owned new alarms that follow the paired speaker. Preserves existing
alarms and timer routing. Shared room ducking respects manual volume changes and
recovers confirmed restores when speakers return.

Shared timers and alarms can use an unpaired assistant's built-in speaker when
it supports Home Assistant announcements. A configured external speaker that is
unavailable does not silently redirect audio. Native ringing uses finite clips,
preserves native volume, and records uncertain output durably until administrator
recovery. New assistant bindings retain device identity across entity changes.

Recognized ambiguous timer changes ask for a name or numbered choice before
changing a schedule. Choices expire and retain revisions, so a stale answer
cannot mutate a different timer. A bare "stop" first dismisses all timers and
alarms ringing in the authoritative household snapshot, preserving countdowns
and future alarms. Only an empty ringing snapshot permits normal media fallback.
These are bounded English command patterns, not unrestricted natural-language
understanding.

Adds optional Family friendly world-knowledge answers with an editable parent
prompt, curated sources, strict Brave SafeSearch and durable search budgets.
Knowledge synthesis has no household tools. The existing household agent receives
the family content prompt without disabling its authorized controls.

Shortens announcement follow-up to "What's the announcement?" and removes the
initial acknowledgment. One-request body-only replay remains disabled pending
successful acoustic alignment validation.

Adds the opt-in EchoMuse companion receiver and administrator controls, with
durable replay protection and per-source credentials. Unpaired sources can use
native speech when selected before a turn; uncertain external playback does not
fall back. Backup preparation drains pending search-ledger writes and receiver
operations before configuration is copied.

The schedule database migrates to schema 4. Back up configuration before upgrading;
do not point an older integration version at the migrated database. Rollback
requires the matching pre-upgrade configuration/state backup, which also rewinds
schedule changes made since that backup.

## 0.2.6

Clarifies weekday numbering and exact-label schedule selection. Normalizes an
explicit album playback request without a search query. Adds a scoped constraint
for whole, simple English alarm/timer stop and book/audiobook resume commands
while delegating through the router. Matching Household Voice tools fill an
omitted category or reject conflicting arguments before acting. The constraint
is bound to the request context and device and is reset after delegation.
Direct fallback-agent requests and unrecognized wording bypass it; it is not a
guarantee that the model will invoke a tool or complete playback.

## 0.2.5

Adds an optional announcement conversation router. Exact start, cancel, and
status commands call the announcement runtime directly; other requests delegate
to the configured fallback conversation agent. The router starts disabled and
does not select itself in a voice pipeline. Replies distinguish queued work from
confirmed delivery.

Recognizes ESPHome device MAC addresses stored in Home Assistant registry
connections as well as identifiers, while preserving exact device/client and
encryption checks.

Adds a phone-presence policy selector: preserve the five-minute freshness default,
allow a one-hour report window, or explicitly trust the current HA home state.
Away, unknown, and unavailable phones remain excluded.

## 0.2.4

Uses compact, stable API display names so Home Assistant's merged tool names fit
OpenAI-compatible provider limits. Existing API identifiers and selections remain
valid. Actual-HA fixtures validate merged tool schemas before release.

## 0.2.3

Implements Home Assistant's LLM platform discovery callback. Household Voice
keeps its four explicitly selectable tool APIs; discovery does not duplicate
them into the built-in Assist API. The actual-HA fixture verifies this contract.

## 0.2.2

Supports Home Assistant 2026.9.4 without requiring a core upgrade. Both the
validation and release workflows test the integration lifecycle against
2026.9.4 and 2026.10.1 before publishing an installation asset. Optional LLM tool
annotations are attached only on HA versions that expose that interface. Tool
results follow each version's native contract: JSON mappings on 2026.9.4 and
ToolResult objects on 2026.10.1. The actual-HA fixture constructs all 17 tools
and checks safe calls through all four tool APIs on both versions.

## 0.2.1

Public prerelease: one Home Assistant custom integration with an embedded
durable coordinator, shared schedules, management UI, Music Assistant transports,
recorded broadcasts, configurable room mappings, and optional research tools.

Fixes cross-platform release hashing by normalizing source text to LF and
declaring Git line-ending attributes. The v0.2.0 release workflow correctly
rejected inconsistent hashes; it produced no installation release.

Physical household acceptance and firmware provisioning are installation steps;
automated packaging checks do not establish acoustic or device compatibility.
