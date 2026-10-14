# Build-only beta validation

The optional app targets amd64 and builds from its own pinned Dockerfile. A local image build or CI check does not install an app, enable recording, publish an image, or contact Home Assistant. Existing immutable image receipts apply to the exact source hashes and dependency versions recorded there; a fresh build needs its own receipt.

From the repository root on a Linux Docker host:

```sh
docker build --platform linux/amd64 --build-arg BUILD_ARCH=amd64 --build-arg BUILD_VERSION=0.0.4 -t household-voice-recording-worker:local recording-worker
mkdir -p /tmp/hv-worker-options
printf '%s\n' '{"enabled":false}' > /tmp/hv-worker-options/options.json
sudo chown 0:0 /tmp/hv-worker-options/options.json
sudo chmod 600 /tmp/hv-worker-options/options.json
docker run --rm --network none --read-only --cap-drop ALL --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges --mount type=bind,src=/tmp/hv-worker-options,dst=/data,readonly household-voice-recording-worker:local
```

Expected output is `{"status": "disabled"}`. Change the root-owned file with `printf '%s\n' '{"enabled":true}' | sudo tee /tmp/hv-worker-options/options.json >/dev/null`; with no Supervisor token, the expected fixed status is `authorization_unavailable`. Omitting both startup capabilities must exit with `privilege_drop_failed`. Do not supply a household token for these checks. To run the synthetic transport/control and portable packaging tests from the same built image:

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 10001:10001 --tmpfs /tmp:rw,nosuid,noexec,size=64m,mode=1777 --mount type=bind,src="$PWD/recording-worker",dst=/work/recording-worker,readonly --env PYTHONDONTWRITEBYTECODE=1 --env PYTHONPATH=/app --entrypoint python household-voice-recording-worker:local -m unittest discover -s /work/recording-worker/tests -p 'test_*.py' -v
```

Supervisor writes `/data/options.json` as root with mode `0600`. The image starts its bounded standard-library bootstrap as root with Python isolated/no-site flags. It reads only the strict `enabled` boolean, clears supplementary groups, changes GID/UID to10001, clears capabilities and sets no-new-privileges before enabling site packages or importing networking. Supervisor's default capability policy already permits SETUID/SETGID; the app requests no added capabilities or protection changes. Do not override the default image user for startup or relax the options file permissions. [Pinned options writer](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/utils/json.py), [pinned default capabilities](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/docker/const.py).

The portable export contains a checksummed test-only validator fixture. Neither fixture nor tests enter the normal runtime image. Loopback sockets work with Docker's network-none mode; the tests have no household endpoint or credential.

The optional `.github/workflows/recording-worker.yml` uses a pinned official checkout action with credential persistence disabled and read-only repository permissions. Pull requests affecting the app and manual workflow runs build a local amd64 image, check disabled/missing-token startup, and run the synthetic tests. It has no image push, registry login, deployment, Supervisor token input, or self-hosted runner. This workflow is staged source; its own GitHub execution remains to be verified after publication. [Official checkout action](https://github.com/actions/checkout).

For a later approved Supervisor trial, add this public repository as an app repository and install/build the worker with manual boot, watchdog off, and Enabled off. First verify app-token authentication and administrator status without registering capture. Enable the integration's native recording option and worker only after that authorization and actual installation gate passes. Then test the selected room with a real satellite before broad rollout. Conversation audio, music/book playback, schedules and announcement destinations remain owned by the integration and existing Home Assistant/Music Assistant transports.
