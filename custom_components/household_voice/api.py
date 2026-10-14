"""Small bearer HTTP client; accepts the HA-owned aiohttp session."""
import asyncio
import json
import re
from .contracts import normalize_url, validate_payload, validate_token


class CoordinatorError(Exception):
    """Safe error text suitable for the HA UI."""


class CoordinatorAuthError(CoordinatorError):
    pass


class CoordinatorConflict(CoordinatorError):
    pass


class VoiceClient:
    def __init__(self, session, url, token):
        self.session = session
        self.url = normalize_url(url)
        self._token = validate_token(token)

    async def _request(self, method, path, payload=None):
        async with asyncio.timeout(15):
            async with self.session.request(method, self.url + path,
                    headers={"Authorization": "Bearer " + self._token}, json=payload,
                    allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise CoordinatorAuthError("Coordinator rejected authentication")
                if response.status == 409:
                    raise CoordinatorConflict("Schedule changed; refresh and retry the intended action")
                if response.status >= 300:
                    raise CoordinatorError(f"Coordinator request failed (HTTP {response.status})")
                content = bytearray()
                while chunk := await response.content.read(65536):
                    content.extend(chunk)
                    if len(content) > 2_000_000:
                        raise CoordinatorError("Coordinator response exceeds size limit")
                try:
                    data = json.loads(content)
                except (ValueError, UnicodeError):
                    raise CoordinatorError("Coordinator returned invalid JSON") from None
                if not isinstance(data, dict):
                    raise CoordinatorError("Coordinator returned an unexpected response")
                return data

    async def schedules(self):
        data = await self._request("GET", "/v1/schedules")
        if not isinstance(data.get("schedules"), list) or not isinstance(data.get("events"), list):
            raise CoordinatorError("Coordinator schedule response is incompatible")
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
               or type(row.get("revision")) is not int for row in data["schedules"]):
            raise CoordinatorError("Coordinator schedule records are incompatible")
        rooms = data.get("rooms", [])
        if not isinstance(rooms, list) or any(not isinstance(room, dict) or not isinstance(room.get("id"), str)
                                            or not isinstance(room.get("name"), str) for room in rooms):
            raise CoordinatorError("Coordinator room metadata is incompatible")
        return data

    async def dismiss_ringing(self, request_id, expected):
        result = await self._request('POST','/v1/schedules/dismiss_ringing',
                                     {'request_id':request_id,'expected':expected})
        decision=result.get('ringing_decision')
        if (not isinstance(decision,dict) or type(decision.get('dismissed_count')) is not int
                or not isinstance(decision.get('dismissed_ids'),list)
                or decision['dismissed_count']!=len(decision['dismissed_ids'])
                or decision.get('no_ringing_at_decision') is not (decision['dismissed_count']==0)):
            raise CoordinatorError('Ringing decision incompatible; do not stop media or retry')
        return result

    async def mutate(self, service, data):
        payload = validate_payload(service, data)
        payload.pop("config_entry_id", None)
        path = {"create_timer": "timer", "create_alarm": "alarm", "command": "command"}[service]
        result = await self._request("POST", "/v1/schedules/" + path, payload)
        schedule = result.get("schedule")
        if not isinstance(schedule, dict) or not isinstance(schedule.get("id"), str) or type(schedule.get("revision")) is not int:
            raise CoordinatorError("Coordinator mutation response is incompatible; refresh before retrying")
        return result

    async def upload_capture(self, captured):
        """Only the dedicated STT wrapper calls this; never expose raw audio to LLMs."""
        result = await self._request('POST', '/v1/captures', {
            'profile_id': captured.profile_id, 'source_device': captured.source_device,
            'nonce': captured.nonce, 'wav_base64': captured.wav_base64, 'transcript': captured.transcript})
        row = result.get('recording')
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not re.fullmatch(r'[a-f0-9]{32}', row['id']):
            raise CoordinatorError('Coordinator capture response is incompatible; retry only with the same nonce')
        return row

    async def resolve_recording(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier):
            raise CoordinatorError('Invalid recording identifier')
        result = await self._request('GET', '/v1/recordings/' + identifier)
        path = result.get('audio_path')
        if not isinstance(path, str) or not re.fullmatch(r'/audio/'+identifier+r'\?expires=[0-9]+&sig=[a-f0-9]{64}', path):
            raise CoordinatorError('Coordinator returned an invalid audio capability')
        # The configured backend origin is authoritative; never follow a returned URL.
        return self.url + path

    async def runtime_config(self):
        return await self._request('GET','/v1/runtime')

    async def research(self, operation, payload):
        if operation not in ('places','forecast','page','recipe_search'):
            raise CoordinatorError('Unknown research operation')
        return await self._request('POST','/v1/research/'+operation,payload)

    async def configure_runtime(self, configuration, expected_revision):
        return await self._request('POST','/v1/runtime',{**configuration,'expected_revision':expected_revision})

    async def claim_ring(self):
        return await self._request('POST','/v1/ring/claim',{})

    async def validate_ring(self, occurrence_id, target, lease):
        return await self._request('POST','/v1/ring/validate',dict(occurrence_id=occurrence_id,target=target,lease=lease))

    async def ack_ring(self, occurrence_id, target, lease, outcome):
        return await self._request('POST','/v1/ring/ack',dict(occurrence_id=occurrence_id,target=target,lease=lease,outcome=outcome))

    async def reserve_ring_fallback(self, occurrence_id, target, lease, actual_target):
        return await self._request('POST','/v1/ring/fallback',dict(occurrence_id=occurrence_id,target=target,lease=lease,actual_target=actual_target))

    async def enqueue_broadcast(self, recording_id, request_id, targets):
        return await self._request('POST','/v1/broadcasts/enqueue',dict(recording_id=recording_id,request_id=request_id,targets=targets))

    async def pull_broadcast(self):
        return await self._request('POST','/v1/broadcasts/pull',{})

    async def validate_broadcast(self, identifier, lease):
        return await self._request('POST','/v1/broadcasts/validate',dict(identifier=identifier,lease=lease))

    async def ack_broadcast(self, identifier, lease, outcome):
        return await self._request('POST','/v1/broadcasts/ack',dict(identifier=identifier,lease=lease,outcome=outcome))

    async def broadcast_status(self, broadcast_id):
        return await self._request('POST','/v1/broadcasts/status',dict(broadcast_id=broadcast_id))

    async def cancel_broadcast(self, broadcast_id):
        return await self._request('POST','/v1/broadcasts/cancel',dict(broadcast_id=broadcast_id))
