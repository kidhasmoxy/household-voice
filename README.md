# Household Voice

Home Assistant custom integration for shared timers and alarms, Music Assistant
playback, recorded announcements, and room speaker configuration. The durable
coordinator runs inside Home Assistant. No companion app, container, cloud
account, or paid subscription is required for the core features.

This is the **0.2.5 prerelease**. Automated tests cover implementation
contracts; actual microphone capture, speaker delivery, phone presence, firmware,
and audiobook resume require validation on your equipment. Keep an independent
alarm available until your installation passes those tests. Home Assistant must
be running for alarms and announcements to play.

## Install and update

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
4. Configure the voice provider to select the Household Voice tools alongside
   its existing house-control tools. Tool registration does not select them.
5. Optionally choose a fallback conversation agent in Household Voice settings,
   then select the new Household Voice router entity in one voice pipeline.
   Exact English requests such as "make an announcement", "cancel announcement",
   and "announcement status" use the announcement runtime directly. Other requests
   go to your selected agent. Leaving the fallback blank keeps this router disabled.
6. Enable and test one feature and one room before expanding to the household.

For manual installation, extract the release's `household_voice.zip` into
`/config/custom_components/household_voice/`, then restart Home Assistant. Updates
use HACS or replace that code directory with a newer release. Back up Home
Assistant before updates; code replacement must not delete stored integration
state. The repository ZIP is source, while `household_voice.zip` is the install
asset. Release tags must match the manifest version.

## Features and configuration

- Durable, shared timers and recurring or one-shot alarms with revision-checked
  commands, restart recovery, bounded ringing, and an HA management page.
- Configured Music Assistant search/play/resume for music, stations, audiobooks,
  and a user-selected sleep playlist. No household profile or library is bundled.
- Recorded broadcasts: ask what to announce, capture the next utterance, replay
  the original recording with a chime on configured speakers, and send its exact
  transcript to configured phones whose presence satisfies the selected home policy.
- Room mappings for external replies, music, wake ducking, and alarm speakers.
- Optional forecast/place lookup and recipe tools. Brave recipe search requires
  your own supported API key and explicit enablement; request limits apply.

Feature switches start disabled. An invalid requested room does not become a
whole-house announcement. Phone notifications contain text, not recordings or
signed audio links. The default phone policy requires a home report within five
minutes. Configuration can instead require a report within one hour or accept the
current Home Assistant home state without an age limit. Away, unknown, and
unavailable states are excluded under every policy. Choose the policy that matches
your phone's reporting behavior. Configure a separate STT wrapper/pipeline for each capture
satellite; shared wrappers cannot safely establish microphone identity.

External replies and wake ducking depend on compatible, authenticated satellite
events. Stock Voice Preview Edition firmware alone does not establish that path.
No firmware or device credentials are distributed here. Ordinary stock satellite
speech and external-speaker speech must be tested separately.

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
