"""SQLite scheduler with transactional idempotency and durable dry-run outbox.

Staging only: no HA client, speakers, phone delivery, shell, or provider credentials.
"""
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date as civil_date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


try:
    BOOT_ID=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError:
    BOOT_ID='process-'+uuid.uuid4().hex


def elapsed_clock():
    # Linux BOOTTIME includes suspend. A reboot changes BOOT_ID and triggers
    # reconciliation from the last persisted UTC deadline.
    if hasattr(time,'CLOCK_BOOTTIME'):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def next_alarm(local_time, tz_name, weekdays, after):
    """First DST fold, nonexistent local times advanced minute-by-minute."""
    if len(local_time) != 5:
        raise ValueError("Use HH:MM")
    hour, minute = map(int, local_time.split(":"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid local time")
    if not weekdays or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays):
        raise ValueError("weekdays must contain Monday=0 through Sunday=6")
    zone = ZoneInfo(tz_name)
    start = datetime.fromtimestamp(after, zone).date()
    for day in range(9):
        date = start + timedelta(days=day)
        if date.weekday() not in weekdays:
            continue
        naive = datetime(date.year, date.month, date.day, hour, minute)
        # Up to a whole skipped civil day (unusual timezone transitions).
        for shift in range(1441):
            candidate = (naive + timedelta(minutes=shift)).replace(tzinfo=zone, fold=0)
            roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == candidate.replace(tzinfo=None):
                if candidate.timestamp() > after:
                    return candidate.timestamp()
                break
    raise ValueError("Cannot determine next occurrence")


def dated_alarm(local_time, tz_name, date, after):
    """One explicit civil date; never silently roll a passed alarm to tomorrow.

    Use the same first-fold / gap policy as recurring alarms. A completely
    skipped civil day is unavailable rather than a different requested date.
    """
    if not isinstance(date, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date):
        raise ValueError('Alarm date must use YYYY-MM-DD')
    day = civil_date.fromisoformat(date)
    if not isinstance(local_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', local_time):
        raise ValueError('Use HH:MM')
    hour, minute = map(int, local_time.split(':'))
    zone = ZoneInfo(tz_name)
    naive = datetime(day.year, day.month, day.day, hour, minute)
    for shift in range(1441):
        local = naive + timedelta(minutes=shift)
        if local.date() != day:
            break
        candidate = local.replace(tzinfo=zone, fold=0)
        roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
        if roundtrip.replace(tzinfo=None) == local:
            if candidate.timestamp() <= after:
                raise ValueError('Requested alarm date and time have passed')
            return candidate.timestamp()
    raise ValueError('Requested civil date is unavailable in this timezone')


class Scheduler:
    def __init__(self, path, clock=time.time, monotonic=None, boot_id=None, ring_seconds=900):
        self.path, self.clock = str(path), clock
        self.monotonic=monotonic or (elapsed_clock if clock is time.time else clock)
        self.boot_id=boot_id or BOOT_ID
        if type(ring_seconds) not in (int,float) or not 1<=ring_seconds<=3600:
            raise ValueError('Ring duration must be 1..3600 seconds')
        self.ring_seconds=ring_seconds
        with self.db() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version>4:
                raise ValueError('Database is newer than this scheduler; refusing downgrade')
            db.executescript("""
            CREATE TABLE IF NOT EXISTS schedules(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
              source_room TEXT NOT NULL, target_room TEXT NOT NULL,
              due REAL, remaining REAL, state TEXT NOT NULL, revision INTEGER NOT NULL,
              spec TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests(
              id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox(
              occurrence_id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL,
              due REAL NOT NULL, status TEXT NOT NULL, target_room TEXT NOT NULL
            );
            """)
            db.execute('BEGIN IMMEDIATE')
            try:
                for table, additions in {
                    'schedules': {'mono_due':'REAL','clock_id':'TEXT','target_assistant_id':'TEXT','target_assistant_device_id':'TEXT'},
                    'outbox': {'ring_until':'REAL','ring_deadline':'REAL','clock_id':'TEXT'},
                }.items():
                    columns={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
                    for name,kind in additions.items():
                        if name not in columns:
                            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
                # Legacy timers inherit their persisted UTC deadline on upgrade.
                db.execute("UPDATE schedules SET mono_due=?,clock_id='' WHERE kind='timer' AND state='active' AND mono_due IS NULL",(0,))
                db.execute("UPDATE outbox SET ring_until=due+?,clock_id='' WHERE status='ringing' AND ring_until IS NULL",(ring_seconds,))
                db.execute('PRAGMA user_version=4')
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _rebase(self, db, r):
        if r['state']=='active' and r['mono_due'] is not None:
            now,mono=self.clock(),self.monotonic()
            if r['clock_id']!=self.boot_id:
                r.update(mono_due=mono+r['due']-now,clock_id=self.boot_id)
            r['due']=now+r['mono_due']-mono
            db.execute('UPDATE schedules SET due=?,mono_due=?,clock_id=? WHERE id=?',
                       (r['due'],r['mono_due'],r['clock_id'],r['id']))
        return r

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def _mutate(self, request_id, payload, action):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id required, maximum 128 characters")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
                if old:
                    if old["fingerprint"] != fingerprint:
                        raise ValueError("Idempotency key reused with different arguments")
                    db.rollback()
                    return json.loads(old["response"])
                result = action(db)
                db.execute("INSERT INTO requests VALUES(?,?,?)",
                           (request_id, fingerprint, json.dumps(result)))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _text(value, name):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
            raise ValueError(f"{name} must contain 1..120 characters")
        return value.strip()

    def create_timer(self, label, seconds, source_room, request_id, target_room=None, target_assistant_id=None):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 604800:
            raise ValueError("Timer duration must be 0..604800 seconds, exclusive of zero")
        return self._create("timer", label, source_room, target_room or source_room,
                            self.clock() + seconds, {}, request_id,
                            {"seconds": seconds}, target_assistant_id=target_assistant_id, requested_target_room=target_room)

    def create_alarm(self, label, local_time, tz_name, weekdays, source_room, request_id, target_room=None, one_shot=False, target_assistant_id=None, date=None):
        if type(one_shot) is not bool:
            raise ValueError('one_shot must be boolean')
        if date is not None and not one_shot:
            raise ValueError('An explicit alarm date requires one_shot=true')
        if date is not None:
            if not isinstance(date, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date):
                raise ValueError('Alarm date must use YYYY-MM-DD')
            if not weekdays or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays):
                raise ValueError('weekdays must contain Monday=0 through Sunday=6')
        # Resolve dated deadlines inside the idempotent transaction. A replay
        # after the original deadline must return its original stored receipt.
        due = (lambda: dated_alarm(local_time, tz_name, date, self.clock())) if date is not None else next_alarm(local_time, tz_name, weekdays, self.clock())
        spec = {"local_time": local_time, "timezone": tz_name, "weekdays": sorted(set(weekdays))}
        if date is not None:
            day = civil_date.fromisoformat(date)
            if day.weekday() not in weekdays:
                raise ValueError('Alarm date conflicts with selected weekdays')
            spec['date'] = date
        if one_shot:
            spec['one_shot']=True
        return self._create("alarm", label, source_room, target_room or source_room,
                            due, spec, request_id, spec, target_assistant_id=target_assistant_id,
                            requested_target_room=target_room)

    @staticmethod
    def _assistant_binding(db, identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'assist_satellite\.[a-z0-9_]+',identifier):
            raise ValueError('Select a configured target_assistant_id')
        table=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_settings'").fetchone()
        row=db.execute('SELECT body FROM runtime_settings WHERE id=1').fetchone() if table else None
        config=json.loads(row['body']) if row else {}
        binding=config.get('assistants',{}).get(identifier)
        output=binding.get('schedule_output') if isinstance(binding,dict) else None
        legacy_external=isinstance(binding,dict) and binding.get('status')=='ready' and isinstance(binding.get('speaker_id'),str) and re.fullmatch(r'media_player\.[a-z0-9_]+',binding['speaker_id'])
        if (not isinstance(binding,dict) or binding.get('area_id') not in config.get('rooms',{})
                or not binding.get('device_id') or not (output or legacy_external)):
            raise ValueError('Target assistant needs an available schedule output')
        return binding

    def _create(self, kind, label, source, target, due, spec, request_id, original, *, target_assistant_id=None, requested_target_room=None):
        label = self._text(label, "label")
        source, target = self._text(source, "source_room"), self._text(target, "target_room")
        payload = dict(kind=kind, label=label, source=source, target=target, **original)
        if kind=='alarm' or target_assistant_id is not None: payload['target_assistant_id']=target_assistant_id
        def action(db):
            destination=target
            device=None
            if kind=='alarm' or target_assistant_id is not None:
                binding=self._assistant_binding(db,target_assistant_id)
                if requested_target_room is not None and requested_target_room!=binding['area_id']:
                    raise ValueError('Alarm room must match the selected assistant area')
                destination=binding['area_id']
                device=binding['device_id']
            identifier = str(uuid.uuid4())
            deadline = due() if callable(due) else due
            mono_due=self.monotonic()+deadline-self.clock() if kind=='timer' else None
            db.execute("INSERT INTO schedules(id,kind,label,source_room,target_room,due,remaining,state,revision,spec,created,mono_due,clock_id,target_assistant_id,target_assistant_device_id) VALUES(?,?,?,?,?,?,?, ?,1,?,?,?,?,?,?)",
                       (identifier, kind, label, source, destination, deadline, None, "active", json.dumps(spec), self.clock(),mono_due,self.boot_id if mono_due is not None else None,target_assistant_id,device))
            return dict(db.execute("SELECT * FROM schedules WHERE id=?", (identifier,)).fetchone())
        return self._mutate(request_id, payload, action)

    def list(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                rows=[self._rebase(db,dict(r)) for r in db.execute("SELECT * FROM schedules WHERE state!='deleted' ORDER BY created,id").fetchall()]
                for row in rows:
                    if row['kind']=='alarm': row['alarm_binding']='assistant_bound' if row['target_assistant_id'] else 'legacy_needs_binding'
                db.commit()
                return rows
            except BaseException:
                db.rollback()
                raise

    def dismiss_ringing(self, request_id, expected):
        """Atomically dismiss the complete observed ringing set, including none.

        A changed set or revision aborts the whole decision. The empty decision
        is durable and idempotent too; it is not permission to stop arbitrary
        audio, nor a lock across an external player operation.
        """
        if (not isinstance(expected, dict) or len(expected)>2000
                or any(not isinstance(key,str) or not 1<=len(key)<=120
                       or type(value) is not int or value<1 for key,value in expected.items())):
            raise ValueError('Invalid ringing snapshot')
        def action(db):
            rows=[dict(row) for row in db.execute("SELECT * FROM schedules WHERE state='ringing' ORDER BY id")]
            if {row['id']:row['revision'] for row in rows} != expected:
                raise ValueError('Revision conflict; ringing set changed')
            for row in rows:
                spec=json.loads(row['spec'])
                recurring=row['kind']=='alarm' and not spec.get('one_shot')
                due=next_alarm(spec['local_time'],spec['timezone'],spec['weekdays'],self.clock()) if recurring else None
                db.execute("UPDATE schedules SET state=?,due=?,mono_due=NULL,clock_id=NULL,revision=revision+1 WHERE id=?",
                           ('active' if recurring else 'completed',due,row['id']))
                db.execute("UPDATE outbox SET status='dismiss' WHERE schedule_id=? AND status='ringing'",(row['id'],))
            return {'dismissed_ids':[row['id'] for row in rows], 'dismissed_count':len(rows),
                    'no_ringing_at_decision':not rows, 'audible_stop_confirmed':False}
        return self._mutate(request_id, {'operation':'dismiss_ringing','expected':expected}, action)

    def command(self, identifier, operation, request_id, expected_revision, seconds=0, target_room=None, label=None, target_assistant_id=None,
                local_time=None, tz_name=None, weekdays=None, one_shot=None, date=None):
        allowed = {"pause", "resume", "add", "set", "cancel", "dismiss", "snooze", "move", "enable", "disable", "rename", "delete", "reschedule"}
        if operation not in allowed:
            raise ValueError("Unsupported operation")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Expected revision must be a positive integer")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or abs(seconds) > 604800:
            raise ValueError("Invalid duration")
        payload = dict(id=identifier, op=operation, revision=expected_revision, seconds=seconds, target=target_room)
        if label is not None:
            payload['label']=label
        if target_assistant_id is not None:
            if operation!='move': raise ValueError('Assistant target is only valid for move')
            payload['target_assistant_id']=target_assistant_id
        settings = {name:value for name,value in dict(local_time=local_time,timezone=tz_name,
            weekdays=weekdays,one_shot=one_shot,date=date).items() if value is not None}
        if settings and operation!='reschedule':
            raise ValueError('Only reschedule takes alarm settings')
        payload.update(settings)
        def action(db):
            row = db.execute("SELECT * FROM schedules WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("Unknown schedule")
            r = self._rebase(db,dict(row))
            if r['state']=='deleted':
                raise ValueError('Schedule was deleted')
            if r["revision"] != expected_revision:
                raise ValueError("Revision conflict; reread schedule")
            now = self.clock()
            if operation == "pause":
                if r["kind"] != "timer" or r["state"] != "active":
                    raise ValueError("Only active countdowns can pause")
                r.update(remaining=max(0, r["due"]-now), due=None, state="paused")
                r.update(mono_due=None,clock_id=None)
            elif operation == "resume":
                if r["state"] != "paused":
                    raise ValueError("Timer is not paused")
                r.update(due=now+r["remaining"], remaining=None, state="active")
                r.update(mono_due=self.monotonic()+r['due']-now,clock_id=self.boot_id)
            elif operation == "set":
                if r['kind']!='timer' or r['state'] not in ('active','paused') or not 0<seconds<=604800:
                    raise ValueError('Set needs an active or paused timer and positive duration')
                if r['state']=='paused':
                    r['remaining']=seconds
                else:
                    r['due']=now+seconds
                    r['mono_due']=self.monotonic()+seconds
                    r['clock_id']=self.boot_id
            elif operation == "add":
                if r["kind"] != "timer" or r["state"] not in ("active", "paused"):
                    raise ValueError("Only active/paused timers can change duration")
                key = "due" if r["state"] == "active" else "remaining"
                minimum = now if key == "due" else 0
                if r[key]+seconds <= minimum:
                    raise ValueError("New duration must remain positive")
                r[key] += seconds
                if key=='due':
                    r['mono_due']+=seconds
            elif operation == 'reschedule':
                if r['kind']!='alarm' or r['state'] not in ('active','disabled') or seconds!=0 or target_room is not None or label is not None:
                    raise ValueError('Reschedule needs an active or disabled alarm')
                if not isinstance(local_time,str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',local_time):
                    raise ValueError('Reschedule requires HH:MM local_time')
                spec = json.loads(r['spec'])
                old_zone = spec['timezone']
                spec['local_time'] = local_time
                if tz_name is not None:
                    ZoneInfo(tz_name)
                    spec['timezone'] = tz_name
                if weekdays is not None:
                    if not isinstance(weekdays,list) or not weekdays or any(type(d) is not int or not 0<=d<=6 for d in weekdays):
                        raise ValueError('weekdays must contain Monday=0 through Sunday=6')
                    spec['weekdays'] = sorted(set(weekdays))
                if one_shot is not None and type(one_shot) is not bool:
                    raise ValueError('one_shot must be boolean')
                once = one_shot if one_shot is not None else False if weekdays is not None else spec.get('one_shot',False)
                if date is not None and not once:
                    raise ValueError('An explicit date cannot silently replace a recurring alarm')
                if once:
                    spec['one_shot'] = True
                    if date is not None:
                        spec['date'] = date
                    elif 'date' not in spec and r['due'] is not None:
                        # Legacy one-shots have no civil date in spec. Preserve
                        # their already scheduled date during a clock-only edit.
                        spec['date'] = datetime.fromtimestamp(r['due'],ZoneInfo(old_zone)).date().isoformat()
                    elif 'date' not in spec:
                        raise ValueError('Choose an explicit date for this disabled legacy one-shot alarm')
                else:
                    spec.pop('one_shot',None)
                    spec.pop('date',None)
                if 'date' in spec:
                    deadline = dated_alarm(spec['local_time'],spec['timezone'],spec['date'],now)
                    if civil_date.fromisoformat(spec['date']).weekday() not in spec['weekdays']:
                        raise ValueError('Alarm date conflicts with selected weekdays')
                else:
                    deadline = next_alarm(spec['local_time'],spec['timezone'],spec['weekdays'],now)
                r.update(spec=json.dumps(spec),due=deadline if r['state']=='active' else None,
                    mono_due=None,clock_id=None)
            elif operation=='rename':
                r['label']=self._text(label,'label')
            elif operation=='delete':
                r.update(state='deleted',due=None,remaining=None,mono_due=None,clock_id=None)
            elif operation == "move":
                if r['kind']=='alarm' or target_assistant_id is not None:
                    binding=self._assistant_binding(db,target_assistant_id)
                    if target_room is not None and target_room!=binding['area_id']:
                        raise ValueError('Alarm room must match the selected assistant area')
                    r['target_assistant_id']=target_assistant_id
                    r['target_assistant_device_id']=binding['device_id']
                    r['target_room']=binding['area_id']
                else:
                    r['target_assistant_id']=None
                    r['target_assistant_device_id']=None
                    r["target_room"] = self._text(target_room, "target_room")
                db.execute("UPDATE outbox SET target_room=? WHERE schedule_id=? AND status='ringing'",
                           (r["target_room"], identifier))
            elif operation == "snooze":
                if r["state"] != "ringing" or not 0 < seconds <= 3600:
                    raise ValueError("Snooze needs ringing state and 1..3600 seconds")
                r.update(state="active", due=now+seconds)
                r.update(mono_due=self.monotonic()+seconds,clock_id=self.boot_id)
            elif operation in ("cancel", "disable"):
                if operation == 'disable' and r['kind'] != 'alarm':
                    raise ValueError('Only alarms can be disabled; cancel the timer')
                if operation=='disable' and r['due'] is not None:
                    spec=json.loads(r['spec'])
                    if spec.get('one_shot') and 'date' not in spec:
                        spec['date']=datetime.fromtimestamp(r['due'],ZoneInfo(spec['timezone'])).date().isoformat()
                        r['spec']=json.dumps(spec)
                r.update(state="cancelled" if operation == "cancel" else "disabled", due=None)
                r.update(mono_due=None,clock_id=None)
            elif operation == "enable":
                if r["kind"] != "alarm" or r["state"] != "disabled":
                    raise ValueError("Only disabled alarms can enable")
                s = json.loads(r["spec"])
                deadline = dated_alarm(s['local_time'], s['timezone'], s['date'], now) if 'date' in s else next_alarm(s["local_time"], s["timezone"], s["weekdays"], now)
                r.update(state="active", due=deadline)
            elif operation == "dismiss":
                if r["state"] != "ringing":
                    raise ValueError("Schedule is not ringing")
                if r["kind"] == "alarm" and not json.loads(r['spec']).get('one_shot'):
                    s = json.loads(r["spec"])
                    r.update(state="active", due=next_alarm(s["local_time"], s["timezone"], s["weekdays"], now))
                else:
                    r.update(state="completed", due=None)
                r.update(mono_due=None,clock_id=None)
            if operation in ("cancel", "disable", "dismiss", "snooze", "delete"):
                db.execute("UPDATE outbox SET status=? WHERE schedule_id=? AND status='ringing'", (operation, identifier))
            r["revision"] += 1
            db.execute("UPDATE schedules SET target_room=?,due=?,remaining=?,state=?,revision=?,label=?,mono_due=?,clock_id=?,target_assistant_id=?,target_assistant_device_id=?,spec=? WHERE id=?",
                       (r["target_room"], r["due"], r["remaining"], r["state"], r["revision"],r['label'],r['mono_due'],r['clock_id'],r['target_assistant_id'],r['target_assistant_device_id'],r['spec'], identifier))
            return r
        return self._mutate(request_id, payload, action)

    def tick(self, grace_seconds=300):
        """Reconcile due schedules transactionally; emits records only, never audio."""
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Rebase UTC projections before deciding which countdowns are due.
                for row in db.execute("SELECT * FROM schedules WHERE state='active' AND mono_due IS NOT NULL").fetchall():
                    self._rebase(db,dict(row))
                for row in db.execute("SELECT * FROM outbox WHERE status='ringing'").fetchall():
                    e=dict(row)
                    if e['clock_id']!=self.boot_id or e['ring_deadline'] is None:
                        e['ring_deadline']=self.monotonic()+e['ring_until']-now
                    remaining=e['ring_deadline']-self.monotonic()
                    db.execute('UPDATE outbox SET ring_until=?,ring_deadline=?,clock_id=? WHERE occurrence_id=?',
                               (now+remaining,e['ring_deadline'],self.boot_id,e['occurrence_id']))
                    if remaining<=0:
                        db.execute("UPDATE outbox SET status='timed_out' WHERE occurrence_id=?",(e['occurrence_id'],))
                        current=db.execute('SELECT * FROM schedules WHERE id=?',(e['schedule_id'],)).fetchone()
                        if current and current['state']=='ringing':
                            due=None
                            state='completed'
                            if current['kind']=='alarm' and not json.loads(current['spec']).get('one_shot'):
                                spec=json.loads(current['spec'])
                                # Preserve a recurrence already due during downtime,
                                # so the normal grace/missed policy handles it below.
                                due=current['due'] or next_alarm(spec['local_time'],spec['timezone'],spec['weekdays'],now)
                                state='active'
                            db.execute('UPDATE schedules SET state=?,due=?,revision=revision+1 WHERE id=?',(state,due,current['id']))
                for row in db.execute("SELECT * FROM schedules WHERE state IN ('active','ringing') AND due<=?", (now,)).fetchall():
                    r = dict(row)
                    status = "ringing" if now-r["due"] <= grace_seconds else "missed"
                    occurrence = f'{r["id"]}:{r["due"]:.6f}'
                    db.execute("INSERT OR IGNORE INTO outbox(occurrence_id,schedule_id,due,status,target_room,ring_until,ring_deadline,clock_id) VALUES(?,?,?,?,?,?,?,?)",
                               (occurrence, r["id"], r["due"], status, r["target_room"],now+self.ring_seconds if status=='ringing' else None,
                                self.monotonic()+self.ring_seconds if status=='ringing' else None,self.boot_id))
                    next_due = None
                    state = status
                    if r["kind"] == "alarm" and not json.loads(r['spec']).get('one_shot'):
                        s = json.loads(r["spec"])
                        next_due = next_alarm(s["local_time"], s["timezone"], s["weekdays"], now)
                        state = "active" if status=='missed' else 'ringing'
                    db.execute("UPDATE schedules SET state=?,due=?,mono_due=NULL,clock_id=NULL,revision=revision+1 WHERE id=?",
                               (state, next_due, r["id"]))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.events()

    def events(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox ORDER BY due,occurrence_id")]

