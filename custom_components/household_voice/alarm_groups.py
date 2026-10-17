"""Exact Sonos isolation for MA alarm clips, with durable guarded regrouping.

No snapshot/restore of queues or volumes. Native Sonos unjoin preserves the
remaining group's playback. Group changes use observed-state guards, not CAS.
An uncertain mutation is never replayed automatically.
"""
import asyncio
from copy import deepcopy
import time

from .satellite_transport import AudioTransportError


def sonos_endpoint(hass, player_id):
    """Map one registered MA RINCON player to one native Sonos entity."""
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    from .area_registry import _physical_ids
    registry=er.async_get(hass)
    item=registry.async_get(player_id)
    if item is None or item.platform!='music_assistant' or item.disabled_by is not None:
        return None
    owner=hass.config_entries.async_get_entry(item.config_entry_id)
    mass=getattr(getattr(owner,'runtime_data',None),'mass',None)
    player=mass.players.get(item.unique_id) if mass is not None else None
    identity=getattr(player,'player_id',None)
    if (getattr(owner,'disabled_by',None) is not None or player is None
            or getattr(player,'available',False) is not True
            or identity!=item.unique_id or not isinstance(identity,str)
            or not identity.upper().startswith('RINCON_')):
        return None
    matches=[]
    for row in registry.entities.values():
        if row.platform!='sonos' or row.disabled_by is not None or not row.entity_id.startswith('media_player.'):
            continue
        source=hass.config_entries.async_get_entry(row.config_entry_id)
        device=dr.async_get(hass).async_get(row.device_id)
        if (source is None or getattr(source,'disabled_by',None) is not None
                or getattr(device,'disabled_by',None) is not None):continue
        if _physical_ids(device)==('sonos:'+identity.upper(),):matches.append(row.entity_id)
    return matches[0] if len(matches)==1 else None


class SonosGroups:
    def __init__(self,hass):self.hass=hass

    def endpoint(self,target):return sonos_endpoint(self.hass,target)

    def members(self,entity):
        from homeassistant.helpers import entity_registry as er
        registry=er.async_get(self.hass);row=registry.async_get(entity)
        state=self.hass.states.get(entity)
        if row is None or row.platform!='sonos' or row.disabled_by is not None or state is None or state.state in ('unknown','unavailable'):
            raise AudioTransportError('Native Sonos group unavailable')
        members=state.attributes.get('group_members')
        if not isinstance(members,(list,tuple)) or len(members)>32 or len(set(members))!=len(members):
            raise AudioTransportError('Native Sonos group unverified')
        members=list(members) or [entity]
        if entity not in members:raise AudioTransportError('Native Sonos identity missing from group')
        for member in members:
            other=registry.async_get(member);current=self.hass.states.get(member)
            if (other is None or other.platform!='sonos' or other.disabled_by is not None
                    or other.config_entry_id!=row.config_entry_id or current is None
                    or current.state in ('unknown','unavailable')
                    or list(current.attributes.get('group_members',[])) not in (members,[] if len(members)==1 else members)):
                raise AudioTransportError('Native Sonos group membership changed')
        return members

    def media(self,entity):
        state=self.hass.states.get(entity)
        if state is None:raise AudioTransportError('Native Sonos state unavailable')
        return [state.attributes.get(k) for k in ('media_content_id','source')]

    def isolated(self,target,entity):
        if self.endpoint(target)!=entity or self.members(entity)!=[entity]:return False
        return self.standalone(target)

    def standalone(self,target):
        from homeassistant.helpers import entity_registry as er
        row=er.async_get(self.hass).async_get(target)
        if row is None or row.platform!='music_assistant':return False
        owner=self.hass.config_entries.async_get_entry(row.config_entry_id)
        mass=getattr(getattr(owner,'runtime_data',None),'mass',None)
        player=mass.players.get(row.unique_id) if mass is not None else None
        state=self.hass.states.get(target)
        return (player is not None and getattr(player,'available',False) is True
                and getattr(player,'player_id',None)==row.unique_id
                and all(hasattr(player,key) for key in ('synced_to','active_group','group_members'))
                and isinstance(player.group_members,(list,tuple,set,frozenset))
                and state is not None and state.state not in ('unknown','unavailable')
                and isinstance(state.attributes.get('group_members',[]),(list,tuple))
                and not getattr(player,'synced_to',None) and not getattr(player,'active_group',None)
                and all(p==row.unique_id for p in (getattr(player,'group_members',[]) or []))
                and all(p==target for p in (state.attributes.get('group_members',[]) or [])))

    async def unjoin(self,entity):
        await self.hass.services.async_call('media_player','unjoin',{'entity_id':entity},blocking=True)

    async def join(self,leader,entity):
        await self.hass.services.async_call('media_player','join',{'entity_id':leader,'group_members':[entity]},blocking=True)

    async def wait(self,predicate):
        async with asyncio.timeout(8):
            while True:
                try:
                    if predicate():return
                except AudioTransportError:
                    # HA and MA report the members separately; inconsistent
                    # intermediate observations are expected during convergence.
                    pass
                await asyncio.sleep(.1)


