# Household Voice

Home Assistant custom integration for shared timers and alarms, Music Assistant
playback, recorded announcements, and room speaker configuration. The durable
coordinator runs inside Home Assistant. No companion app, container, cloud
account, or paid subscription is required for the core features. Original recordings
on a single shared voice pipeline use an optional [Recording Worker app](recording-worker/README.md),
which transports native microphone audio and leaves scheduling and playback in the integration.

This is the **0.3.25 prerelease**. Automated tests cover implementation
contracts; actual microphone capture, speaker delivery, phone presence, firmware,
and audiobook resume require validation on your equipment. Keep an independent
alarm available until your installation passes those tests. Home Assistant must
be running for alarms and announcements to play.

## Install and update

For music, an explicitly named room or speaker is the destination, including from
the authenticated HA app. Otherwise the requesting device's room supplies the
default. If location is unavailable or the destination is ambiguous, the assistant
asks which room or speaker. A voice satellite is not required for explicit targets.

Music addressed to a speaker that belongs to a group plays on that current group.
Follow-up media controls use its current group leader. This does not change group
membership or the separate room-isolation rules for alarms and announcements.

To try web-assisted music discovery, enable **Configure → Music content → Find
music using Apple Music web results**, then **Review and save**. It reuses the
configured Brave credentials and search budget. Music terms leave home for search;
only Apple Music links with verified catalog metadata become playback candidates.
Playback continues through Music Assistant, including content filters and controls.
Books and sleep stories are unaffected. Both new switches default off. Leave the
separate direct Sonos experiment off for normal use: its native queue follow-up
controls have not yet been integrated.

Choose **Configure → Music content → Block explicit tracks**, then **Review and
save**, to filter Household Voice music suggestions and playback. Filtering is
off by default. **Also block tracks without a content rating** defaults to on
when filtering is enabled; turn that option off to allow unrated tracks.
Ratings come from Music Assistant provider metadata, not a title or lyrics scan.
Albums, playlists and artist selections become finite lists of allowed tracks.
Radio streams and automatic endless playback are unavailable with filtering on.
Clear or replace an existing dynamic/autoplay queue with a finite queue with
autoplay off in Music Assistant before starting filtered playback. Collections
and existing queues exceeding 500 items cannot be verified by this filter.
Audiobooks keep their existing playback and resume behavior. Saving this setting
does not stop music already playing or control playback started by other apps.

For announcements, say **"broadcast"** or **"announcement"**. The assistant asks
**"What's the announcement?"**, then records your answer and plays your original
voice on your selected speakers. The two short commands use Home Assistant's
native sentence handling and do not wait for an LLM acknowledgement. Use the
normal Assist pipeline; agent-only API calls bypass Core sentence handling.

The Household Voice panel shows the current defaults. Choose **Configure →
Announcement speakers**, select one or more Music Assistant speakers, then
**Review and save**. These defaults apply when no room is named. Selected
destinations dispatch concurrently; exact audible alignment depends on the
speaker provider and Music Assistant grouping. Saving settings plays no audio.

Requires Home Assistant 2026.9.4 or newer. CI validates 2026.9.4 and 2026.10.1;
other versions need compatibility validation. Music Assistant and a compatible voice satellite are required for
their respective audio features.

1. In HACS, add `https://github.com/kidhasmoxy/household-voice` as a custom repository
   of type **Integration**. Install Household Voice, then restart Home Assistant.
2. Go to **Settings → Devices & services → Add integration → Household Voice**.
3. Choose the local embedded coordinator. Set a Home Assistant internal URL that
   your Music Assistant server can reach, then open configuration to map rooms,
   voice devices, Music Assistant
   players, announcement defaults, and optional phone presence/notification pairs.
4. For the native-tool preview, follow [shared assistant setup](docs/shared-assistant.md)
   using one ordinary conversation agent and one editable master prompt. Tool
   registration alone does not select or curate the agent's tools.
5. Keep an existing legacy beta configuration until its migration gates pass.
   Legacy APIs and the optional router remain for compatibility; do not select
   both legacy APIs and native Household Voice tools in the same agent.
6. Enable and test one feature and one room before expanding to the household.

