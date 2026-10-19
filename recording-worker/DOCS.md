# Candidate app configuration

Leave **Enabled** off. The canonical Household Voice candidate wires the worker endpoint behind its separate default-false **native recording** option. Actual Supervisor-token acceptance and physical capture timing remain pending. Starting this app with Enabled off performs no network or microphone work. The beta advertises only amd64.

The v0.0.4 image reads Supervisor's private options file in a bounded root bootstrap, then irreversibly switches to UID/GID10001 with no supplementary groups or capabilities before the runtime loads. It requests no added app permissions, host mounts or protection changes. Invalid options or an unsuccessful privilege drop stop before networking.

After the backend and image are reviewed, the app can use Home Assistant's own Supervisor-provided Core API access. You will not enter an administrator token, room name, STT profile, music provider or announcement speaker here. Those remain Home Assistant integration settings.

Native recording needs one embedded Household Voice authority, announcements, live actions and shared Assist tools. It reuses the normal Assist pipeline and native question service. Unknown microphone outcomes remain held across restart, reload and recording-mode changes; an administrator must verify the selected microphone has stopped before using the integration's recovery controls. Stopping the app or seeing an idle indicator alone is not that proof.

Stopping the worker ends its recording subscription. It does not itself stop a satellite microphone or dismiss an unresolved question. Household Voice must retain and resolve that operation's ownership before another announcement is allowed on the source.
