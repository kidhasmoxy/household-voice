"""Persisted HA-owned room configuration and bounded alarm delivery leases.

Only the authenticated administrative adapter configures this API. The model is
never given it. A lease authorizes one short clip, not indefinite player control.
No transport or generic player stop exists in the coordinator.
"""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import io
import json
import math
import re
import struct
import uuid
import wave

from .api import APIError
from .capture_api import room_capture_bindings
from .routing import RoomRegistry


def alarm_wav():
    """Two seconds of generated, finite audio; no third-party sound asset."""
    out = io.BytesIO()
    with wave.open(out, 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        for index in range(32000):
            phase = index % 8000
            amplitude = min(1, phase/160, max(0, (4800-phase)/160)) if phase < 4800 else 0
            value = int(10000 * amplitude * math.sin(2*math.pi*880*index/16000))
            wav.writeframesraw(struct.pack('<h', value))
    return out.getvalue()


def validate_config(payload):
    if (not isinstance(payload, dict) or not {'rooms','ring_enabled','alarm_rooms'}<=set(payload)
            or set(payload)-{'rooms','ring_enabled','alarm_rooms','ring_fallback_speakers'}):
        raise APIError('Invalid runtime configuration fields')
    rooms = payload['rooms']
    if not isinstance(rooms, dict) or len(rooms) > 100 or type(payload['ring_enabled']) is not bool:
        raise APIError('Invalid runtime configuration')
    devices = set()
    for key, room in rooms.items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', key):
            raise APIError('Invalid room identifier')
        if not isinstance(room, dict) or set(room)-{'name', 'aliases', 'satellites', 'capture_profiles'}:
            raise APIError('Invalid room fields')
        if not isinstance(room.get('name', key), str) or not 1 <= len(room.get('name', key)) <= 120:
            raise APIError('Invalid room name')
        aliases = room.get('aliases', [])
        satellites = room.get('satellites', [])
        if (not isinstance(aliases, list) or len(aliases)>20 or any(not isinstance(a,str) or not 1<=len(a)<=120 for a in aliases)
                or not isinstance(satellites,list) or len(satellites)>100
                or any(not isinstance(d,str) or not 1<=len(d)<=128 or d in devices for d in satellites)
                or len(satellites)!=len(set(satellites))):
            raise APIError('Invalid or duplicate room binding')
        devices.update(satellites)
    try:
        RoomRegistry(rooms)
        bindings = room_capture_bindings(rooms)
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p) for p in bindings):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise APIError('Invalid room or capture binding') from None
    alarm_rooms = payload['alarm_rooms']
    if not isinstance(alarm_rooms, dict) or len(alarm_rooms)>100:
        raise APIError('Invalid alarm rooms')
    used = set()
    for room, targets in alarm_rooms.items():
        if (room not in rooms or not isinstance(targets,list) or not 1<=len(targets)<=20
                or any(not isinstance(t,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',t) for t in targets)
                or len(set(targets))!=len(targets) or used.intersection(targets)):
            raise APIError('Invalid or duplicate alarm target')
        used.update(targets)
    if payload['ring_enabled'] and not alarm_rooms:
        raise APIError('Configure alarm speakers before enabling ringing')
    fallback=payload.get('ring_fallback_speakers',[])
    if (not isinstance(fallback,list) or len(fallback)>20
            or any(not isinstance(t,str) or not re.fullmatch(r'media_player\.[a-z0-9_]+',t) for t in fallback)
            or len(set(fallback))!=len(fallback)):
        raise APIError('Invalid fallback speakers')
    return deepcopy(payload)


class RuntimeAuthority:
    def __init__(self, scheduler, initial_rooms=None):
        self.scheduler, self.clock = scheduler, scheduler.clock
        self.initial_rooms = deepcopy(initial_rooms or {})
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS runtime_settings(id INTEGER PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS ring_clips(
                occurrence TEXT NOT NULL,target TEXT NOT NULL,room TEXT NOT NULL,
                lease TEXT,lease_until REAL,available REAL NOT NULL DEFAULT 0,
                sequence INTEGER NOT NULL DEFAULT 0,outcome TEXT,config_revision INTEGER,
                PRIMARY KEY(occurrence,target))''')
            if 'schedule_revision' not in {r['name'] for r in db.execute('PRAGMA table_info(ring_clips)')}:
                db.execute('ALTER TABLE ring_clips ADD COLUMN schedule_revision INTEGER')
            if 'actual_target' not in {r['name'] for r in db.execute('PRAGMA table_info(ring_clips)')}:
                db.execute('ALTER TABLE ring_clips ADD COLUMN actual_target TEXT')

    @contextmanager
    def db(self):
        with self.scheduler.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _config(self, db):
        row = db.execute('SELECT * FROM runtime_settings WHERE id=1').fetchone()
        return {'revision':row['revision'], **json.loads(row['body'])} if row else {
            'revision':0,'rooms':self.initial_rooms,'alarm_rooms':{},'ring_enabled':False}

    def config(self):
        with self.db() as db:
            return deepcopy(self._config(db))

    def configure(self, payload):
        if not isinstance(payload,dict) or 'expected_revision' not in payload:
            raise APIError('Expected runtime revision is required')
        data = dict(payload)
        expected = data.pop('expected_revision')
        if type(expected) is not int or expected<0:
            raise APIError('Invalid runtime revision')
        data = validate_config(data)
        encoded = json.dumps(data,sort_keys=True,separators=(',',':'))
        with self.db() as db:
            old = self._config(db)
            if old['revision'] != expected:
                raise APIError('Runtime configuration changed; refresh before saving',409)
            previous = {k:v for k,v in old.items() if k!='revision'}
            if previous == data:
                return old
            revision = expected+1
            db.execute('INSERT OR REPLACE INTO runtime_settings VALUES(1,?,?)',(revision,encoded))
            # Invalidate fetched work; a finite clip already submitted may finish.
            db.execute("UPDATE ring_clips SET lease=NULL,outcome='configuration_changed' WHERE lease IS NOT NULL")
            return {'revision':revision,**data}

    def _active(self, db):
        now = self.clock()
        return {r['occurrence_id']:dict(r) for r in db.execute('''SELECT o.*,s.label,s.kind,s.revision AS schedule_revision FROM outbox o
            JOIN schedules s ON s.id=o.schedule_id WHERE o.status='ringing' AND s.state='ringing' AND o.ring_until>?''',(now,))}

    def claim(self):
        self.scheduler.tick()
        with self.db() as db:
            config = self._config(db)
            if not config['ring_enabled']:
                return {'mode':'disabled','clip':None}
            active, now = self._active(db), self.clock()
            for occurrence,event in active.items():
                for target in config['alarm_rooms'].get(event['target_room'],[]):
                    db.execute('INSERT OR IGNORE INTO ring_clips(occurrence,target,room) VALUES(?,?,?)',(occurrence,target,event['target_room']))
            # One outstanding clip per physical target even if multiple alarms ring.
            busy = {r['actual'] for r in db.execute('SELECT COALESCE(actual_target,target) AS actual FROM ring_clips WHERE lease_until>?',(now,))}
            rows = db.execute('SELECT * FROM ring_clips ORDER BY available,sequence,occurrence,target').fetchall()
            for row in rows:
                event = active.get(row['occurrence'])
                if (not event or row['target'] not in config['alarm_rooms'].get(event['target_room'],[])
                        or row['target'] in busy or row['available']>now or event['ring_until']<=now+2):
                    continue
                lease = uuid.uuid4().hex
                sequence = row['sequence']+1
                # Unknown previous outcome is not retried: this is a new periodic
                # ring pulse after the prior 30-second exclusion window.
                db.execute('''UPDATE ring_clips SET room=?,lease=?,lease_until=?,available=?,sequence=?,
                    config_revision=?,schedule_revision=?,actual_target=?,outcome='claimed' WHERE occurrence=? AND target=?''',
                    (event['target_room'],lease,now+30,now+30,sequence,config['revision'],event['schedule_revision'],row['target'],row['occurrence'],row['target']))
                return {'mode':'execution_enabled','clip':{'occurrence_id':row['occurrence'],'target':row['target'],
                    'lease':lease,'sequence':sequence,'lease_until':now+30,'ring_until':event['ring_until'],
                    'duration':2,'audio_path':'/audio/alarm.wav','room':event['target_room'],'label':event['label']}}
            return {'mode':'execution_enabled','clip':None}

    def _owned(self, db, occurrence_id, target, lease):
        config = self._config(db)
        event = self._active(db).get(occurrence_id)
        row = db.execute('SELECT * FROM ring_clips WHERE occurrence=? AND target=?',(occurrence_id,target)).fetchone()
        return row if (config['ring_enabled'] and event and row and row['lease']==lease
            and row['lease_until']>self.clock() and row['config_revision']==config['revision']
            and row['schedule_revision']==event['schedule_revision']
            and row['room']==event['target_room'] and target in config['alarm_rooms'].get(event['target_room'],[])) else None

    def validate(self, occurrence_id, target, lease):
        self.scheduler.tick()
        with self.db() as db:
            return {'valid':self._owned(db,occurrence_id,target,lease) is not None}

    def reserve_fallback(self, occurrence_id, target, lease, actual_target):
        with self.db() as db:
            row=self._owned(db,occurrence_id,target,lease)
            if row is None or actual_target not in self._config(db).get('ring_fallback_speakers',[]):
                return {'reserved':False}
            busy=db.execute('''SELECT 1 FROM ring_clips WHERE COALESCE(actual_target,target)=? AND lease_until>?
                AND NOT (occurrence=? AND target=?)''',(actual_target,self.clock(),occurrence_id,target)).fetchone()
            if busy:
                return {'reserved':False}
            db.execute('UPDATE ring_clips SET actual_target=? WHERE occurrence=? AND target=?',(actual_target,occurrence_id,target))
            return {'reserved':True}

    def acknowledge(self, occurrence_id, target, lease, outcome):
        if outcome not in ('service_completed','service_outcome_unknown','not_invoked','target_unavailable'):
            raise APIError('Invalid ring outcome')
        with self.db() as db:
            row = self._owned(db,occurrence_id,target,lease)
            if row is None:
                raise APIError('Ring lease expired or superseded',409)
            # Uncertain service execution reserves the whole exclusion window.
            delay = 30 if outcome=='service_outcome_unknown' else 5 if outcome=='target_unavailable' else 2
            available = max(row['available'],self.clock()+delay) if outcome=='service_outcome_unknown' else self.clock()+delay
            db.execute('UPDATE ring_clips SET lease=NULL,lease_until=?,available=?,outcome=? WHERE occurrence=? AND target=?',
                       (available if outcome=='service_outcome_unknown' else None,available,outcome,occurrence_id,target))
            return {'outcome':outcome,'physical_delivery_confirmed':False}

    def dispatch(self, operation, payload):
        schemas = {'claim':set(),'validate':{'occurrence_id','target','lease'},'ack':{'occurrence_id','target','lease','outcome'},
                   'fallback':{'occurrence_id','target','lease','actual_target'}}
        if operation not in schemas or not isinstance(payload,dict) or set(payload)!=schemas[operation]:
            raise APIError('Invalid ring operation fields')
        if any(not isinstance(v,str) or not 1<=len(v)<=200 for v in payload.values()):
            raise APIError('Invalid ring argument')
        return getattr(self,{'claim':'claim','validate':'validate','ack':'acknowledge','fallback':'reserve_fallback'}[operation])(**payload)
