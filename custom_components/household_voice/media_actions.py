"""Pairing-bound native media controls; no text routing or arbitrary services."""
import asyncio
from copy import deepcopy
import hashlib
import json

import probatio

from .media_controls import CurrentMedia, ControlError, _result
from .media_llm import _resolve, _text, _GuardedServices, llm
from .llm_compat import tool_result
from .intent_scope import constrain_arguments, ScopeConflict
from .room_focus import UserControlError

NAME='household_media_control'
OPERATIONS=('next','previous','volume_set','volume_up','volume_down','mute','unmute')


class PairedControl(CurrentMedia):
    def feature(self, operation):
        from homeassistant.components.media_player import MediaPlayerEntityFeature as F
        row,_,_,enabled=self.binding()
        state=self.api.hass.states.get(row['music_player'])
        required={'next':F.NEXT_TRACK,'previous':F.PREVIOUS_TRACK,
                  'volume_set':F.VOLUME_SET,'volume_up':F.VOLUME_SET,'volume_down':F.VOLUME_SET,
                  'mute':F.VOLUME_MUTE,'unmute':F.VOLUME_MUTE}[operation]
        if state is None or state.state in ('unknown','unavailable'):
            raise ControlError('media_player_unavailable')
        supported=state.attributes.get('supported_features')
        if type(supported) is not int or supported & required != required:
            raise ControlError('media_control_not_supported')
        return row['music_player'],state,enabled

    @staticmethod
    def skip_uri(snapshot):
        player,queue=snapshot
        if (not queue or queue.get('queue_id') != player.get('player_id')
                or player.get('playback_state') != 'playing'
                or type(queue.get('is_dynamic')) is not bool):
            raise ControlError('active_own_music_queue_required')
        # Skipping a verified music item is valid in an endless mix. The stricter
        # queue-options guard rejects dynamic shuffle/repeat changes; retain all
        # its content checks while permitting this separate next/previous action.
        checked=deepcopy(queue);checked['is_dynamic']=False
        try:
            return CurrentMedia.protected_content_check((player,checked))
        except ControlError as exc:
            if str(exc)=='spoken_or_unknown_content_queue_options_blocked':
                raise ControlError('spoken_or_unknown_content_skip_blocked') from None
            raise

    async def skip(self, operation):
        before=await self.snapshot()
        uri=self.skip_uri(before)
        guard=_GuardedServices(self.api,self.context,self.target,self.signature)
        if await guard.async_authorize_provider_uri(uri) is not True:
            raise ControlError('current_media_not_available_to_configured_profile')
        fresh=await self.snapshot();self.skip_uri(fresh)
        if self.identity(before)!=self.identity(fresh):
            raise ControlError('media_changed_before_control')
        entity,_,enabled=self.feature(operation)
        if not enabled or self.api.execution_authorized is False:
            return _result('dry_run','media_control_execution_disabled')
        service='media_next_track' if operation=='next' else 'media_previous_track'
        if not self.api.hass.services.has_service('media_player',service):
            raise ControlError('media_control_not_supported')
        # HA services do not offer atomic queue-generation compare-and-swap.
        # Validate immediately before dispatch and never replay an unknown result.
        self.dispatched=True
        await self.api.hass.services.async_call('media_player',service,{'entity_id':entity},
            blocking=True,context=self.context.context)
        after=await self.snapshot()
        if (after[0].get('active_source')!=fresh[0].get('active_source')
                or (after[1] or {}).get('queue_id')!=fresh[1]['queue_id']):
            raise ControlError('media_changed_during_control')
        result=_result('action_accepted','media_control_requested',operation=operation)
        result['executed']=True
        return result

    async def volume(self,args):
        operation=args['operation']
        original_row, original_id, original_client, _ = self.binding()
        original_entry = original_row['config_entry_id']
        original_entity = original_row['music_player']
        def bound():
            row, native_id, client, enabled = self.binding()
            if (row['music_player'] != original_entity or row['config_entry_id'] != original_entry
                    or native_id != original_id or client is not original_client):
                raise ControlError('media_configuration_changed')
            return enabled
        def transport_clear():
            # The transport hold outlives a focus lease and survives MA reloads.
            # It must never be cleared or bypassed by a volume request.
            from .satellite_transport import _UNCERTAIN, _LOCKS
            key = (original_entry, original_id)
            lock = _LOCKS.get(self.api.hass, {}).get(key)
            if key in _UNCERTAIN.get(self.api.hass, {}) or lock is not None and lock.locked():
                raise ControlError('room_audio_ownership_unavailable')
        bound(); transport_clear()
        await self.snapshot() # Initial topology, never catalog lookup.
        bound(); transport_clear()
        entity,_,enabled=self.feature(operation)
        if not enabled or self.api.execution_authorized is False:
            return _result('dry_run','media_control_execution_disabled')
        from .area_registry import collect_pairings
        from .room_focus import get_room_focus_state, RoomVolumeController, AnnouncementRoomFocus
        output=collect_pairings(self.api.hass,self.api.entry.options)['outputs'].get(entity)
        if output is None:
            raise ControlError('verified_media_player_required')
        aliases=set(output.get('physical_aliases',()))|{entity}
        focus=get_room_focus_state(self.api.hass,self.api.entry)
        authority=AnnouncementRoomFocus(focus,RoomVolumeController(self.api.hass,focus,self.context.context))
        async def before_dispatch(mark):
            bound(); transport_clear()
            # Room-lock/journal awaits may outlive cached HA/MA topology. Read
            # the original native player last, then perform only synchronous
            # admission checks before the exact-entity service is invoked.
            player = await original_client.send_command('players/get', player_id=original_id)
            bound(); transport_clear()
            if not isinstance(player, dict) or player.get('player_id') != original_id:
                raise ControlError('verified_media_player_required')
            if player.get('available') is not True:
                raise ControlError('media_player_unavailable')
            members = player.get('group_members') or []
            if (not isinstance(members, list) or any(member != original_id for member in members)
                    or player.get('synced_to') not in (None, '', original_id)
                    or player.get('active_group') not in (None, '', original_id)):
                raise ControlError('grouped_media_target_requires_clarification')
            current,_,allowed=self.feature(operation)
            if current!=entity or not allowed or self.api.execution_authorized is False:
                raise ControlError('media_configuration_changed')
            if (focus.failed or any(focus.protected_players.get(p) or p in focus.blocked for p in aliases)):
                raise ControlError('room_audio_ownership_unavailable')
            if mark:self.dispatched=True
        if operation in ('mute','unmute'):
            async def change():
                await before_dispatch(False)
                if not self.api.hass.services.has_service('media_player','volume_mute'):
                    raise ControlError('media_control_not_supported')
                await before_dispatch(True)
                desired=operation=='mute'
                await self.api.hass.services.async_call('media_player','volume_mute',
                    {'entity_id':entity,'is_volume_muted':desired},blocking=True,context=self.context.context)
                _,state,_=self.feature(operation)
                if state.attributes.get('is_volume_muted') is not desired:
                    raise ControlError('media_control_readback_unconfirmed')
                return {'status':'action_accepted','reason':'media_mute_verified',
                    'settings_confirmed':True,'is_volume_muted':desired}
            values=await authority.user_control(entity,aliases,change)
        else:
            values=await authority.user_volume(entity,aliases,
                target=args['volume_percent']/100 if operation=='volume_set' else None,
                delta=(args.get('volume_delta',5)/100)*(1 if operation=='volume_up' else -1) if operation!='volume_set' else None,
                before_dispatch=before_dispatch)
        result=_result(values.pop('status'),values.pop('reason'),**values)
        result['executed']=self.dispatched
        return result


