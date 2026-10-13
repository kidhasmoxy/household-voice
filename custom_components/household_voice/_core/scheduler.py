"""SQLite scheduler with transactional idempotency and durable dry-run outbox.

Staging only: no HA client, speakers, phone delivery, shell, or provider credentials.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
            if version>2:
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
                    'schedules': {'mono_due':'REAL','clock_id':'TEXT'},
                    'outbox': {'ring_until':'REAL','ring_deadline':'REAL','clock_id':'TEXT'},
                }.items():
                    columns={r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
                    for name,kind in additions.items():
                        if name not in columns:
                            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')
                # Legacy timers inherit their persisted UTC deadline on upgrade.
                db.execute("UPDATE schedules SET mono_due=?,clock_id='' WHERE kind='timer' AND state='active' AND mono_due IS NULL",(0,))
                db.execute("UPDATE outbox SET ring_until=due+?,clock_id='' WHERE status='ringing' AND ring_until IS NULL",(ring_seconds,))
                db.execute('PRAGMA user_version=2')
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

    def create_timer(self, label, seconds, source_room, request_id, target_room=None):
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or not 0 < seconds <= 604800:
            raise ValueError("Timer duration must be 0..604800 seconds, exclusive of zero")
        return self._create("timer", label, source_room, target_room or source_room,
                            self.clock() + seconds, {}, request_id,
                            {"seconds": seconds})

    def create_alarm(self, label, local_time, tz_name, weekdays, source_room, request_id, target_room=None, one_shot=False):
        if type(one_shot) is not bool:
            raise ValueError('one_shot must be boolean')
        due = next_alarm(local_time, tz_name, weekdays, self.clock())
        spec = {"local_time": local_time, "timezone": tz_name, "weekdays": sorted(set(weekdays))}
        if one_shot:
            spec['one_shot']=True
        return self._create("alarm", label, source_room, target_room or source_room,
                            due, spec, request_id, spec)

    def _create(self, kind, label, source, target, due, spec, request_id, original):
        label = self._text(label, "label")
        source, target = self._text(source, "source_room"), self._text(target, "target_room")
        payload = dict(kind=kind, label=label, source=source, target=target, **original)
        def action(db):
            identifier = str(uuid.uuid4())
            mono_due=self.monotonic()+due-self.clock() if kind=='timer' else None
            db.execute("INSERT INTO schedules(id,kind,label,source_room,target_room,due,remaining,state,revision,spec,created,mono_due,clock_id) VALUES(?,?,?,?,?,?,?, ?,1,?,?,?,?)",
                       (identifier, kind, label, source, target, due, None, "active", json.dumps(spec), self.clock(),mono_due,self.boot_id if mono_due is not None else None))
            return dict(db.execute("SELECT * FROM schedules WHERE id=?", (identifier,)).fetchone())
        return self._mutate(request_id, payload, action)

    def list(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                rows=[self._rebase(db,dict(r)) for r in db.execute("SELECT * FROM schedules WHERE state!='deleted' ORDER BY created,id").fetchall()]
                db.commit()
                return rows
            except BaseException:
                db.rollback()
                raise

    def command(self, identifier, operation, request_id, expected_revision, seconds=0, target_room=None, label=None):
        allowed = {"pause", "resume", "add", "cancel", "dismiss", "snooze", "move", "enable", "disable", "rename", "delete"}
        if operation not in allowed:
            raise ValueError("Unsupported operation")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("Expected revision must be a positive integer")
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or abs(seconds) > 604800:
            raise ValueError("Invalid duration")
        payload = dict(id=identifier, op=operation, revision=expected_revision, seconds=seconds, target=target_room)
        if label is not None:
            payload['label']=label
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
            elif operation=='rename':
                r['label']=self._text(label,'label')
            elif operation=='delete':
                r.update(state='deleted',due=None,remaining=None,mono_due=None,clock_id=None)
            elif operation == "move":
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
                r.update(state="cancelled" if operation == "cancel" else "disabled", due=None)
                r.update(mono_due=None,clock_id=None)
            elif operation == "enable":
                if r["kind"] != "alarm" or r["state"] != "disabled":
                    raise ValueError("Only disabled alarms can enable")
                s = json.loads(r["spec"])
                r.update(state="active", due=next_alarm(s["local_time"], s["timezone"], s["weekdays"], now))
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
            db.execute("UPDATE schedules SET target_room=?,due=?,remaining=?,state=?,revision=?,label=?,mono_due=?,clock_id=? WHERE id=?",
                       (r["target_room"], r["due"], r["remaining"], r["state"], r["revision"],r['label'],r['mono_due'],r['clock_id'], identifier))
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

