"""Concrete Music Assistant announcement transport using its loaded HA entry.

HA 2026.10.1 MusicAssistantPlayer._async_handle_play_announcement delegates to
entry.runtime_data.mass.players.play_announcement. This same supported client API
is used here to retain its full await rather than infer completion from HA state.
MA 2.10.6 c24866405bd2068c25a60e1014e5ef8c3f6445bb awaits its announcement workflow;
Sonos uses calculated clip duration, not acoustic feedback. provider_completed
therefore means the MA workflow returned. delivery_confirmed is ALWAYS False.
Cancelling this coroutine does not prove a remote clip stopped. MA exposes no
announcement-ID-specific stop through this API; never substitute generic stop.

Preflight uses public client Player/OutputProtocol fields (client1.5.1 models1.1.189),
not server-only properties. verified_native requires a sole available native
output on an ungrouped physical player; aggregated features alone cannot prove
native support. legacy_ma preserves standard MA group/linked/fallback routing,
without an exact physical-room scope guarantee. The policy is fixed at creation.
Spoken queue IDs must match the exact native destination. These repeated cached
checks are not atomic server topology/queue ownership or acoustic completion.
"""
from __future__ import annotations

import asyncio
import math
import weakref

from homeassistant.helpers import entity_registry as er
from homeassistant.core import callback
from .announcement_adapter import transport_policy_from_options,validate_transport_policy

_LOCKS = weakref.WeakKeyDictionary()
_SPOKEN_GUARDS = weakref.WeakKeyDictionary()
_UNCERTAIN = weakref.WeakKeyDictionary()



def _player_key(hass,player_id,native_id):
    # MA reconnect/reload must not erase uncertainty about remote audio.
    entity=er.async_get(hass).async_get(player_id)
    return (entity.config_entry_id,native_id)


def reset_uncertain_audio(hass, player_id, *, operator_confirmed_stopped=False):
    """Administrative acknowledgement only; never issue or prove a physical stop."""
    if operator_confirmed_stopped is not True:
        raise AudioTransportError('Confirm the previous audio has stopped before recovery')
    mass,native_id=MAAnnouncementTransport(hass,{player_id}).resolve(player_id)
    key=_player_key(hass,player_id,native_id)
    return reset_uncertain_audio_identity(hass, provider_entry_id=key[0],
        native_player_id=key[1], operator_confirmed_stopped=operator_confirmed_stopped)


def _uncertain_identity(provider_entry_id, native_player_id):
    if (not isinstance(provider_entry_id, str) or not 1 <= len(provider_entry_id) <= 128
            or not isinstance(native_player_id, str) or not 1 <= len(native_player_id) <= 512
            or any(ord(c) < 32 for c in provider_entry_id + native_player_id)):
        raise AudioTransportError('Invalid stored player identity')
    return provider_entry_id, native_player_id


def restore_uncertain_audio(hass, *, provider_entry_id, native_player_id):
    """Restore a trusted durable hold, even with an offline or renamed HA entity.

    Only the receiver's local ledger calls this; no model or HTTP identifiers are
    accepted here. The MA config-entry/native-player tuple is the transport key.
    """
    key = _uncertain_identity(provider_entry_id, native_player_id)
    _UNCERTAIN.setdefault(hass, {})[key] = True
    return True


def reset_uncertain_audio_identity(hass, *, provider_entry_id, native_player_id,
                                   operator_confirmed_stopped=False):
    """Admin recovery for a trusted saved identity; never issue a physical stop."""
    if operator_confirmed_stopped is not True:
        raise AudioTransportError('Confirm the previous audio has stopped before recovery')
    key = _uncertain_identity(provider_entry_id, native_player_id)
    lock=_LOCKS.get(hass,{}).get(key)
    if lock is not None and lock.locked():
        raise AudioTransportError('Cannot recover while a local announcement is active')
    cleared=_UNCERTAIN.get(hass,{}).pop(key,None) is not None
    return {'cleared':cleared,'physical_stop_confirmed':False,'operator_confirmation_required':True}


def invalidate_spoken_resume(hass, player_id):
    """Call before a direct, user-requested MA control mutation (not announcements)."""
    for guard in tuple(_SPOKEN_GUARDS.get(hass, ())):
        if player_id in (guard.entity_id,guard.native_id):
            guard.invalid = True


