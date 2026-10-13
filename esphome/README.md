# ESPHome native voice playback lifecycle overlay

Temporary, narrowly scoped backport for ESPHome's native `voice_assistant`
component. It fixes early completion when a TTS media-player URL takes more than
two seconds to start playing. The original behavior can reopen the microphone
while the assistant is speaking, causing it to transcribe its own reply.

The overlay retains the official Voice PE package, wake-word configuration,
native audio path and controller. It does not route speech to another speaker.
It is unrelated to Household Voice's media-provider selection.

Use with an existing Voice PE configuration:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/kidhasmoxy/household-voice
      ref: REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA
      path: esphome/components
    components: [voice_assistant]
```

Keep existing `external_components` entries. Pin an immutable reviewed commit;
do not use a moving branch. Compile and back up the device before uploading.

The component waits up to 35 seconds for playback startup, then follows the
actual announcement state. Failed startup clears continuation and stops the
pending announcement. Stop, error, disconnect and stale callbacks are handled
without treating failure as successful playback. The original speaker transport
timeout is retained.

Source: ESPHome 2026.9.1, commit
`0b2b16e664ecfc0add37f3435db1c2140b8a687e`; hashes in `source-pin.json`.
Validated with twelve executable C++ lifecycle scenarios and a full Voice PE
26.9.0 / ESPHome 2026.9.1 compile. Physical beta acceptance remains required.
See [upstream issue 16462](https://github.com/esphome/esphome/issues/16462) and
[upstream proposed fix 19408](https://github.com/esphome/esphome/pull/19408).

This directory contains only reviewed source and its upstream license. It has
no device configuration, credentials, recordings or firmware images. Remove the
overlay once an upstream release passes the equivalent playback/follow-up tests.

The startup bound measures entry into the player's `ANNOUNCING` state; that can
precede audible output. Completion follows that state ending, not a microphone
measurement. This patch does not add an independent stalled-stream deadline
while the player remains `ANNOUNCING`, or distinguish decoder failure from every
normal idle transition. Acoustic completion and interruption still require
hardware checks.
