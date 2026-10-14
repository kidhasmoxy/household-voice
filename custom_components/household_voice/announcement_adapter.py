"""Configured HA announcement transport, disabled unless explicitly enabled.

BroadcastRuntime binds this adapter to the durable outbox, resolves fresh signed
original-WAV URLs, and persists each target outcome. The shared MA transport
awaits the same native play_announcement operation used by the HA service.
HA/MA service acceptance is not proof of audible/phone delivery.
Options are an immutable snapshot: rebuild this adapter when configuration changes.
An unknown service outcome must not be retried automatically; acceptance may have
occurred before the await failed. Phone notifications intentionally disclose the
transcript to the configured notification transport/phone, with no audio URL,
attachment, or click action. They are not a local-only privacy guarantee.
"""
from copy import deepcopy
import math
import re
import time
from urllib.parse import urlsplit

TRANSPORT_POLICIES=frozenset(('legacy_ma','verified_native'))

def validate_transport_policy(value):
    if not isinstance(value,str) or value not in TRANSPORT_POLICIES:
        raise ValueError('Invalid internal announcement transport policy')
    return value

def transport_policy_from_options(options):
    """Entry-backed immutable option snapshots only; not a new user setting."""
    flag=options.get('native_recording_enabled',False)
    if type(flag) is not bool:raise ValueError('Invalid native recording setting')
    return 'verified_native' if flag else 'legacy_ma'


