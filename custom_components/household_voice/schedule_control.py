"""Deterministic ringing priority and revision-bound schedule clarification."""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
import time

from .api import CoordinatorConflict
from .schedule_journal import ScheduleJournal
from .schedule_language import bare_stop, new_timer, normalize, schedule_edit
from .policy.schedule_tools import ScheduleRecord, _eligible

DATA_KEY='_household_voice_schedule_control'
TTL=120


def get_schedule_control(hass):
    """Shared backup-aware admission authority; storage opens in the executor."""
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY]=ScheduleControl(journal_path=hass.config.path(
            '.storage','household_voice_schedule_control.sqlite3'))
    return hass.data[DATA_KEY]


@dataclass
class Pending:
    expires: float
    edit: object
    choices: tuple
    question: str


def _reply(speech,conversation_id=None,continuing=False):
    return {'speech':speech,'conversation_id':conversation_id,'continue_conversation':continuing}


def _records(data):
    rows=data.get('schedules') if isinstance(data,dict) else None
    if not isinstance(rows,list) or len(rows)>2000:raise ValueError('Invalid snapshot')
    records=tuple(ScheduleRecord.from_row(row) for row in rows)
    if len({row.id for row in records})!=len(records):raise ValueError('Duplicate schedule')
    return records


def _request_id(entry_id,context,device_id,record,operation,seconds=None,*,text='',batch=False):
    body=[entry_id,getattr(context,'id',None),device_id,text,operation,seconds]
    if batch: body.extend([record.id,record.due,record.revision])
    return 'ha-control-'+hashlib.sha256(json.dumps(body,separators=(',',':')).encode()).hexdigest()


def _label(value):
    return ' '.join(value.casefold().replace('’',"'").split())


def _choices(records,edit):
    return tuple(sorted((row for row in records if row.kind==edit.kind and _eligible(row,edit.operation or 'set')
        and (edit.label is None or _label(row.label)==_label(edit.label))),
        key=lambda row:(row.kind,_label(row.label),row.target_room,row.id)))


def _question(choices,kind,rooms=None,now=None):
    rooms=rooms or {};now=time.time() if now is None else now
    descriptions=[]
    for index,row in enumerate(choices[:10],1):
        detail=row.state
        remaining=row.remaining if row.state=='paused' else max(0,row.due-now) if row.due is not None else None
        if row.kind=='timer' and row.state in ('active','paused') and remaining is not None:
            seconds=max(0,int(remaining));minutes,seconds=divmod(seconds,60)
            detail=f'{minutes} minutes {seconds} seconds remaining'+(' and paused' if row.state=='paused' else '')
        room=rooms.get(row.target_room)
        descriptions.append(f'{index}: {row.label}'+(f', in {room}' if room else '')+f', {detail}')
    if not choices:return f'I could not find an eligible {kind}. Tell me its exact name or create a new one.'
    return f'Which {kind} do you mean? '+ '; '.join(descriptions)+'. Say its name or number, or cancel.'


def _selection(text,choices):
    value=normalize(text)
    if value is None:return None
    ordinals={'first':0,'second':1,'third':2,'fourth':3,'fifth':4,'sixth':5,'seventh':6,'eighth':7,'ninth':8,'tenth':9}
    for index in range(min(10,len(choices))):
        words=[word for word,number in ordinals.items() if number==index]
        if value in {str(index+1),f'number {index+1}',f'the {index+1}',*words,*('the '+word for word in words),*(f'the {word} one' for word in words),*(f'the {word} {choices[index].kind}' for word in words)}:
            return choices[index]
    matches=[row for row in choices if value in {_label(row.label), 'the '+_label(row.label),
        _label(row.label)+' '+row.kind,'the '+_label(row.label)+' '+row.kind,row.id.casefold()}]
    return matches[0] if len(matches)==1 else None


def _looks_like_selection(text,language):
    value=normalize(text,language) or ''
    return bool(re.fullmatch(r'(?:the |number )?(?:[1-9]|10|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)(?: one| timer| alarm)?',value)
        or value in ('that one','this one'))


