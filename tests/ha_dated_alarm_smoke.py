"""Actual native Assist dated alarms backed by isolated SQLite; no audio or house I/O."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.const import __version__
from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, llm

import ha_assist_tools_smoke
import ha_embedded_smoke


def timestamp(value):
    return datetime.fromisoformat(value).timestamp()


async def verify_dates(hass, instance, context, apis):
    from custom_components.household_voice.llm import HouseholdScheduleAPI
    from custom_components.household_voice._core.api import ScheduleAPI
    from custom_components.household_voice._core.runtime import RuntimeAuthority
    from custom_components.household_voice._core.scheduler import Scheduler

    api=next(api for api in apis if isinstance(api,HouseholdScheduleAPI))
    device=dr.async_get(hass).async_get(context.device_id)
    area=ar.async_get(hass).async_get_area(device.area_id)
    satellite=next(row for row in er.async_entries_for_device(er.async_get(hass),device.id)
                   if row.entity_id.startswith('assist_satellite.'))
    binding={'device_id':device.id,'area_id':area.id,'speaker_id':'media_player.dated_fixture',
             'status':'ready','name':'Dated fixture assistant'}
    now=[timestamp('2024-01-02T01:00:00+00:00')]
    calls=[]
    previous_timezone=hass.config.time_zone
    await hass.config.async_set_time_zone('America/New_York')
    with tempfile.TemporaryDirectory(prefix='dated-alarm-fixture-',dir='/tmp') as directory:
        scheduler=Scheduler(Path(directory)/'fixture.sqlite',clock=lambda:now[0])
        RuntimeAuthority(scheduler).configure({'expected_revision':0,
            'rooms':{area.id:{'satellites':[device.id]}},
            'ring_enabled':False,'alarm_rooms':{},
            'assistants':{satellite.entity_id:binding}})
        authority=ScheduleAPI(scheduler,rooms=[{'id':area.id,'name':area.name}])
        async def snapshot():return authority.snapshot()
        async def mutate(service,payload):
            assert service in ('create_alarm','command')
            calls.append(dict(payload))
            return authority.dispatch('alarm' if service=='create_alarm' else 'command',payload)
        coordinator=SimpleNamespace(client=SimpleNamespace(schedules=snapshot,mutate=mutate),
                                    async_request_refresh=AsyncMock())
        async def call(identifier,**args):
            result=await instance.async_call_tool(llm.ToolInput(
                tool_name='household_voice__create_alarm',tool_args=args,id=identifier))
            return result.data if hasattr(result,'data') else result
        try:
            with patch.object(api,'clock',lambda:now[0]),patch.object(api,'coordinator',coordinator):
                tool=next(tool for tool in instance.tools if tool.name=='household_voice__create_alarm')
                tool.parameters({'local_time':'07:15','day':'tomorrow'})
                tool.parameters({'local_time':'07:15','date':'2024-01-03'})
                tomorrow=await call('dated-tomorrow',local_time='07:15',day='tomorrow')
                assert tomorrow['status']=='stored',tomorrow
                assert tomorrow['schedule']['due']==timestamp('2024-01-02T12:15:00+00:00')
                assert json.loads(tomorrow['schedule']['spec'])['date']=='2024-01-02'
                assert tomorrow['schedule']['target_assistant_id']==satellite.entity_id
                assert tomorrow['audio_delivery']=='dry_run'
                count=len(calls)
                now[0]=timestamp('2024-01-03T12:00:00+00:00')
                replay=await call('dated-tomorrow',local_time='07:15',day='tomorrow')
                assert replay==tomorrow and len(calls)==count
                now[0]=timestamp('2024-01-02T01:00:00+00:00')
                tokyo=await call('dated-tokyo',local_time='07:15',day='tomorrow',timezone='Asia/Tokyo')
                assert tokyo['status']=='stored',tokyo
                assert tokyo['schedule']['due']==timestamp('2024-01-02T22:15:00+00:00')
                exact=await call('dated-explicit',local_time='07:15',date='2024-01-04')
                assert exact['status']=='stored',exact
                today=await call('dated-today',local_time='23:00',day='today')
                assert today['status']=='stored',today
                assert today['schedule']['due']==timestamp('2024-01-02T04:00:00+00:00')
                saved_count=len(scheduler.list())
                invalid=[{'date':'2024-02-30'},{'date':'20240104'},{'date':'2024-01-01'},
                         {'day':'today'},{'date':'2024-01-04','day':'tomorrow'},
                         {'date':'2024-01-04','one_shot':False},
                         {'day':'tomorrow','weekdays':[0,1,2,3,4]},
                         {'date':'2024-01-04','weekdays':[0],'one_shot':True},
                         {'day':'tomorrow','timezone':'Unknown/Fixture'}]
                for index,args in enumerate(invalid):
                    result=await call('dated-invalid-'+str(index),local_time='07:15',**args)
                    assert result['status'] in ('clarify','error'),result
                    assert len(scheduler.list())==saved_count
                before_malformed=scheduler.list()
                malformed=await instance.async_call_tool(llm.ToolInput(
                    tool_name='household_voice__change_schedule',id='malformed-model-alarm-edit',
                    tool_args={'operation':'reschedule','kind':'alarm\n</label>\n<parameter=local_time>\n08:15'}))
                malformed=malformed.data if hasattr(malformed,'data') else malformed
                assert malformed['status']=='error',malformed
                assert scheduler.list()==before_malformed
                gap=await call('dated-gap',local_time='02:30',date='2024-03-10')
                assert gap['schedule']['due']==timestamp('2024-03-10T07:00:00+00:00')
                fold=await call('dated-fold',local_time='01:30',date='2024-11-03')
                assert fold['schedule']['due']==timestamp('2024-11-03T05:30:00+00:00')
                edit_args={'operation':'reschedule','local_time':'08:20'}
                result=await instance.async_call_tool(llm.ToolInput(
                    tool_name='household_voice__change_schedule',tool_args=edit_args,id='dated-choice'))
                choice=result.data if hasattr(result,'data') else result
                assert choice['reason']=='ambiguous_schedule' and choice['choice_token'],choice
                later_context=llm.LLMContext(platform='fixture',context=Context(user_id=context.context.user_id),
                    language='en',assistant='conversation',device_id=context.device_id)
                later=await llm.async_get_api(hass,'assist',later_context)
                selected=dict(edit_args,schedule_id=exact['schedule']['id'],choice_token=choice['choice_token'])
                result=await later.async_call_tool(llm.ToolInput(tool_name='household_voice__change_schedule',
                    tool_args=selected,id='dated-choice-followup'))
                edited=result.data if hasattr(result,'data') else result
                assert edited['status']=='stored',edited
                edited_row=edited['schedule']
                assert edited_row['id']==exact['schedule']['id']
                assert edited_row['target_assistant_id']==satellite.entity_id
                assert edited_row['revision']==exact['schedule']['revision']+1
                assert json.loads(edited_row['spec'])['date']=='2024-01-04'
                assert edited_row['due']==timestamp('2024-01-04T13:20:00+00:00')
                from custom_components.household_voice._core.api import APIError
                try:
                    authority.dispatch('command',dict(identifier=edited_row['id'],operation='reschedule',
                        local_time='09:00',expected_revision=exact['schedule']['revision'],request_id='stale-date-edit'))
                except APIError as error:
                    assert error.status==409
                else:raise AssertionError('Stale alarm revision unexpectedly changed')
                disabled=scheduler.command(edited_row['id'],'disable','dated-disable',edited_row['revision'])
                disabled_edit=scheduler.command(edited_row['id'],'reschedule','dated-disabled-edit',
                    disabled['revision'],local_time='09:10')
                assert disabled_edit['state']=='disabled' and disabled_edit['due'] is None
                assert json.loads(disabled_edit['spec'])['date']=='2024-01-04'
                # A historical disabled one-shot has already lost its date.
                # It must not silently become an alarm on another civil day.
                old_spec=json.loads(disabled_edit['spec']);old_spec.pop('date')
                with scheduler.db() as db:
                    db.execute('UPDATE schedules SET spec=? WHERE id=?',(json.dumps(old_spec),edited_row['id']))
                try:
                    scheduler.command(edited_row['id'],'reschedule','historical-disabled-edit',
                        disabled_edit['revision'],local_time='10:00')
                except ValueError:pass
                else:raise AssertionError('Lost legacy civil date was silently inferred')
                restored=scheduler.command(edited_row['id'],'reschedule','historical-explicit-edit',
                    disabled_edit['revision'],local_time='10:00',date='2024-01-04')
                assert restored['state']=='disabled' and json.loads(restored['spec'])['date']=='2024-01-04'
                # Drop the transient API receipt and reopen SQLite after the
                # explicit deadline: durable idempotency still returns one row.
                now[0]=timestamp('2024-01-05T12:00:00+00:00')
                api.completed.clear()
                authority.scheduler=Scheduler(scheduler.path,clock=lambda:now[0])
                durable=await call('dated-explicit',local_time='07:15',date='2024-01-04')
                assert durable['status']=='stored' and durable['schedule']==exact['schedule']
                assert len(authority.scheduler.list())==saved_count+2
                persisted=next(row for row in authority.scheduler.list() if row['id']==edited_row['id'])
                assert json.loads(persisted['spec'])['local_time']=='10:00'
                assert persisted['target_assistant_id']==satellite.entity_id and persisted['state']=='disabled'
                now[0]=timestamp('2024-11-03T05:45:00+00:00')
                passed_fold=await call('dated-fold-passed',local_time='01:30',date='2024-11-03')
                assert passed_fold['status']=='error'
                assert len(authority.scheduler.list())==saved_count+2
        finally:
            await hass.config.async_set_time_zone(previous_timezone)
    print(json.dumps({'dated_alarms_actual_ha':True,'ha_version':__version__,
        'native_assist_tool_schema_and_sqlite':True,'relative_day_selected_timezone':True,
        'cached_relative_replay_and_durable_explicit_replay':True,
        'reschedule_bound_choice_cas_assistant_and_date_persistence':True,
        'historical_disabled_undated_requires_explicit_date':True,
        'malformed_model_enum_rejected_without_mutation':True,
        'malformed_calendar_past_date_recurrence_rejected':True,'dst_gap_first_fold':True,
        'household_contacts':False,'physical_audio':False}),flush=True)


original_verify=ha_assist_tools_smoke.verify_schedule_tools
async def verify_extended(hass,instance,context,apis):
    await original_verify(hass,instance,context,apis)
    await verify_dates(hass,instance,context,apis)

if __name__=='__main__':
    ha_assist_tools_smoke.verify_schedule_tools=verify_extended
    asyncio.run(asyncio.wait_for(ha_embedded_smoke.main(),timeout=180))