class AnnouncementAdapter:
    def __init__(self, hass, options, resolve_audio, *, allowed_audio_origins,
                 execution_authorized=False, presence_freshness=300, transport=None,
                 allowed_audio_path_prefix='/audio/', focus_entry=None,transport_policy=None,
                 require_recording_provenance=False):
        self.hass, self.options, self.resolve_audio = hass, deepcopy(options), resolve_audio
        self.allowed_audio_origins = frozenset(allowed_audio_origins)
        if not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)*audio/', allowed_audio_path_prefix):
            raise ValueError('Invalid configured audio path')
        self.allowed_audio_path_prefix = allowed_audio_path_prefix
        self.execution_authorized = execution_authorized is True
        if type(presence_freshness) not in (int,float) or not math.isfinite(presence_freshness) or not 30<=presence_freshness<=3600:
            raise ValueError('Invalid presence freshness')
        self.presence_freshness=presence_freshness
        self.presence_policy = self.options.get('phone_presence_policy', 'recent_five_minutes')
        if self.presence_policy not in ('current_state','recent_hour','recent_five_minutes'):
            raise ValueError('Invalid phone presence policy')
        self.transport = transport
        self.focus_entry = focus_entry
        self.transport_policy=validate_transport_policy(transport_policy if transport_policy is not None else transport_policy_from_options(self.options))
        self.require_recording_provenance=require_recording_provenance is True
        self._strict_transport=None

    def targets(self, target_rooms=None, include_home_phones=True):
        """Rooms come from a request; entities/services only from saved options."""
        if type(include_home_phones) is not bool:
            raise ValueError('Choose whether to include phones')
        if target_rooms is None:
            speakers=self.options.get('default_announcement_speakers',[])
        else:
            if not isinstance(target_rooms,list) or not target_rooms or any(not isinstance(r,str) for r in target_rooms):
                raise ValueError('Choose valid announcement rooms')
            speakers=[]
            for room in target_rooms:
                # The room's MA endpoint is preferred. Native Sonos and its MA
                # representation are never implicitly treated as two targets.
                matches={s['music_player'] for s in self.options.get('media_rooms',[]) if s['room_id']==room}
                if not matches:
                    matches={s['tts_media_player'] for s in self.options.get('satellites',[]) if s['room_id']==room}
                if not matches:
                    raise ValueError('Unknown room; no announcement will be sent')
                speakers.extend(matches)
        if not speakers or any(not re.fullmatch(r'media_player\.[a-z0-9_]+',s) for s in speakers):
            raise ValueError('Configure default announcement speakers first')
        result=[{'kind':'speaker','target':speaker} for speaker in sorted(set(speakers))]
        if include_home_phones:
            for phone in self.options.get('phones',[]):
                if phone.get('enabled') is True:
                    result.append({'kind':'phone','target':phone['notify_service'],'device_tracker':phone['device_tracker']})
        # Exact duplicate endpoints only; physical MA/Sonos equivalence must be
        # reviewed administratively and is never guessed from entity names.
        result=list({(x['kind'],x['target']):x for x in result}.values())
        if (self.execution_authorized and self.options.get('live_actions_enabled') is True
                and self.options.get('announcements_enabled') is True):
            self.preflight_targets(result)
        return result

    def preflight_targets(self,targets):
        """Revalidate the frozen destination list; never expand or replace it."""
        if not isinstance(targets,list) or not targets:raise ValueError('Announcement targets are unverified')
        for target in targets:
            if not isinstance(target,dict) or target.get('kind') not in ('speaker','phone'):
                raise ValueError('Announcement targets are unverified')
            if target['kind']=='speaker':self._preflight(target['target'])

    def _transport_for(self,policy):
        validate_transport_policy(policy)
        allowed=set(self.options.get('default_announcement_speakers',[]))
        allowed.update(row['tts_media_player'] for row in self.options.get('satellites',[]))
        allowed.update(row['music_player'] for row in self.options.get('media_rooms',[]))
        def build(mode):
            from .satellite_transport import MAAnnouncementTransport
            return MAAnnouncementTransport(self.hass,allowed,timeout=60,
                spoken_media_policy=self.options.get('spoken_media_policy','pause_and_resume'),transport_policy=mode)
        if self.transport is None:self.transport=build(self.transport_policy)
        actual=getattr(self.transport,'transport_policy',self.transport_policy)
        validate_transport_policy(actual)
        if policy=='verified_native' and actual!='verified_native':
            if self._strict_transport is None:self._strict_transport=build('verified_native')
            return self._strict_transport
        return self.transport

    def _recording_policy(self,recording):
        provenance=recording.get('transport_policy')
        if provenance is None:
            if self.require_recording_provenance:raise ValueError('Recording origin is unverified')
            return self.transport_policy # Trusted legacy remote/injected contract; native cannot run remotely.
        validate_transport_policy(provenance)
        return 'verified_native' if 'verified_native' in (self.transport_policy,provenance) else 'legacy_ma'

    def _preflight(self,player,policy=None):
        """Destination authorization before a question or any queue/provider action."""
        preflight=getattr(self._transport_for(policy or self.transport_policy),'preflight',None)
        if not callable(preflight):raise ValueError('Announcement destination scope is unverified')
        return preflight(player)

    def _home_now(self, tracker, now):
        state=self.hass.states.get(tracker)
        if state is None or state.state!='home':
            return False
        if self.presence_policy == 'current_state':
            return True
        reported=getattr(state,'last_reported',None)
        if reported is None:
            return False
        try:
            age=now-reported.timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return False
        freshness = 3600 if self.presence_policy == 'recent_hour' else self.presence_freshness
        return 0<=age<=freshness

    async def deliver(self, target, recording, *, now, before_dispatch=None):
        """Called once for a leased target against this configuration snapshot."""
        started=time.monotonic()
        if type(now) not in (int,float) or not math.isfinite(now):
            raise ValueError('Invalid dispatch time')
        if not isinstance(recording,dict) or not isinstance(recording.get('id'),str) or not re.fullmatch(r'[a-f0-9]{32}',recording['id']):
            raise ValueError('Invalid stored recording')
        if type(recording.get('expires_at')) not in (float,int) or not math.isfinite(recording['expires_at']) or recording['expires_at']<=now:
            return {'outcome':'expired','service_accepted':False}
        configured=[{'kind':'speaker','target':speaker} for speaker in self.options.get('default_announcement_speakers',[])]
        configured += [{'kind':'phone','target':p['notify_service'],'device_tracker':p['device_tracker']}
                       for p in self.options.get('phones',[]) if p.get('enabled') is True]
        # Explicit room targets may be outside the default group, but still must
        # be an exact configured satellite speaker.
        configured += [{'kind':'speaker','target':s['tts_media_player']} for s in self.options.get('satellites',[])]
        configured += [{'kind':'speaker','target':s['music_player']} for s in self.options.get('media_rooms',[])]
        if target not in configured:
            raise ValueError('Target is no longer configured')
        if target['kind']=='phone' and not self._home_now(target['device_tracker'],now):
            return {'outcome':'skipped_not_home_or_stale','service_accepted':False}
        if not (self.execution_authorized and self.options.get('live_actions_enabled') is True
                and self.options.get('announcements_enabled') is True):
            return {'outcome':'dry_run','service_accepted':False,'kind':target['kind'],'target':target['target']}
        if target['kind']=='speaker':
            try:
                policy=self._recording_policy(recording)
                delivery_transport=self._transport_for(policy)
                self._preflight(target['target'],policy)
            except ValueError:
                return {'outcome':'not_invoked','service_accepted':False,'provider_completed':False,
                    'delivery_confirmed':False,'reason':'announcement_destination_unsafe_unavailable_or_held'}
            # URL is obtained internally at dispatch, never accepted as model input
            # and never included in this method's outcome or diagnostic messages.
            try:
                url=await self.resolve_audio(recording['id'])
            except Exception:
                # Exceptions from clients may include signed URLs, response bodies,
                # or transcripts. Keep them out of outbox/diagnostic payloads.
                return {'outcome':'audio_resolution_failed','service_accepted':False,'delivery_confirmed':False}
            if not isinstance(url,str) or len(url)>4096 or any(ord(c)<33 for c in url) or '\\' in url:
                raise ValueError('Invalid resolved audio URL')
            try:
                parsed=urlsplit(url)
            except ValueError:
                raise ValueError('Invalid resolved audio URL') from None
            origin=f'{parsed.scheme}://{parsed.netloc}'
            if parsed.scheme not in ('http','https') or parsed.username or parsed.password or parsed.fragment or origin not in self.allowed_audio_origins:
                raise ValueError('Audio URL is outside the configured origin')
            if parsed.path!=self.allowed_audio_path_prefix+recording['id']:
                raise ValueError('Audio URL does not identify the recording')
            player_state=self.hass.states.get(target['target'])
            if player_state is None or player_state.state in ('unknown','unavailable'):
                return {'outcome':'unavailable','service_accepted':False}
            volume=self.options.get('announcement_volume',0.5)
            chime=self.options.get('announcement_chime','none')
            if type(volume) not in (int,float) or not math.isfinite(volume) or not 0<=volume<=1 or chime not in ('none','default'):
                raise ValueError('Invalid announcement volume or chime')
            domain,service='music_assistant','play_announcement'
        else:
            domain,service=target['target'].split('.',1)
            if domain!='notify' or not re.fullmatch(r'[a-z0-9_]+',service):
                raise ValueError('Invalid configured phone service')
            transcript=recording.get('transcript','')
            if not isinstance(transcript,str) or not transcript.strip():
                return {'outcome':'transcript_unavailable','service_accepted':False}
            if len(transcript)>8000:
                raise ValueError('Invalid announcement transcript')
            data={'title':'Home announcement','message':transcript}
        if target['kind']=='phone' and not self.hass.services.has_service(domain,service):
            return {'outcome':'unavailable','service_accepted':False}
        if before_dispatch is not None and not await before_dispatch():
            return {'outcome':'lease_invalid','service_accepted':False}
        # Recheck phone presence immediately before dispatch and advance the
        # supplied dispatch timestamp by monotonic elapsed processing time.
        dispatch_now=now+max(0,time.monotonic()-started)
        if recording['expires_at']<=dispatch_now:
            return {'outcome':'expired','service_accepted':False}
        if target['kind']=='phone' and not self._home_now(target['device_tracker'],dispatch_now):
            return {'outcome':'skipped_not_home_or_stale','service_accepted':False}
        try:
            if target['kind']=='speaker':
                self._preflight(target['target'],policy)
                if self.focus_entry is not None:
                    from .room_focus import announcement_room_focus
                    async with announcement_room_focus(self.hass, self.focus_entry, target['target']):
                        return await delivery_transport.play(target['target'],url,volume=volume,chime=chime=='default',before_dispatch=before_dispatch)
                return await delivery_transport.play(target['target'],url,volume=volume,chime=chime=='default',before_dispatch=before_dispatch)
            await self.hass.services.async_call(domain,service,data,blocking=True)
        except Exception:
            # A transport failure can happen after HA accepted the call. Do not
            # assert non-delivery or allow blind retries that duplicate audio.
            return {'outcome':'service_outcome_unknown','service_accepted':None,'delivery_confirmed':False}
        return {'outcome':'service_accepted','service_accepted':True,'delivery_confirmed':False}