class AlarmGroupIsolation:
    def __init__(self,hass,entry,*,backend=None,store=None,clock=time.time):
        if store is None:
            from homeassistant.helpers.storage import Store
            store=Store(hass,1,'household_voice_'+entry.entry_id+'_alarm_groups')
        self.backend=backend or SonosGroups(hass)
        self.store,self.clock=store,clock
        self.records={};self.active=set();self.lock=asyncio.Lock();self.loaded=False
        self.last_error=None

    @property
    def diagnostics(self):
        return {'isolated_speakers':len(self.records),'active_clips':len(self.active),
                'recovery_required':sum(r['phase']!='isolated' for r in self.records.values()),
                'last_error':self.last_error}

    async def load(self):
        if self.loaded:return
        data=await self.store.async_load()
        if data is not None and (not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('records'),dict)):
            raise AudioTransportError('Alarm grouping journal unavailable')
        self.records=deepcopy((data or {}).get('records',{}));self.loaded=True

    async def save(self):
        await self.store.async_save({'version':1,'records':deepcopy(self.records)})

    async def prepare(self,target,clip,current):
        """Called only after authoritative ring lease validation, before audio."""
        async with self.lock:
            await self.load()
            entity=self.backend.endpoint(target)
            if entity is None:
                # Never broaden a grouped unknown provider to a whole group.
                if self.backend.standalone(target):return None
                raise AudioTransportError('Grouped alarm destination cannot be isolated')
            existing=self.records.get(target)
            if existing:
                if (existing['phase']!='isolated' or existing['occurrence']!=clip['occurrence_id']
                        or target in self.active or not self.backend.isolated(target,entity)):
                    raise AudioTransportError('Alarm speaker isolation is owned or uncertain')
                self.active.add(target);return target
            members=self.backend.members(entity)
            if members==[entity]:return None
            if (not isinstance(clip.get('schedule_id'),str) or not clip['schedule_id']
                    or type(clip.get('ring_until')) not in (int,float)
                    or clip['ring_until']<=self.clock() or not await current()):
                raise AudioTransportError('Alarm isolation requires a current schedule lease')
            record={'entity':entity,'members':members,'occurrence':clip['occurrence_id'],
                    'schedule':clip['schedule_id'],'until':clip['ring_until'],'phase':'detaching',
                    'media':self.backend.media(entity)}
            self.records[target]=record
            await self.save()  # Intent is durable before the first topology write.
            try:
                if self.backend.members(entity)!=members or not await current():
                    del self.records[target];await self.save();raise AudioTransportError('Alarm route changed')
                await self.backend.unjoin(entity)
                await self.backend.wait(lambda:self.backend.isolated(target,entity))
                rest=set(members)-{entity}
                remaining=self.backend.members(next(iter(rest)))
                if set(remaining)!=rest:raise AudioTransportError('Remaining speaker group changed')
                record.update(phase='isolated',remaining=remaining,media=self.backend.media(entity))
                await self.save()
                if not await current():raise AudioTransportError('Alarm ended during isolation')
                self.active.add(target);return target
            except BaseException:
                self.last_error='alarm_group_isolation_incomplete'
                raise

    def check(self,target):
        row=self.records.get(target)
        return (self.backend.standalone(target) if row is None else
                (row['phase']=='isolated' or row['phase']=='playing' and target in self.active)
                and self.backend.isolated(target,row['entity']))

    async def begin_play(self,target):
        if target is None:return
        async with self.lock:
            row=self.records[target]
            if row['phase']!='isolated' or target not in self.active:
                raise AudioTransportError('Alarm isolation ownership lost')
            row['phase']='playing';await self.save()

    async def finish(self,target,outcome):
        if target is None:return
        async with self.lock:
            self.active.discard(target)
            row=self.records.get(target)
            if row and outcome not in ('service_completed','not_invoked','target_unavailable'):
                row['phase']='audio_uncertain';self.last_error='alarm_audio_outcome_unknown';await self.save()
            elif row and row['phase']=='playing':
                row['phase']='isolated';await self.save()

    async def reconcile(self,schedules):
        """Restore group membership only after ringing and owned audio end."""
        async with self.lock:
            await self.load()
            ringing={s['id'] for s in schedules if s.get('state')=='ringing'}
            for target,row in list(self.records.items()):
                if target in self.active or row['phase']!='isolated':continue
                if row['schedule'] in ringing and self.clock()<row['until']:continue
                try:
                    entity=row['entity'];remaining=row['remaining']
                    unchanged=(self.backend.isolated(target,entity)
                        and self.backend.members(remaining[0])==remaining
                        and self.backend.media(entity)==row['media'])
                    if not unchanged:
                        # User/topology/playback changes win; do not regroup later.
                        del self.records[target];await self.save();continue
                    row['phase']='restoring';await self.save()
                    await self.backend.join(remaining[0],entity)
                    await self.backend.wait(lambda:set(self.backend.members(entity))==set(row['members']))
                    del self.records[target];await self.save()
                except Exception:
                    self.last_error='alarm_group_restore_unconfirmed'