class _SpokenGuard:
    def __init__(self,hass,mass,entity_id,native_id,queue_id):
        self.hass,self.mass,self.entity_id,self.native_id,self.queue_id=hass,mass,entity_id,native_id,queue_id
        self.invalid=False
        self.baseline=None
        self.removers=[]
        self.observed=False

    @staticmethod
    def snapshot(queue):
        item=getattr(queue,'current_item',None)
        elapsed=getattr(queue,'elapsed_time',None)
        if item is None or type(elapsed) not in (int,float) or not math.isfinite(elapsed):
            return None
        return (queue.queue_id,item.queue_item_id,getattr(queue.state,'value',queue.state),
                float(elapsed),getattr(queue,'current_index',None),getattr(queue,'items',None),
                getattr(queue,'shuffle_enabled',None),str(getattr(queue,'repeat_mode',None)),
                getattr(queue,'playback_speed',None))

    def start(self):
        _SPOKEN_GUARDS.setdefault(self.hass,set()).add(self)
        try:
            self.removers.append(self.mass.subscribe(self._ma_event,id_filter=(self.queue_id,self.native_id)))
            self.removers.append(self.hass.bus.async_listen('call_service',self._ha_service))
            self.observed=all(callable(remover) for remover in self.removers)
        except Exception:
            self.observed=False  # Pause still protects progress; unsafe resume is skipped.

    @callback
    def _ha_service(self,event):
        data=event.data
        domain,service=data.get('domain'),data.get('service')
        controls={'media_pause','media_stop','media_play','media_play_pause','media_seek',
                  'media_next_track','media_previous_track','play_media','clear_playlist',
                  'shuffle_set','repeat_set','turn_off','join','unjoin'}
        # MA reads (search/get_queue/get_library) are not user playback changes.
        if not ((domain=='music_assistant' and service in {'play_media','play_announcement','transfer_queue'})
                or (domain=='media_player' and service in controls)):
            return
        target=data.get('service_data') or {}
        entities=target.get('entity_id')
        if entities is None or entities=='all' or self.entity_id in (entities if isinstance(entities,list) else [entities]):
            self.invalid=True

    def _ma_event(self,event):
        if self.baseline is None:
            return
        kind=getattr(event.event,'value',event.event)
        if event.object_id!=self.queue_id:
            return
        if kind=='queue_time_updated':
            try:
                if abs(float(event.data)-self.baseline[3])>.25: self.invalid=True
            except (TypeError,ValueError): self.invalid=True
        elif kind in ('queue_updated','queue_added'):
            data=event.data
            if not isinstance(data,dict): self.invalid=True; return
            current_item=data.get('current_item') or {}
            observed=(data.get('queue_id'),current_item.get('queue_item_id'),data.get('state'),
                data.get('elapsed_time'),data.get('current_index'),data.get('items'),
                data.get('shuffle_enabled'),str(data.get('repeat_mode')),data.get('playback_speed'))
            if observed!=self.baseline: self.invalid=True

    def close(self):
        _SPOKEN_GUARDS.get(self.hass,set()).discard(self)
        for remover in self.removers:
            if callable(remover):
                try: remover()
                except Exception: pass


class AudioTransportError(ValueError):
    """Sanitized routing failure without media URLs or underlying credentials."""


