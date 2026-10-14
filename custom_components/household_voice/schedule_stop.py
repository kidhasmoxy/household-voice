"""Ringing-first stop, with durable scheduler decision before scoped media stop."""
from copy import deepcopy
import asyncio
import hashlib
import json

import probatio
from homeassistant.helpers import llm
from .llm_compat import tool_result


class HouseholdStopTool(llm.Tool):
    integration='household_voice'
    name='household_voice__stop'
    description=('Stop: first dismiss all currently ringing household alarms and timers. '
        'Only if none are ringing, stop media on this requesting assistant’s paired output. '
        'Set ringing_only=true for an explicit alarm/timer stop, so no media is stopped if none ring. '
        'For bare stop omit it. Never use generic speaker stop as an alarm substitute. '
        'Accepted finite alarm clips may finish; do not claim acoustic silence.')
    parameters=probatio.Schema({probatio.Optional('ringing_only'):bool})

    def __init__(self, api):
        self.api=api

    async def async_call(self,hass,tool_input,context):
        from .api import CoordinatorConflict
        from .request_area import resolve_source_area, RequestAreaError
        from homeassistant.helpers import device_registry as dr, entity_registry as er
        if (not isinstance(tool_input.tool_args,dict) or set(tool_input.tool_args)-{'ringing_only'}
                or type(tool_input.tool_args.get('ringing_only',False)) is not bool
                or not isinstance(tool_input.id,str) or not 1<=len(tool_input.id)<=512):
            return tool_result(llm,data={'status':'error','reason':'invalid_stop_arguments'},error=True)
        if self.api.entry.options.get('assist_tools_enabled') is not True or getattr(self.api.entry,'disabled_by',None) is not None:
            return tool_result(llm,data={'status':'error','reason':'assist_tools_disabled'},error=True)
        device_id=getattr(context,'device_id',None)
        device=dr.async_get(hass).async_get(device_id) if device_id else None
        if (device is None or getattr(device,'disabled_by',None) is not None
                or not getattr(getattr(context,'context',None),'id',None)
                or not any(row.device_id==device_id and row.entity_id.startswith('assist_satellite.')
                           and getattr(row,'disabled_by',None) is None
                           for row in er.async_get(hass).entities.values())):
            return tool_result(llm,data={'status':'clarify','reason':'trusted_source_assistant_required'})
        ringing_only=tool_input.tool_args.get('ringing_only',False)
        signature=('stop',ringing_only)
        request_id='ha-stop-'+hashlib.sha256(json.dumps([self.api.entry.entry_id,
            getattr(context.context,'id',None),getattr(context.context,'user_id',None),
            context.device_id,tool_input.id]).encode()).hexdigest()
        async with self.api.call_lock:
            if request_id in self.api.completed:
                if self.api.completed[request_id][0]!=signature:
                    return tool_result(llm,data={'status':'error','reason':'tool_call_id_reused_with_different_arguments'},error=True)
                return tool_result(llm,data=deepcopy(self.api.completed[request_id][1]),
                                   error=self.api.completed[request_id][2])
            uncertain={'status':'error','reason':'stop_result_uncertain','retry_automatically':False,
                       'audible_stop_confirmed':False}
            self.api.completed[request_id]=(signature,uncertain,True)
            while len(self.api.completed)>256:self.api.completed.popitem(last=False)
            try:
                await self.api.coordinator.async_request_refresh()
                data=await self.api.coordinator.client.schedules()
                expected={row['id']:row['revision'] for row in data['schedules'] if row['state']=='ringing'}
                result=await self.api.coordinator.client.dismiss_ringing(request_id,expected)
                decision=result['ringing_decision']
                if decision['dismissed_count']:
                    output={'status':'stored','reason':'ringing_schedules_dismissed',**decision,
                        'schedule_database_changed':True,'media_stop_dispatched':False,
                        'speech':'The ringing alarms and timers are dismissed. A sound already playing may finish.'}
                elif decision['no_ringing_at_decision'] is True and ringing_only:
                    output={'status':'noop','reason':'nothing_ringing','media_stop_dispatched':False}
                elif decision['no_ringing_at_decision'] is True:
                    try:
                        resolve_source_area(hass,self.api.entry.options,context)
                    except RequestAreaError:
                        output={'status':'clarify','reason':'trusted_source_room_required','media_stop_dispatched':False}
                        self.api.completed[request_id]=(signature,deepcopy(output),False)
                        return tool_result(llm,data=output)
                    # Scheduler and external player have no shared transaction.
                    # Refresh immediately before dispatch; if new ringing exists,
                    # fail closed instead of stopping an unrelated media queue.
                    latest=await self.api.coordinator.client.schedules()
                    if any(row['state']=='ringing' for row in latest['schedules']):
                        output={'status':'clarify','reason':'ringing_changed_before_media_stop',
                                'media_stop_dispatched':False,'retry_automatically':False}
                    else:
                        from .media_llm import HouseholdMediaAPI,MediaTool
                        apis=[api for api in llm.async_get_apis(hass) if isinstance(api,HouseholdMediaAPI)
                              and api.entry.entry_id==self.api.entry.entry_id]
                        if len(apis)!=1 or apis[0].entry.options.get('media_enabled') is not True:
                            output={'status':'noop','reason':'nothing_ringing_media_unavailable','media_stop_dispatched':False}
                        else:
                            answer=await MediaTool('household_media_stop',apis[0]).async_call(hass,
                                llm.ToolInput(tool_name='household_media_stop',tool_args={},id=request_id+'-media'),context)
                            output=deepcopy(answer.data if hasattr(answer,'data') else answer)
                            output['ringing_decision']='none_at_decision_and_recheck'
                else:
                    output=uncertain
            except CoordinatorConflict:
                output={'status':'clarify','reason':'ringing_changed','retry_automatically':False,'media_stop_dispatched':False}
            except asyncio.CancelledError:
                raise
            except Exception:
                output=uncertain
            self.api.completed[request_id]=(signature,deepcopy(output),output.get('status')=='error')
            return tool_result(llm,data=output,error=output.get('status')=='error')
