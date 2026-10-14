"""Owned native composition lifecycle, independent of Home Assistant imports."""
import asyncio
from .native_recording_shutdown import OwnedShutdown
DATA_KEY="household_voice_native_recording_composition"

def claim_owner(hass,owner):
    if DATA_KEY in hass.data:raise ValueError('composition_already_owned')
    hass.data[DATA_KEY]=owner

class Composition:
    native_recording_owner=True
    def __init__(self,hass,backend,runtime,removers,make_runtime,*,client=None):
        self.hass,self.backend,self.runtime,self.removers=hass,backend,runtime,removers
        self.make_runtime=make_runtime
        self.client=client
        self.on_runtime=None
        self.shutdown=OwnedShutdown()
        self.pause_shutdown=OwnedShutdown()
        self.paused=False
        self.pause_complete=False

    async def _drain(self):
        # A prior timed-out pause may still own cleanup writers independently of
        # the current close attempt. Retain them until all have actually ended.
        pending=tuple(task for name,task in self.pause_shutdown.tasks.items()
                      if name != "final" and not task.done())
        if pending:await asyncio.wait(pending)
        if self.client is not None:
            while pending:=tuple(self.client._pending):await asyncio.wait(pending)

    async def pause_for_backup(self):
        # Caller then pauses the existing embedded client. Delivery workers must
        # stop before executor drain so no new audio work enters the snapshot.
        self.paused=True
        self.pause_complete=False
        self.runtime._closed=True
        self.backend.paused=True
        await self.pause_shutdown.run({'runtime':self.runtime.async_close,'backend':self.backend.pause},self._drain)
        self.pause_complete=True

    async def resume_after_backup(self):
        if not self.paused:return
        if self.backend.closed:raise ValueError('composition_closed')
        if not self.pause_complete:raise ValueError('shutdown_incomplete')
        if self.client is not None and (getattr(self.client,'_backup_paused',False) or getattr(self.client,'_closed',False)):
            raise ValueError('storage_unavailable')
        await self.backend.journal.quiesce()
        self.runtime=self.make_runtime()
        if self.on_runtime:self.on_runtime(self.runtime)
        self.backend.resume()
        await self.runtime.handoff.recover()
        await self.runtime.async_start_worker()
        self.paused=False
        self.pause_shutdown=OwnedShutdown()

    async def async_pause_for_backup(self):await self.pause_for_backup()

    async def async_resume_after_backup(self):await self.resume_after_backup()

    async def close(self):
        self.paused=True
        if self.runtime is not None:self.runtime._closed=True
        if self.backend is not None:self.backend.closed=True
        steps={}
        if self.runtime is not None:steps['runtime']=self.runtime.async_close
        if self.backend is not None:steps['backend']=self.backend.unload
        await self.shutdown.run(steps,self._drain)
        for remove in self.removers:remove()
        self.removers=[]
        if self.hass.data.get(DATA_KEY) is self:self.hass.data.pop(DATA_KEY)