class ScheduleControl:
    def __init__(self,*,clock=time.monotonic,journal_path=None):
        self.clock=clock
        self.pending=OrderedDict()
        self.lock=asyncio.Lock()
        self.journal=ScheduleJournal(journal_path)
        self.paused=False
        self.workers=set()

    async def _write(self, function, *args):
        task=asyncio.create_task(asyncio.to_thread(function,*args))
        self.workers.add(task)
        cancelled=False
        try:
            while True:
                try:
                    result=await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled=True
                    if task.done():
                        result=task.result()
                        break
            if cancelled:raise asyncio.CancelledError
            return result
        finally:
            self.workers.discard(task)

    async def async_pause_for_backup(self):
        self.paused=True
        async with self.lock:
            pass  # Active request and shielded storage commits have drained.

    async def async_resume_after_backup(self):
        self.paused=False

    def _replay(self,response,key):
        if response is None:
            return _reply('That request was already handled. I have not sent it again.')
        if response['continue_conversation']:
            pending=self.pending.get((*key[:-1],response['conversation_id']))
            if pending is None or pending.expires<=self.clock():
                return _reply('That timer selection has expired. Please repeat the complete change you want.')
        return response

    async def route(self,client,entry_id,user_input,*,trusted,conversation_id):
        async with self.lock:
            key=(entry_id,user_input.device_id,getattr(user_input.context,'user_id',None),user_input.conversation_id)
            pending=self.pending.get(key)
            handled=(bare_stop(user_input.text,user_input.language)
                or schedule_edit(user_input.text,user_input.language) is not None
                or pending is not None and pending.expires>self.clock())
            if not trusted:
                return await self._route(client,entry_id,user_input,trusted=trusted,conversation_id=conversation_id)
            if self.paused and handled:
                return _reply('Timer controls are paused for a backup. Please try again when it finishes.')
            context_id=getattr(user_input.context,'id',None)
            if not isinstance(context_id,str) or not 1<=len(context_id)<=512:
                if not handled:
                    return await self._route(client,entry_id,user_input,trusted=trusted,conversation_id=conversation_id)
                return _reply('A trusted request identity is required to change timers or alarms.')
            identifier=hashlib.sha256(json.dumps([entry_id,user_input.device_id,context_id],separators=(',',':')).encode()).hexdigest()
            dialogue_scope=hashlib.sha256(json.dumps([entry_id,user_input.device_id,getattr(user_input.context,'user_id',None)],separators=(',',':')).encode()).hexdigest()
            fingerprint=hashlib.sha256(json.dumps([user_input.text,getattr(user_input.context,'user_id',None),user_input.conversation_id],separators=(',',':')).encode()).hexdigest()
            try:
                if not handled:
                    existing=await self._write(self.journal.lookup,identifier,fingerprint)
                    if existing is None:
                        if (user_input.conversation_id and not new_timer(user_input.text,user_input.language)
                                and await self._write(self.journal.known_dialogue,dialogue_scope,user_input.conversation_id)):
                            return _reply('That timer selection has expired or was already resolved. Please repeat the complete change you want.')
                        return await self._route(client,entry_id,user_input,trusted=trusted,conversation_id=conversation_id)
                    state,response=existing
                    if state=='completed':
                        return self._replay(response,key)
                    return _reply('That request was already received, but I cannot safely repeat it. Please check the timers and alarms page.')
                state,response=await self._write(self.journal.admit,identifier,fingerprint)
                if state=='completed':
                    # A previously delegated bare stop must not reach media again.
                    return self._replay(response,key)
                if state!='admitted':
                    return _reply('That request was already received, but I cannot safely repeat it. Please check the timers and alarms page.')
                response=await self._route(client,entry_id,user_input,trusted=trusted,conversation_id=conversation_id)
                await self._write(self.journal.complete,identifier,fingerprint,response,dialogue_scope)
                return response
            except Exception:
                return _reply('I could not safely confirm this timer request. Please check the timers and alarms page before trying again.')

    async def _route(self,client,entry_id,user_input,*,trusted,conversation_id):
        """None alone permits the ordinary agent; failures never invite guessing."""
        text,language=user_input.text,user_input.language
        stop=bare_stop(text,language)
        edit=schedule_edit(text,language)
        key=(entry_id,user_input.device_id,getattr(user_input.context,'user_id',None),user_input.conversation_id)
        now=self.clock()
        self.pending=OrderedDict((k,v) for k,v in self.pending.items() if v.expires>now)
        pending=self.pending.get(key)
        if not stop and edit is None and pending is None:
            if _looks_like_selection(text,language):
                return _reply('That timer selection has expired or belongs to another conversation. Please repeat the change you want.')
            return None
        if not trusted:
            return _reply('Use a registered voice assistant to change household timers or stop ringing alarms.')
        if stop:
            self.pending.pop(key,None)
            return await self._stop(client,entry_id,user_input)
        selection=None
        if pending and edit is None:
            if new_timer(text,language):
                self.pending.pop(key,None)
                return None
            selection=_selection(text,pending.choices)
            if selection is None:
                value=normalize(text,language) or ''
                if value in ('cancel','never mind','nevermind'):
                    self.pending.pop(key,None)
                    return _reply('Okay, I have not changed the timer or alarm.')
                if re.match(r'^(?:set|change|reset|adjust|update|extend|increase|pause|resume|cancel|delete|add)\b',value):
                    self.pending.pop(key,None)
                    return _reply('I have cleared the previous change. Please repeat the timer name and the complete new change, for example, set the pasta timer to five minutes.')
                # Until resolved or cancelled, no ambiguous follow-up is
                # given to a model that could invent its own selected ID.
                return _reply(pending.question,key[-1],True)
            edit=pending.edit
        if edit.operation is None:
            self.pending.pop(key,None)
            return _reply('Please say which timer to change and the duration, for example, set the pasta timer to five minutes.')
        try:
            snapshot=await client.schedules()
            records=_records(snapshot)
            rooms={row['id']:row['name'] for row in snapshot.get('rooms',[]) if isinstance(row,dict) and isinstance(row.get('name'),str) and isinstance(row.get('id'),str)}
        except Exception:return _reply('I could not check the current schedules. Nothing has been changed by this request.')
        choices=_choices(records,edit)
        if selection is not None:
            current=next((row for row in choices if row.id==selection.id),None)
            if current is None or current.revision!=selection.revision:
                return self._ask(key,conversation_id,edit,choices,'That schedule changed. ',rooms)
            selected=current
        elif len(choices)==1:
            selected=choices[0]
        else:
            return self._ask(key,conversation_id,edit,choices,rooms=rooms)
        self.pending.pop(key,None)
        payload={'identifier':selected.id,'operation':edit.operation,'expected_revision':selected.revision,
            'request_id':_request_id(entry_id,user_input.context,user_input.device_id,selected,edit.operation,edit.seconds,text=text)}
        if edit.seconds is not None:payload['seconds']=edit.seconds
        try:
            result=await client.mutate('command',payload)
            stored=ScheduleRecord.from_row(result['schedule'])
            if stored.id!=selected.id or stored.kind!=selected.kind or stored.revision<=selected.revision:
                raise ValueError('Invalid stored result')
        except CoordinatorConflict:
            try:choices=_choices(_records(await client.schedules()),edit)
            except Exception:return _reply('That schedule changed. Please ask again; I have not retried the change.')
            return self._ask(key,conversation_id,edit,choices,'That schedule changed. ',rooms)
        except Exception:
            return _reply('I could not confirm that change. Please check the timers and alarms page before trying again.')
        return _reply(f'{selected.label} {selected.kind} updated.' if edit.operation!='dismiss'
            else f'{selected.label} {selected.kind} dismissed.')
    def _ask(self,key,conversation_id,edit,choices,prefix='',rooms=None):
        self.pending.pop(key,None)
        if not choices:return _reply(prefix+_question(choices,edit.kind))
        # Never hide selectable IDs behind an arbitrary first-N mutation.
        if len(choices)>10:return _reply(prefix+f'There are {len(choices)} matching {edit.kind}s. Please say the exact name of the one to change.')
        key=(*key[:-1],conversation_id)
        question=_question(choices,edit.kind,rooms)
        self.pending[key]=Pending(self.clock()+TTL,edit,choices,question)
        while len(self.pending)>64:self.pending.popitem(last=False)
        return _reply(prefix+question,conversation_id,True)

    async def _stop(self,client,entry_id,user_input):
        try:initial=tuple(row for row in _records(await client.schedules()) if row.state=='ringing')
        except Exception:return _reply('I could not check which timers and alarms are ringing. Please check the timers and alarms page.')
        if not initial:return None
        # Only IDs ringing in this first authoritative snapshot are in scope.
        stopped=already=failed=0
        for original in initial:
            row=original
            for attempt in range(2):
                payload={'identifier':row.id,'operation':'dismiss','expected_revision':row.revision,
                    'request_id':_request_id(entry_id,user_input.context,user_input.device_id,row,'dismiss',text=user_input.text,batch=True)}
                try:
                    result=await client.mutate('command',payload)
                    stored=ScheduleRecord.from_row(result['schedule'])
                    if stored.id!=row.id or stored.kind!=row.kind or stored.state=='ringing' or stored.revision<=row.revision:
                        raise ValueError('Invalid dismiss result')
                    stopped+=1;break
                except CoordinatorConflict:
                    try:current=next((item for item in _records(await client.schedules()) if item.id==original.id),None)
                    except Exception:failed+=1;break
                    if current is None or current.state!='ringing':already+=1;break
                    if current.kind!=original.kind or current.due!=original.due or attempt==1:failed+=1;break
                    row=current
                except Exception:failed+=1;break
        if failed:
            return _reply(f'Dismissed {stopped} ringing timers or alarms. I could not confirm {failed}; please check the timers and alarms page.')
        if stopped:
            return _reply(f'Dismissed {stopped} ringing timers or alarms across the house.')
        return _reply('The timers and alarms from that request are no longer marked as ringing.')
