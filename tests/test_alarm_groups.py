"""Durable grouping decisions with inert topology and storage, no devices."""
import importlib
from copy import deepcopy
from pathlib import Path
import sys
from types import ModuleType,SimpleNamespace
import unittest

NAME='alarm_groups_fixture'
p=ModuleType(NAME);p.__path__=[str(Path(__file__).resolve().parents[1]/'custom_components/household_voice')];sys.modules[NAME]=p
t=ModuleType(NAME+'.satellite_transport');t.AudioTransportError=type('AudioTransportError',(ValueError,),{});sys.modules[t.__name__]=t
m=importlib.import_module(NAME+'.alarm_groups')

class Store:
    def __init__(self):self.data=None;self.fail=False
    async def async_load(self):return deepcopy(self.data)
    async def async_save(self,data):
        if self.fail:raise OSError('fixture disk failure')
        self.data=deepcopy(data)

class Backend:
    def __init__(self,store):
        self.store=store;self.groups={x:['a','b','c'] for x in 'abc'}
        self.calls=[];self.identity=['original','music'];self.timeout=False
    def endpoint(self,target):return 'a'
    def members(self,entity):return list(self.groups[entity])
    def media(self,entity):return list(self.identity)
    def standalone(self,target):return self.groups['a']==['a']
    def isolated(self,target,entity):return self.standalone(target)
    async def unjoin(self,entity):
        assert self.store.data['records']['target']['phase']=='detaching'
        self.calls.append(('unjoin',entity))
        if self.timeout:raise TimeoutError()
        self.groups={'a':['a'],'b':['b','c'],'c':['b','c']}
    async def join(self,leader,entity):
        assert self.store.data['records']['target']['phase']=='restoring'
        self.calls.append(('join',leader,entity));self.groups={x:['b','c','a'] for x in 'abc'}
    async def wait(self,predicate):assert predicate()

class AlarmGroupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=Store();self.backend=Backend(self.store);self.manager=self.new()
        self.clip={'occurrence_id':'occurrence','schedule_id':'schedule','ring_until':200}
    def new(self):return m.AlarmGroupIsolation(None,None,backend=self.backend,store=self.store,clock=lambda:100)
    async def current(self):return True
    async def isolate(self):return await self.manager.prepare('target',self.clip,self.current)
    async def test_detach_once_for_entire_alarm_then_restore_membership(self):
        token=await self.isolate();await self.manager.begin_play(token)
        await self.manager.finish(token,'service_completed')
        await self.manager.reconcile([{'id':'schedule','state':'ringing'}]);assert len(self.backend.calls)==1
        token=await self.isolate();await self.manager.begin_play(token);await self.manager.finish(token,'service_completed')
        await self.manager.reconcile([])
        assert self.backend.calls==[('unjoin','a'),('join','b','a')]
        assert self.manager.records=={}
    async def test_stale_lease_never_changes_group(self):
        async def stale():return False
        with self.assertRaises(m.AudioTransportError):await self.manager.prepare('target',self.clip,stale)
        assert self.backend.calls==[]
    async def test_write_failure_prevents_ungroup(self):
        self.store.fail=True
        with self.assertRaises(OSError):await self.isolate()
        assert self.backend.calls==[]
    async def test_uncertain_ungroup_never_replayed_after_restart(self):
        self.backend.timeout=True
        with self.assertRaises(TimeoutError):await self.isolate()
        self.manager=self.new()
        with self.assertRaises(m.AudioTransportError):await self.isolate()
        await self.manager.reconcile([]);assert self.backend.calls==[('unjoin','a')]
    async def test_restart_during_play_never_regroups_unknown_audio(self):
        token=await self.isolate();await self.manager.begin_play(token)
        self.manager=self.new();await self.manager.reconcile([])
        assert self.backend.calls==[('unjoin','a')]
        assert self.manager.diagnostics['recovery_required']==1
    async def test_restart_after_completed_audio_restores(self):
        token=await self.isolate();await self.manager.begin_play(token);await self.manager.finish(token,'service_completed')
        self.manager=self.new();await self.manager.reconcile([])
        assert self.backend.calls[-1]==('join','b','a')
    async def test_manual_group_change_wins(self):
        token=await self.isolate();await self.manager.finish(token,'not_invoked')
        self.backend.groups['b']=['b'];self.backend.groups['c']=['c']
        await self.manager.reconcile([]);assert len(self.backend.calls)==1;assert not self.manager.records
    async def test_manual_playback_change_wins(self):
        token=await self.isolate();await self.manager.finish(token,'service_completed')
        self.backend.identity=['user-music','music'];await self.manager.reconcile([])
        assert len(self.backend.calls)==1;assert not self.manager.records
    async def test_uncertain_audio_blocks_regroup(self):
        token=await self.isolate();await self.manager.begin_play(token);await self.manager.finish(token,'service_outcome_unknown')
        await self.manager.reconcile([]);assert len(self.backend.calls)==1
        assert self.manager.diagnostics['recovery_required']==1
    async def test_second_occurrence_cannot_steal_isolation(self):
        await self.isolate()
        with self.assertRaises(m.AudioTransportError):await self.manager.prepare('target',{**self.clip,'occurrence_id':'other'},self.current)
        assert len(self.backend.calls)==1
    async def test_topology_wait_tolerates_staggered_state_updates(self):
        observations=iter(('inconsistent',False,True))
        def converging():
            value=next(observations)
            if value=='inconsistent':raise m.AudioTransportError('state updates in progress')
            return value
        await m.SonosGroups(None).wait(converging)

if __name__=='__main__':unittest.main()