class PairedMediaControlTool(llm.Tool):
    name=NAME
    description=('Control the assistant\'s paired Music Assistant speaker: next/previous music track, '
        'volume_set (volume_percent 0–100), volume_up/down (optional volume_delta 1–100, default 5 percentage points), '
        'mute/unmute. Omit target_room for this assistant; only specify an explicitly requested other room. '
        'No player IDs or arbitrary device targets. Next/previous cannot skip books or stories. '
        'Volume changes preserve the requested baseline while wake-word ducking finishes. '
        'Report unsupported, protected or uncertain results; never retry uncertain controls.')

    def __init__(self,api):
        self.api=api
        self.parameters=probatio.Schema({probatio.Required('operation'):probatio.In(OPERATIONS),
            probatio.Optional('target_room'):str,probatio.Optional('volume_percent'):probatio.All(int,probatio.Range(min=0,max=100)),
            probatio.Optional('volume_delta'):probatio.All(int,probatio.Range(min=1,max=100))})
        if hasattr(llm,'ToolAnnotations'):
            self.annotations=llm.ToolAnnotations(read_only=False,destructive=False,idempotent=False,open_world=False)

    async def async_call(self,hass,tool_input,context):
        if (self.api.entry.options.get('assist_tools_enabled') is not True
                or self.api.entry.options.get('media_enabled') is not True
                or getattr(self.api.entry,'disabled_by',None) is not None):
            return tool_result(llm,data=_result('disabled','media_controls_disabled'),error=True)
        try:
            args=deepcopy(tool_input.tool_args)
            if not isinstance(args,dict) or set(args)-{'operation','target_room','volume_percent','volume_delta'} or args.get('operation') not in OPERATIONS:
                raise ValueError()
            operation=args['operation']
            if (not _text(tool_input.id,512) or 'target_room' in args and not _text(args['target_room'])
                    or (operation=='volume_set') != ('volume_percent' in args)
                    or 'volume_percent' in args and (type(args['volume_percent']) is not int or not 0<=args['volume_percent']<=100)
                    or 'volume_delta' in args and (operation not in ('volume_up','volume_down') or type(args['volume_delta']) is not int or not 1<=args['volume_delta']<=100)):
                raise ValueError()
            self.parameters(args)
            args=constrain_arguments('media','control',args,context)
            _,_,config_signature,_=_resolve(hass,self.api.entry,context,args.get('target_room'))
        except ScopeConflict:
            return tool_result(llm,data=_result('clarify','tool_does_not_match_requested_category'))
        except Exception:
            return tool_result(llm,data=_result('clarify','valid_arguments_and_verified_media_configuration_required'))
        key=hashlib.sha256(json.dumps([self.api.entry.entry_id,context.device_id,
            getattr(context.context,'id',None),getattr(context.context,'user_id',None),tool_input.id],allow_nan=False).encode()).hexdigest()
        signature=(hashlib.sha256(json.dumps([self.name,args],sort_keys=True,allow_nan=False).encode()).hexdigest(),config_signature)
        cached=self.api.completed.get(key);pending=self.api.pending.get(key)
        if cached or pending:
            found=cached or pending
            if found[0]!=signature:
                return tool_result(llm,data=_result('error','tool_call_id_reused_or_configuration_changed'),error=True)
            result,error=(cached[1],cached[2]) if cached else await asyncio.shield(pending[1])
            return tool_result(llm,data=deepcopy(result),error=error)
        if len(self.api.pending)>=256:
            return tool_result(llm,data=_result('error','media_controls_busy'),error=True)
        self.api.pending[key]=(signature,asyncio.get_running_loop().create_future())
        control=PairedControl(self.api,context,args.get('target_room'),config_signature)
        error=False
        try:
            async with self.api.call_lock:
                result=await control.skip(operation) if operation in ('next','previous') else await control.volume(args)
                error=result['status']=='error'
        except asyncio.CancelledError:
            self.api.complete_call(key,signature,_result('error','media_control_result_uncertain',mutation_dispatched=control.dispatched),True)
            raise
        except Exception as exc:
            error=True
            reason='media_control_result_uncertain' if control.dispatched else str(exc) if isinstance(exc,(ControlError,UserControlError)) else 'media_control_unavailable'
            result=_result('error',reason,mutation_dispatched=control.dispatched)
        self.api.complete_call(key,signature,result,error)
        return tool_result(llm,data=result,error=error)