For manual installation, extract the release's `household_voice.zip` into
`/config/custom_components/household_voice/`, then restart Home Assistant. Updates
use HACS or replace that code directory with a newer release. Keep a usable
existing backup and small scoped configuration/code rollback copies; an
additional whole-installation backup is not required for every update.
Code replacement must not delete stored integration
state. The repository ZIP is source, while `household_voice.zip` is the install
asset. Release tags must match the manifest version.

For Voice PE installations affected by premature native reply completion, the
optional [ESPHome lifecycle overlay](esphome/README.md) preserves the official
device package and can be pinned to a reviewed source commit. It is a separate
firmware update, not part of a HACS installation.

## Features and configuration

Native microphone eligibility is independent of the preferred music output.
Native recording requires an available, ungrouped physical Music Assistant
player with one verified native announcement output. The legacy configuration
retains standard Music Assistant protocol and physical-group routing. Native
recordings keep the stricter policy through durable capture receipts when that
option is disabled. Held or unclear destinations remain refused, and neither
mode adds group changes or automatic retries. Several published Sonos output
protocols currently block native recording even when native is active; keep
the preview disabled in that configuration. Spoken-content pause/resume requires
the exact destination's queue. These client checks cannot atomically prevent a
concurrent server-side topology or queue change, and provider completion does
not prove acoustic delivery.

Development preview: native Assist contribution is implemented behind the
off-by-default `assist_tools_enabled` setting. It reuses existing Household Voice
capabilities without a custom conversation agent, and adds source-context and
sourced family-knowledge tools. The options menu provides **Shared Assist tools
(preview)**. Current-area media/recipe resolution, ringing-first stop and later
request schedule-choice validation are implemented. Keep the existing beta
configuration until provider tool selection, microphone follow-up and original
recording migration pass their respective gates. Enabling the preview must also
remove legacy API selections from the agent; HA's contribution callback does not
know what other APIs that agent selected. No automatic cutover occurs.

- Durable, shared timers and recurring or one-shot alarms with revision-checked
  commands, restart recovery, bounded ringing, and an HA management page.
- Configured Music Assistant search/play/resume for music, stations, audiobooks,
  and a user-selected sleep playlist. No household profile or library is bundled.
- Broad music requests choose a relevant playlist and name the selection. When
  no playlist fits, the adapter tries a metadata-verified genre seed on a provider
  supporting similar tracks, then a matching continuous station. A genre seed
  starts as a normal track, then native Music Assistant autoplay supplies the
  continuation without foreground mix generation. Continuation follows the MA
  autoplay mode; automatic mode may fall back to the library. Artist requests
  explicitly play their top tracks; clear spelling variations can match ordinary
  music names. Bare exact artist names prefer the provider's first matching
  artist; uncertain spelling and explicit ambiguous selections still ask.
  Soundtrack requests select albums with soundtrack metadata; conflicting editions
  still ask for a choice. Explicit shuffle uses native Music Assistant album
  playback so shuffle survives loading. Simple named-room destinations resolve
  through configured Home Assistant areas. Books/stories retain exact matching.
  Structured music requests preserve title, artist, album, room and shuffle across
  typed albums, artist clauses, polite wording and reordered modifiers. Recognized
  requests bypass the language model while retaining profile, room and catalog
  guards. Unsupported constraints remain with the configured agent. Media calls
  have no integration-added completion deadline; slow valid requests can finish.
  Caller cancellation and native transport errors remain, and uncertain actions
  are never automatically replayed. Five seconds is an optimization target, not
  a reason to fail a valid request or a microphone-to-speaker latency guarantee.
  Supported Household Voice media fallback acknowledgements require request-bound
  tool receipts. This does not validate every native Assist action or prove the
  requested audio was audible. Ambiguous results offer numbered choices with
  edition, artist and album metadata. On a later turn, replies such as “the
  second one” or “the live version” select a saved choice through the same tool.
  Original constraints are retained; an explicit room or music-shuffle change
  is allowed. Choices expire after two minutes, belong to the source/user and
  available conversation identity, and cannot be replayed as a new action.
  A fresh catalog lookup checks the selected metadata before playback. Natural
  language interpretation and physical follow-up listening require acceptance
  on your configured agent and equipment; this does not establish Alexa parity.
  Autoplay is disabled before finite replacements and spoken resume. On MA 2.10.6
  an already-running refill can still append after disable; no public cancellation
  barrier is available, so seamless switching during refill remains a beta limit.
