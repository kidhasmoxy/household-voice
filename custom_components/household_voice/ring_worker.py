"""Run finite MA alarm clips under coordinator leases; never stop a user's queue.

Dismissal prevents subsequent clips after an authoritative check. A clip already
submitted may finish (two seconds of audio plus transport buffering). MA offers
no announcement-ID stop; this code deliberately does not invent one.
"""
import asyncio
from contextlib import suppress, asynccontextmanager
from copy import deepcopy
import time

from .satellite_transport import MAAnnouncementTransport, AudioTransportError


class RingWorker:
    def __init__(self, hass, entry, client, *, transport=None):
        self.hass,self.entry,self.client=hass,entry,client
        self.options=deepcopy(dict(entry.options))
        targets={p for r in self.options.get('alarm_rooms',[]) for p in r.get('speakers',[])}
        targets.update(self.options.get('ring_fallback_speakers',[]))
        self.transport=transport or MAAnnouncementTransport(hass,targets,timeout=20,
            spoken_media_policy=self.options.get('spoken_media_policy','pause_and_resume'))
        self.injected_transport=transport
        self.task=None
        self.pending=set()
        self.last_error=None
        self.last_success=None
        self.stopping=False

    def enabled(self):
        return (dict(self.entry.options)==self.options and self.options.get('live_actions_enabled') is True
                and self.options.get('ring_actions_enabled') is True and not self.stopping)

    async def async_start(self):
        if self.enabled() and self.task is None:
            self.task=asyncio.create_task(self._loop(),name='household-voice-rings')

    async def _loop(self):
        while self.enabled():
            try:
                # Parallel rooms, bounded network/service concurrency.
                if len(self.pending)<8:
                    result=await self.client.claim_ring()
                    clip=result.get('clip')
                    if clip:
                        task=asyncio.create_task(self._play(clip))
                        self.pending.add(task)
                        task.add_done_callback(self.pending.discard)
                self.last_success=time.time()
                self.last_error=None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error='coordinator_unavailable'
            await asyncio.sleep(.5)

    async def _play(self, clip):
        outcome='not_invoked'
        try:
            keys=('occurrence_id','target','lease')
            if (not isinstance(clip,dict) or any(not isinstance(clip.get(k),str) for k in keys)
                    or clip.get('audio_path')!='/audio/alarm.wav' or clip.get('duration')!=2):
                self.last_error='invalid_ring_job'
                return
            args={k:clip[k] for k in keys}
            valid_targets={p for r in self.options.get('alarm_rooms',[]) if r.get('room_id')==clip.get('room') for p in r.get('speakers',[])}
            assistant=clip.get('target_assistant_id')
            def current_assistant():
                from .area_registry import collect_pairings
                row=collect_pairings(self.hass,self.options)['assistants'].get(assistant,{})
                output=row.get('schedule_output')
                if output is None and row.get('status')=='ready':
                    output={'kind':'ma','entity_id':row.get('speaker_id'),'device_id':row.get('device_id')}
                return (output is not None and output.get('entity_id')==clip['target']
                    and output.get('kind')==clip.get('output_kind','ma')
                    and (not clip.get('target_assistant_device_id') or output.get('device_id')==clip['target_assistant_device_id']))
            transport=self.transport
            if assistant:
                valid_targets={clip['target']} if current_assistant() else set()
                if self.injected_transport is None:
                    if clip.get('output_kind')=='native':
                        from .native_alarm import NativeAlarmTransport
                        from .area_registry import collect_pairings
                        row=collect_pairings(self.hass,self.options)['assistants'].get(assistant,{})
                        transport=NativeAlarmTransport(self.hass,self.entry,assistant,row.get('device_id'))
                    else:
                        transport=MAAnnouncementTransport(self.hass,valid_targets,timeout=20,
                            spoken_media_policy=self.options.get('spoken_media_policy','pause_and_resume'))
            if clip['target'] not in valid_targets:
                return
            async def current():
                if not self.enabled():
                    return False
                valid=(await self.client.validate_ring(**args)).get('valid') is True
                return valid and self.enabled() and (not assistant or current_assistant())
            if not await current():
                return
            target=clip['target']
            try:
                transport.resolve(target)
            except AudioTransportError:
                if assistant:
                    outcome='target_unavailable'
                    return
                # Fallback is allowed only before any primary service invocation.
                target=None
                available_but_busy=False
                for candidate in self.options.get('ring_fallback_speakers',[]):
                    try:
                        transport.resolve(candidate)
                    except AudioTransportError:
                        continue
                    if not await current():
                        return
                    reserved=await self.client.reserve_ring_fallback(**args,actual_target=candidate)
                    if reserved.get('reserved') is not True:
                        available_but_busy=True
                        continue
                    target=candidate
                    break
                if target is None:
                    outcome='not_invoked' if available_but_busy else 'target_unavailable'
                    return
            outcome='service_outcome_unknown'
            @asynccontextmanager
            async def focus():
                if assistant and clip.get('output_kind','ma')=='ma' and self.injected_transport is None:
                    from .room_focus import announcement_room_focus
                    async with announcement_room_focus(self.hass,self.entry,target):yield
                else:yield
            async with focus():
                result=await transport.play(target,self.client.url+'/audio/alarm.wav',
                    volume=self.options.get('ring_volume',45)/100,chime=False,before_dispatch=current)
            outcome=result.get('outcome','service_outcome_unknown')
            if outcome not in ('service_completed','not_invoked'):
                outcome='service_outcome_unknown'
        except asyncio.CancelledError:
            raise
        except AudioTransportError:
            outcome='target_unavailable'
        except Exception:
            self.last_error='ring_transport_failed'
        finally:
            if 'args' in locals():
                try:
                    await self.client.ack_ring(**args,outcome=outcome)
                except Exception:
                    # Loss of ACK is resolved by the persisted exclusion window.
                    self.last_error='ring_ack_unavailable'

    async def async_close(self):
        self.stopping=True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        pending=list(self.pending)
        for task in pending: task.cancel()
        if pending: await asyncio.gather(*pending,return_exceptions=True)