class MAAnnouncementTransport:
    def __init__(self, hass, allowed_players, *, timeout=125, spoken_media_policy='pause_and_resume',transport_policy='verified_native'):
        self.hass = hass
        self.allowed_players = frozenset(allowed_players)
        self.timeout = timeout
        self._transport_policy=validate_transport_policy(transport_policy)
        if spoken_media_policy not in {'provider_default','pause_and_hold','pause_and_resume'}:
            raise AudioTransportError('Invalid spoken-media protection policy')
        self.spoken_media_policy = spoken_media_policy

    @property
    def transport_policy(self):return self._transport_policy

    @staticmethod
    def _value(value):
        return getattr(value,'value',value)

    async def _protect_spoken(self, mass, native_id, player_id, guards):
        """Pause known spoken media, preserving a best-effort intervention guard."""
        self._same_destination(player_id,mass,native_id)
        if self.spoken_media_policy == 'provider_default':
            return 'provider_default'
        queues = getattr(mass,'player_queues',None)
        if queues is None or not callable(getattr(queues,'get_active_queue',None)):
            return 'classification_unavailable_provider_default'
        queue = await queues.get_active_queue(native_id)
        if queue is None or self._value(queue.state) != 'playing':
            return 'no_playing_spoken_queue'
        item = queue.current_item
        if item is None:
            return 'classification_unavailable_provider_default'
        origin = getattr(getattr(item,'origin',None),'container',None)
        kinds = {self._value(getattr(item,'media_type',None)), self._value(getattr(origin,'media_type',None))}
        if not kinds.intersection({'audiobook','podcast','podcast_episode'}):
            return 'non_spoken_queue'
        if queue.queue_id!=native_id:
            raise AudioTransportError('Spoken-media queue is not owned by the announcement destination')
        identity = (queue.queue_id,item.queue_item_id)
        guard=_SpokenGuard(self.hass,mass,player_id,native_id,identity[0])
        guards.append(guard)
        guard.start()
        # Re-resolve immediately before the unconditional public queue pause.
        current = await queues.get_active_queue(native_id)
        if (guard.invalid or current is None or current.current_item is None or
                current.queue_id!=native_id or (current.queue_id,current.current_item.queue_item_id)!=identity or
                self._value(current.state)!='playing'):
            raise AudioTransportError('Spoken-media queue changed before pause')
        self._same_destination(player_id,mass,native_id)
        await queues.pause(identity[0])
        current = await queues.get_active_queue(native_id)
        if (current is None or current.current_item is None or
                (current.queue_id,current.current_item.queue_item_id)!=identity or
                self._value(current.state)!='paused'):
            raise AudioTransportError('Spoken-media pause is unconfirmed')
        guard.baseline=guard.snapshot(current)
        if self.spoken_media_policy=='pause_and_resume':
            return 'paused_pending_guarded_resume'
        return 'paused_explicit_resume_required'

    async def _resume_spoken(self,mass,native_id,guard,before_dispatch):
        if guard.queue_id!=native_id:return 'resume_skipped_queue_owner_changed'
        if guard.invalid or not guard.observed or guard.baseline is None:
            return 'resume_skipped_intervention_or_unavailable_observation'
        current=await mass.player_queues.get_active_queue(native_id)
        if guard.invalid or current is None or guard.snapshot(current)!=guard.baseline:
            return 'resume_skipped_playback_changed'
        if before_dispatch is not None and await before_dispatch() is False:
            return 'resume_skipped_cancelled_or_expired'
        # The lease check can await. Refresh queue identity/state again afterwards.
        current=await mass.player_queues.get_active_queue(native_id)
        if guard.invalid or current is None or guard.snapshot(current)!=guard.baseline:
            return 'resume_skipped_playback_changed'
        try:self._same_destination(guard.entity_id,mass,native_id)
        except AudioTransportError:return 'resume_skipped_announcement_scope_changed'
        # Public MA resume is unconditional: checks are best-effort, never CAS.
        await mass.player_queues.resume(guard.queue_id)
        return 'resume_service_completed'

    def resolve(self, entity_id):
        if entity_id not in self.allowed_players:
            raise AudioTransportError('Speaker is outside the configured routing scope')
        entity = er.async_get(self.hass).async_get(entity_id)
        if entity is None or entity.platform != 'music_assistant' or entity.disabled_by is not None:
            raise AudioTransportError('Select an enabled Music Assistant speaker')
        entry = self.hass.config_entries.async_get_entry(entity.config_entry_id)
        mass = getattr(getattr(entry, 'runtime_data', None), 'mass', None)
        if mass is None or not callable(getattr(getattr(mass, 'players', None), 'play_announcement', None)):
            raise AudioTransportError('Music Assistant is unavailable')
        player = mass.players.get(entity.unique_id)
        if player is None or getattr(player, 'available', False) is not True:
            raise AudioTransportError('Music Assistant speaker is unavailable')
        return mass, entity.unique_id

    def preflight(self,entity_id):
        """Common identity/availability/holds, then immutable route policy.

        HA's ANNOUNCE and MA's final feature set also cover linked/fallback paths.
        verified_native requires the complete published output list to contain
        only native, so feature support cannot come solely from a linked protocol.
        legacy_ma deliberately retains the existing standard MA route.
        This is fresh client validation, not server-side topology CAS.
        """
        mass,native=self.resolve(entity_id)
        player=mass.players.get(native)
        if getattr(player,'player_id',None)!=native:
            raise AudioTransportError('Announcement destination identity is unverified')
        state=self.hass.states.get(entity_id)
        if state is None or state.state in ('unknown','unavailable'):
            raise AudioTransportError('Announcement destination is unavailable')
        if _player_key(self.hass,entity_id,native) in _UNCERTAIN.get(self.hass,{}):
            raise AudioTransportError('Announcement output requires operator recovery')
        if self.transport_policy=='legacy_ma':
            # Preserve standard MA routing, including its group/linked/fallback
            # behavior. This is not an exact physical-room scope guarantee.
            return mass,native
        if (getattr(player,'player_id',None)!=native or self._value(getattr(player,'type',None))!='player'
                or any(not hasattr(player,key) for key in ('group_members','synced_to','active_group','supported_features'))
                or player.synced_to is not None or player.active_group is not None):
            raise AudioTransportError('Announcement destination scope is unverified or grouped')
        members=player.group_members
        features=player.supported_features
        if (not isinstance(members,(list,tuple)) or any(member!=native for member in members)
                or not isinstance(features,(set,frozenset,list,tuple))
                or 'play_announcement' not in {self._value(feature) for feature in features}):
            raise AudioTransportError('Announcement destination has no verified isolated native clip')
        outputs=getattr(player,'output_protocols',None)
        if (not isinstance(outputs,(list,tuple)) or len(outputs)!=1
                or not hasattr(player,'active_output_protocol')
                or player.active_output_protocol not in (None,'native')):
            raise AudioTransportError('Announcement output protocol is ambiguous or unsupported')
        output=outputs[0]
        if (getattr(output,'output_protocol_id',None)!='native' or getattr(output,'is_native',None) is not True
                or getattr(output,'available',None) is not True or not hasattr(output,'derived_from')
                or output.derived_from is not None):
            raise AudioTransportError('Announcement native output is unverified')
        visible=(state.attributes or {}).get('group_members',[])
        if not isinstance(visible,(list,tuple)) or any(member!=entity_id for member in visible):
            raise AudioTransportError('Announcement destination scope is grouped or unknown')
        return mass,native

    def _same_destination(self,entity_id,mass,native):
        current,current_id=self.preflight(entity_id)
        if current is not mass or current_id!=native:
            raise AudioTransportError('Music Assistant routing changed')

    async def play(self, player_id, url, *, volume=.5, chime=False, before_dispatch=None):
        if (not isinstance(url, str) or len(url)>4096 or not url.startswith(('http://','https://'))
                or any(ord(c)<33 for c in url)):
            raise AudioTransportError('Invalid internally resolved announcement URL')
        if type(volume) not in (float,int) or not math.isfinite(volume) or not 0<=volume<=1 or type(chime) is not bool:
            raise AudioTransportError('Invalid announcement playback options')
        # Shared across reply, broadcast and alarm transport instances. Waiting
        # consumes the same bounded deadline; stale work rechecks its lease only
        # after obtaining the lock, immediately before the provider invocation.
        try:mass, native_id = self.preflight(player_id)
        except AudioTransportError:
            return {'outcome':'not_invoked','service_accepted':False,'provider_completed':False,
                    'delivery_confirmed':False,'reason':'announcement_destination_unsafe_unavailable_or_held'}
        key = _player_key(self.hass,player_id,native_id)
        lock = _LOCKS.setdefault(self.hass, {}).setdefault(key, asyncio.Lock())
        quarantines=_UNCERTAIN.setdefault(self.hass,{})
        invoked = False
        completed = False
        spoken = 'not_checked'
        guards=[]
        try:
            async with asyncio.timeout(self.timeout):
                async with lock:
                    if key in quarantines:
                        return {'outcome':'not_invoked','service_accepted':False,
                                'provider_completed':False,'delivery_confirmed':False,
                                'reason':'previous_announcement_outcome_unknown_operator_recovery_required'}
                    self._same_destination(player_id,mass,native_id)
                    if before_dispatch is not None and await before_dispatch() is False:
                        return {'outcome':'not_invoked','service_accepted':False,
                                'provider_completed':False,'delivery_confirmed':False}
                    self._same_destination(player_id,mass,native_id)
                    spoken = await self._protect_spoken(mass,native_id,player_id,guards)
                    # Queue inspection/pause may await remote state. Recheck the
                    # announcement lease; if it expired, leave any book paused.
                    if before_dispatch is not None and await before_dispatch() is False:
                        return {'outcome':'not_invoked','service_accepted':False,
                                'provider_completed':False,'delivery_confirmed':False,
                                'spoken_media':spoken}
                    self._same_destination(player_id,mass,native_id)
                    invoked = True
                    await mass.players.play_announcement(native_id, url, pre_announce=chime,
                                                         volume_level=round(volume*100))
                    completed=True
                    if spoken=='paused_pending_guarded_resume':
                        spoken='resume_outcome_unknown'
                        spoken=await self._resume_spoken(mass,native_id,guards[-1],before_dispatch)
        except asyncio.CancelledError:
            if invoked and not completed:
                quarantines[key]={'entity_id':player_id}
            raise
        except Exception:
            if completed:
                return {'outcome':'service_completed','service_accepted':True,'provider_completed':True,
                        'delivery_confirmed':False,'spoken_media':spoken,
                        'completion_basis':'music_assistant_workflow_returned'}
            if invoked:
                quarantines[key]={'entity_id':player_id}
            return {'outcome':'service_outcome_unknown' if invoked else 'not_invoked','service_accepted':None if invoked else False,
                    'provider_completed':False,'delivery_confirmed':False,'spoken_media':spoken}
        finally:
            for guard in guards: guard.close()
        return {'outcome':'service_completed','service_accepted':True,
                'provider_completed':True,'delivery_confirmed':False,
                'spoken_media':spoken,
                'completion_basis':'music_assistant_workflow_returned'}