- Recorded broadcasts: ask what to announce, capture the next utterance, replay
  the original recording with a chime on configured speakers, and send its exact
  transcript to configured phones whose presence satisfies the selected home policy.
- Room mappings for external replies, music, wake ducking, and alarm speakers.
- Automatic assistant/speaker pairing from Home Assistant areas, with optional
  per-assistant overrides. Multiple eligible speakers require a choice. New alarms
  belong to an assistant and follow its current paired speaker or, when unpaired,
  its built-in speaker if HA announcements are supported. New voice timers can
  also follow their listening assistant. Existing room-only schedules remain intact.
- Ambiguous recognized timer edits ask which timer you mean. Follow-up choices
  are tied to the device, conversation, and saved revision. Saying "stop" first
  dismisses all currently ringing household timers and alarms without cancelling
  countdowns or future alarms. The native-tool preview handles these through the
  shared ordinary agent; the older router remains only for existing beta setups.
- Shared room volume handling preserves manual changes and retries confirmed
  restores when an unavailable speaker returns. Group and physical-speaker aliases
  are checked before ducking; discovery does not enable external speech by itself.
- Optional **Family friendly** knowledge answers using an existing local OpenAI
  compatible provider and Brave search credentials. The parent can edit/reset the
  prompt, choose source domains, and set daily/monthly search limits in HA settings.
- Optional forecast/place lookup and recipe tools. Brave recipe search requires
  your own supported API key and explicit enablement; request limits apply.

Feature switches start disabled. An invalid requested room does not become a
whole-house announcement. Phone notifications contain text, not recordings or
signed audio links. The default phone policy requires a home report within five
minutes. Configuration can instead require a report within one hour or accept the
current Home Assistant home state without an age limit. Away, unknown, and
unavailable states are excluded under every policy. Choose the policy that matches
your phone's reporting behavior. For original recordings on one shared pipeline,
use the optional Recording Worker and the opt-in native backend described in
[shared assistant setup](docs/shared-assistant.md). Existing legacy recording
profiles need a separate STT wrapper per satellite; do not share those wrappers.

For stock assistants, enable **Lower room speakers during native assistant
conversations (beta)**. Conversation stays on the assistant's built-in speaker;
music, books, broadcasts and shared alarms/timers retain their configured paired
outputs. HA satellite state starts ducking after it observes listening and keeps
it through processing and responding. It restores owned volume changes after
the assistant finishes; deliberate manual volume changes are preserved. The HA
event arrives after the physical wake detection, so ducking is not instantaneous.
Native wake interruption remains dependent on the device and its settings.

Grouped Sonos speakers use native per-speaker volume for room ducking, so only
speakers in the assistant's room are lowered. Alarms detach their paired Sonos
speaker before ringing and restore its old group afterward only if the remaining
group and the target's playback are unchanged. Later user changes take priority.
An uncertain grouping or audio outcome leaves the speaker isolated and reports
recovery required in the schedules sensor; it does not blindly replay a mutation.
This requires the native Sonos integration and an exact registered physical
identity matching the Music Assistant player. Unknown grouped providers are
refused. Home Assistant Core is unchanged.

External conversation replies require the separate advanced routing adapters and
compatible, authenticated satellite events. Stock Voice Preview Edition firmware
alone does not establish that external reply path.
No firmware or device credentials are distributed here. Ordinary stock satellite
speech and external-speaker speech must be tested separately.

The optional EchoMuse companion app uses the Home Assistant management page for
administrator provisioning, enabling, revoking and recovering individual sources.
Provisioning creates a disabled source and shows its controller credential once.
An unpaired assistant keeps its normal local speech: the controller selects native
audio before the turn starts. A configured but unavailable speaker, an identity
mismatch, or an uncertain previous playback does not trigger duplicate local audio.
Pairing an assistant does not install the companion app or change device firmware.

Built-in alarm playback retains the assistant's native volume. It uses finite
clips; dismissal stops further clips but an already submitted clip may finish.
If completion becomes uncertain, a durable hold prevents blind replay. After
checking that audio stopped, an administrator can use **Recover built-in speaker**
in Household Voice settings. Recovery does not send a generic media-stop command.

