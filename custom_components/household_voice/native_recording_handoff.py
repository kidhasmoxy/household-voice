"""Draft acquisition replacement; reuses existing BroadcastRuntime delivery.

No STT wrapper, private Core patch, separate outbox or alternate player. Dependency
injection permits actual existing runtime/SQLite tests without auto-registration.
"""
import asyncio
import base64
from copy import deepcopy
import io
import hashlib
import json
import secrets
from types import SimpleNamespace
import wave
from .native_recording_backend import QuestionAdmissionRejected
from .native_recording_ledger import STANDARD_MA_CONTRACT,request_signature


def captured_answer(answer,source,nonce,captured_type):
    if (answer.source_device_id!=source.device_id or answer.satellite_entity_id!=source.entity_id
            or (answer.sample_rate,answer.sample_width,answer.channels)!=(16000,2,1)):
        raise ValueError('capture_source_conflict')
    stream=io.BytesIO()
    with wave.open(stream,'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
        output.writeframes(answer.pcm)
    return captured_type(profile_id=source.profile_id,source_device=source.device_id,nonce=nonce,
        wav_base64=base64.b64encode(stream.getvalue()).decode('ascii'),transcript=answer.transcript,
        duration_seconds=len(answer.pcm)/32000)


class NativeHandoff:
    def __init__(self,backend,client,authority,run_owned,resolve_source,captured_type):
        self.backend,self.client,self.authority,self.run_owned=backend,client,authority,run_owned
        self.resolve_source,self.captured_type=resolve_source,captured_type

    async def begin(self,request,source,targets):
        return await self.run_owned(self.authority.ledger.begin,request,source,targets,
                                    playback_contract=STANDARD_MA_CONTRACT)

    async def get(self,request):return await self.run_owned(self.authority.ledger.get,request)

    async def cancel(self,request):return await self.run_owned(self.authority.cancel,request)

    async def execute(self,request,source,*,preflight_targets=None):
        row=await self.get(request)
        if row is None or row['phase']!='reserved' or row['cancel_requested']:
            raise ValueError('handoff_not_new')
        if self.resolve_source(source.device_id)!=source:raise ValueError('source_binding_changed')
        try:
            if preflight_targets is not None:preflight_targets(row['targets'])
        except Exception:
            await self.cancel(request)
            await self.run_owned(self.authority.ledger.advance,request,'reserved','cancelled')
            raise
        await self.run_owned(self.authority.ledger.advance,request,'reserved','capturing')
        try:
            if preflight_targets is not None:preflight_targets(row['targets'])
        except Exception:
            # No backend/question/capture invocation has happened. Finalize only
            # this reserved request; a failed durable write retains its evidence.
            await self.cancel(request)
            await self.run_owned(self.authority.ledger.advance,request,'capturing','cancelled')
            raise
        try:
            if preflight_targets is None:
                answer=await self.backend.record(source.device_id,source.entity_id,request)
            else:
                answer=await self.backend.record(source.device_id,source.entity_id,request,
                    before_question=lambda:preflight_targets(row['targets']))
        except QuestionAdmissionRejected:
            # Backend has proven no native question invocation and released its
            # own source reservation; do not leave an uncertain capture request.
            await self.cancel(request)
            await self.run_owned(self.authority.ledger.advance,request,'capturing','cancelled')
            raise
        if answer.transcript.casefold().strip().rstrip('.!?') in ('cancel','cancel announcement','never mind','nevermind'):
            await self.cancel(request)
            await self.run_owned(self.authority.ledger.advance,request,'capturing','cancelled')
            return {'cancelled':True,'broadcast_id':None}
        if self.resolve_source(source.device_id)!=source:raise ValueError('source_binding_changed')
        if (await self.get(request))['cancel_requested']:return await self.cancel(request)
        captured=captured_answer(answer,source,row['nonce'],self.captured_type)
        payload={key:getattr(captured,key) for key in ('profile_id','source_device','nonce','wav_base64','transcript')}
        signature=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        await self.run_owned(self.authority.ledger.advance,request,'capturing','uploading',upload_signature=signature)
        # Only the original immutable payload/nonce is retried. No second question.
        for attempt in range(3):
            if self.resolve_source(source.device_id)!=source:raise ValueError('source_binding_changed')
            if (await self.get(request))['cancel_requested']:return await self.cancel(request)
            try:
                recording=await self.client.upload_capture(captured)
                break
            except asyncio.CancelledError:raise
            except Exception:
                if attempt==2:raise ValueError('upload_outcome_unknown') from None
        await self.run_owned(self.authority.ledger.advance,request,'uploading','ready',recording_id=recording['id'])
        # Serialized with cancellation and the existing outbox pull authority.
        return await self.run_owned(self.authority.enqueue,request)

    async def recover(self):
        """Recover only stored recording identities; never recapture lost content."""
        outcomes=[]
        for row in await self.run_owned(self.authority.ledger.pending):
            if row['cancel_requested']:outcomes.append(await self.cancel(row['request_id']))
            else:
                source=self.resolve_source(row['source'])
                if source is None:continue
                expected=request_signature(source.__dict__,row['targets'],row.get('playback_contract'))
                if expected!=row['signature']:continue
                if row['phase']=='uploading':
                    resolved=await self.run_owned(self.authority.recover_upload_and_enqueue,row['request_id'],source)
                    if resolved is not None:outcomes.append(resolved)
                elif row['phase']=='ready':
                    if self.resolve_source(row['source'])!=source:continue
                    outcomes.append(await self.run_owned(self.authority.enqueue,row['request_id']))
        return outcomes


def composed_runtime_type(base):
    """Subclass only question acquisition; delivery/status workers stay upstream."""
    class NativeBroadcastRuntime(base):
        def __init__(self,*args,handoff,**kwargs):
            kwargs['transport_policy']='standard_ma'
            super().__init__(*args,**kwargs)
            # A real injected adapter must honor the same native admission policy.
            # Custom inert adapters are reserved for isolated fixtures.
            from .announcement_adapter import AnnouncementAdapter
            if isinstance(self.adapter,AnnouncementAdapter) and self.adapter.transport_policy!='standard_ma':
                raise ValueError('Native recording requires versioned standard MA announcement routing')
            self.handoff=handoff
            self._native_starting=set()

        async def async_start(self,device_id,context,*,target_rooms=None,include_home_phones=True,request_id=None):
            def reply(status,reason,**fields):
                return dict(status=status,reason=reason,audio_delivery_confirmed=False,
                            phone_delivery_confirmed=False,**fields)
            if not isinstance(device_id,str) or not device_id or context is None:
                return reply('clarify','trusted_voice_device_required')
            if not self.enabled:return reply('dry_run','announcement_execution_disabled')
            if getattr(self.handoff.backend.journal,'failed',False):
                return reply('error','recording_source_requires_recovery')
            request_id=request_id or 'broadcast-'+secrets.token_hex(24)
            try:
                refresh=getattr(self.handoff,'refresh',None)
                if refresh is not None:await refresh()
                source=self.handoff.resolve_source(device_id)
                if source is None:raise ValueError('source_unavailable')
                targets=self.adapter.targets(target_rooms,include_home_phones)
                if self.handoff.backend.book is None or self.handoff.backend.paused or self.handoff.backend.closed:
                    if await self.handoff.get(request_id) is None:
                        return reply('error','recording_worker_unavailable')
                if device_id in self.sessions or device_id in self._native_starting:
                    return reply('clarify','announcement_already_in_progress_on_this_device')
                self._native_starting.add(device_id)
                try:row,created=await self.handoff.begin(request_id,source,targets)
                finally:self._native_starting.discard(device_id)
            except Exception:return reply('error','source_targets_or_request_unavailable')
            if not created:
                return reply('no_change','request_already_recorded_or_outcome_unknown',phase=row['phase'],
                             broadcast_id=row['broadcast_id'])
            if not self.enabled:
                await self.handoff.cancel(request_id)
                return reply('cancelled','announcement_execution_disabled')
            session=SimpleNamespace(device_id=device_id,request_id=request_id,targets=targets,
                source=source,satellite=source.entity_id,phase='waiting_for_first_turn',task=None)
            self.sessions[device_id]=session
            session.task=self._spawn(self._run_native(session),'household voice native recording')
            return reply('queued','wait_for_prompt_then_speak_announcement',target_count=len(targets))

        async def _run_native(self,session):
            result={'status':'error','reason':'recording_or_enqueue_outcome_unknown'}
            try:
                await self._wait_idle(session.satellite)
                if not self.enabled:raise ValueError('execution_disabled')
                preflight=getattr(self.adapter,'preflight_targets',None)
                if not callable(preflight):raise ValueError('announcement_destination_guard_required')
                def current_targets(targets):
                    if not self.enabled:raise ValueError('execution_disabled')
                    preflight(targets)
                session.phase='recording_and_queuing'
                queued=await self.handoff.execute(session.request_id,session.source,preflight_targets=current_targets)
                self._wake_delivery_workers()
                result={'status':'cancelled' if queued.get('cancelled') else 'queued',
                    'reason':'announcement_cancelled' if queued.get('cancelled') else 'recording_queued_for_delivery',
                    'broadcast_id':queued.get('broadcast_id')}
            except asyncio.CancelledError:
                # Durable cancel must settle before another executor can enqueue.
                operation=asyncio.create_task(self.handoff.cancel(session.request_id))
                try:await asyncio.shield(operation)
                except asyncio.CancelledError:await asyncio.shield(operation)
                result={'status':'cancelled','reason':'pending_delivery_cancelled_accepted_audio_may_continue'}
                raise
            except QuestionAdmissionRejected:
                result={'status':'error','reason':'announcement_destination_no_longer_available'}
            except Exception:pass
            finally:
                result.update(audio_delivery_confirmed=False,phone_delivery_confirmed=False)
                self.latest[session.device_id]=result
                self.sessions.pop(session.device_id,None)

        async def async_start_inline(self,device_id,context,*,transcript,body,request_id):
            # Never trim or synthesize inline content. Request a native answer.
            return await self.async_start(device_id,context,request_id=request_id)

        async def async_cancel(self,device_id):
            if session:=self.sessions.get(device_id):
                # Persist first; cancellation cannot create an untracked enqueue.
                await self.handoff.cancel(session.request_id)
                session.task.cancel();await asyncio.gather(session.task,return_exceptions=True)
                self.sessions.pop(device_id,None)
                return {'status':'cancelled','reason':'pending_delivery_cancelled_accepted_audio_may_continue'}
            row=await self.handoff.run_owned(self.handoff.authority.ledger.latest,device_id)
            if row:
                result=await self.handoff.cancel(row['request_id'])
                return {'status':'cancelled','reason':'pending_delivery_cancelled_accepted_audio_may_continue',
                        'broadcast_id':result.get('broadcast_id')}
            return await super().async_cancel(device_id)

        async def async_status(self,device_id):
            if device_id in self.sessions:return await super().async_status(device_id)
            row=await self.handoff.run_owned(self.handoff.authority.ledger.latest,device_id)
            if row:
                if row['broadcast_id']:
                    return {'status':'delivery_status','delivery':await self.client.broadcast_status(row['broadcast_id']),
                            'audio_delivery_confirmed':False,'phone_delivery_confirmed':False}
                return {'status':'cancelled' if row['cancel_requested'] else 'outcome_unknown',
                        'phase':row['phase'],'reason':'recording_not_retried_automatically',
                        'audio_delivery_confirmed':False,'phone_delivery_confirmed':False}
            return await super().async_status(device_id)

        async def async_close(self):
            await super().async_close()
            # Global composition owner separately unloads backend after all users.

    return NativeBroadcastRuntime
