"""Finite native satellite alarm clips with durable uncertain-output exclusion.

HA's announce service awaits async_announce. Cancellation or timeout cannot prove
speaker silence: the per-device pending record survives reload until an admin
confirms output stopped. One global journal prevents cross-entry bypass.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from .satellite_transport import AudioTransportError

_KEY='_household_voice_native_alarm'


class NativeAlarmJournal:
    def __init__(self,path):
        self.path=str(path);existed=Path(path).exists()
        with closing(sqlite3.connect(self.path)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existed and (version!=1 or tables!={'native_output'}):
                raise ValueError('Native output journal needs administrator repair')
            if not existed:
                db.execute('CREATE TABLE native_output(device TEXT PRIMARY KEY,assistant TEXT NOT NULL)')
                db.execute('PRAGMA user_version=1');db.commit()
            if {row[1] for row in db.execute('PRAGMA table_info(native_output)')}!={'device','assistant'}:
                raise ValueError('Invalid native output journal')
    def held(self,device):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT 1 FROM native_output WHERE device=?',(device,)).fetchone() is not None
    def begin(self,device,assistant):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO native_output VALUES(?,?)',(device,assistant));db.commit()
    def clear(self,device):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DELETE FROM native_output WHERE device=?',(device,));db.commit()


class NativeAlarmBackend:
    def __init__(self,hass):
        self.hass=hass;self.locks={};self.journal=None;self.journal_lock=asyncio.Lock()
        self.paused=False;self.busy=0;self.workers=set()
    def lock(self,device):return self.locks.setdefault(device,asyncio.Lock())
    async def write(self,function,*args):
        task=asyncio.create_task(asyncio.to_thread(function,*args));self.workers.add(task)
        task.add_done_callback(self.workers.discard)
        cancelled=False
        while True:
            try:
                result=await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():raise
                cancelled=True
        if cancelled:raise asyncio.CancelledError
        return result
    async def get_journal(self):
        async with self.journal_lock:
            if self.journal is None:
                self.journal=await self.write(NativeAlarmJournal,self.hass.config.path('.storage','household_voice_native_alarm.sqlite3'))
            return self.journal
    async def async_pause_for_backup(self):
        self.paused=True
        async with asyncio.timeout(30):
            while self.busy or self.workers:await asyncio.sleep(.01)
    async def async_resume_after_backup(self):self.paused=False


def get_native_alarm_backend(hass):
    return hass.data.setdefault(_KEY,NativeAlarmBackend(hass))


class NativeAlarmTransport:
    def __init__(self,hass,entry,assistant_id,device_id,*,timeout=20):
        self.hass,self.entry,self.assistant_id,self.device_id=hass,entry,assistant_id,device_id
        self.timeout=timeout
    def resolve(self,target):
        from .area_registry import collect_pairings
        row=collect_pairings(self.hass,self.entry.options)['assistants'].get(target,{})
        expected={'kind':'native','entity_id':self.assistant_id,'device_id':self.device_id}
        state=self.hass.states.get(target)
        if (target!=self.assistant_id or row.get('schedule_output')!=expected or state is None or state.state!='idle'
                or not self.hass.services.has_service('assist_satellite','announce')):
            raise AudioTransportError('Native assistant output unavailable or busy')
        return row
    async def play(self,target,url,*,volume=None,chime=False,before_dispatch=None):
        parsed=urlsplit(url)
        if (not parsed.path.endswith('/audio/alarm.wav') or parsed.scheme not in ('http','https')
                or parsed.username or parsed.password or parsed.query or parsed.fragment or chime is not False):
            raise AudioTransportError('Only the fixed finite alarm clip is allowed')
        backend=get_native_alarm_backend(self.hass)
        async with backend.lock(self.device_id):
            if backend.paused:raise AudioTransportError('Native output paused for backup')
            backend.busy+=1
            try:return await self._play(backend,target,url,before_dispatch)
            finally:backend.busy-=1
    async def _play(self,backend,target,url,before_dispatch):
        journal=await backend.get_journal()
        if await backend.write(journal.held,self.device_id):
            raise AudioTransportError('Native output needs administrator recovery')
        self.resolve(target)
        from .echomuse_runtime import async_native_output_guard
        from .room_focus import announcement_room_focus
        async with async_native_output_guard(self.hass,target):
            async with announcement_room_focus(self.hass,self.entry,target):
                self.resolve(target)
                if before_dispatch is not None and not await before_dispatch():
                    return {'outcome':'not_invoked'}
                await backend.write(journal.begin,self.device_id,target)
                try:
                    async with asyncio.timeout(self.timeout):
                        await self.hass.services.async_call('assist_satellite','announce',
                            {'entity_id':target,'media_id':url,'preannounce':False},blocking=True)
                    await backend.write(journal.clear,self.device_id)
                    return {'outcome':'service_completed','physical_delivery_confirmed':False}
                except asyncio.CancelledError:raise
                except Exception:return {'outcome':'service_outcome_unknown','physical_delivery_confirmed':False}


async def async_reset_native_alarm(hass,entry,assistant_id,confirm_output_stopped=False):
    if confirm_output_stopped is not True:raise AudioTransportError('Confirm native speaker output stopped')
    from .area_registry import collect_pairings
    row=collect_pairings(hass,entry.options)['assistants'].get(assistant_id,{})
    device=row.get('device_id')
    if not device:raise AudioTransportError('Select a registered assistant')
    backend=get_native_alarm_backend(hass);lock=backend.lock(device)
    if lock.locked() or backend.paused:raise AudioTransportError('Wait for active native audio or backup')
    async with lock:
        backend.busy+=1
        try:
            journal=await backend.get_journal();await backend.write(journal.clear,device)
        finally:backend.busy-=1
    return {'cleared':True,'physical_stop_confirmed':False}