Version 0.3.0 migrates schedules to database schema 4. Do not run an older version
against migrated state. Keep a pre-upgrade HA configuration backup for rollback;
restoring it also rewinds subsequent schedule changes.

General knowledge uses curated search excerpts and a separate model request with
no household tools or conversation history. Strict SafeSearch is always requested.
The default explains things at about an eight-year-old level, including factual,
non-graphic body, puberty and health education. It avoids explicit sexual content,
graphic violence and dangerous instructions. Filtering and model moderation are
fallible; sources are cited, not certified as true. The router recognizes common
English question forms; other requests use the existing agent with the family
prompt added. This is not full natural-language isolation of every mixed request.
Provider failures in the knowledge path do not fall back to an unrestricted agent.
Questions, including personal or health questions, are sent to Brave and the
configured local model. Search limits bound attempts; they do not determine your
provider's billing. Existing subscriptions require a supported API credential.

Bare announcement requests receive only "What's the announcement?" before the
recording. Body-only replay from a command containing its message remains
experimental: the available alignment service failed acoustic validation and
stays disabled. Without validated alignment, the assistant asks for a fresh
recording instead of replaying the command or substituting generated speech.

The default spoken-media policy pauses recognized audiobook/podcast queues and
resumes after completed announcements only if the queue, item, position, and
observed user intent are unchanged. Uncertain playback leaves the book paused.
Music Assistant provider completion is not acoustic confirmation. Explicit
speaker recovery requires confirmation that earlier pending audio has stopped.

## Data and networking

The integration stores SQLite schedule/delivery state and its signing key in
`.storage/household_voice`. Transient recordings live in
`.cache/household_voice/recordings`; HA 2026.9.4 and 2026.10.1 configuration backups exclude
`.cache/*`. Backup hooks temporarily freeze the coordinator during configuration
copying, so timer execution can pause while a backup runs. Stored schedules recover
afterward according to the missed-alarm policy. Voice capture is
explicitly armed; it does not continuously record idle microphone audio.
Recordings use limited retention and signed access for playback. Keep temporary
recordings/transcripts out of long-term backups and verify any external backup
tool's policy. Never put tokens, device provisioning, recordings, or exported HA
configuration into this repository.

Core household operation uses Home Assistant, Music Assistant, and your configured
speech/media providers. Optional research calls Open-Meteo, Brave Search when
enabled, and explicitly selected public recipe pages. Existing media and speech
providers have their own networking and billing. No telemetry or hidden remote
management service is included. No Nous subscription adapter is claimed.

Home Assistant and Music Assistant outages prevent physical output. Cross-room
state consistency does not provide an independent hardware alarm. Firmware,
speaker/provider differences, interrupted playback, and startup recovery need
physical acceptance tests before relying on the system.

## Development

Run `python -m unittest discover -s tests -v` and
`python -m compileall -q custom_components` from the repository root. These checks
exercise durable scheduler behavior and packaging, not real HA audio. Run
`python scripts/build_release.py` to create the deterministic install ZIP.
CI also runs `tests/ha_embedded_smoke.py` inside Home Assistant 2026.9.4 and 2026.10.1 with
network access disabled. It checks local lifecycle, state persistence, frontend,
signed synthetic audio, and construction of all 17 registered LLM tools; it does
not contact household endpoints.

After intentional source edits, review the diff and run
`python scripts/update_manifest.py` to refresh hashes of the existing allowlist.
New files require an explicit manifest entry before they can enter a release.
Update the integration version and changelog before publishing a matching `v*`
tag. The workflow marks releases as prereleases until physical acceptance is
complete. Enable prerelease display in HACS to select them.

Source is MIT-licensed; see [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). Report
bugs at [GitHub issues](https://github.com/kidhasmoxy/household-voice/issues).

### Default speakers by room

Open **Settings → Devices & services → Household Voice → Configure → Room speakers**. Choose a room, choose its default speaker, then use **Review and save**. All assistants in that room inherit the default for music, alarms and timers. Rooms without assistants can also have a default. An explicitly named music speaker takes priority. Automatic uses the sole suitable room speaker; conflicting migrated choices require selection.
