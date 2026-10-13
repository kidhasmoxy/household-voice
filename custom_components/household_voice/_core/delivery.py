"""Durable per-target delivery queue; transport execution belongs to an adapter.

Leases prevent concurrent workers, not duplicate sound after a lost ACK. A
receiver must deduplicate job IDs, or report that exactly-once playback cannot
be guaranteed. Expiry bounds stale announcements; cancellation invalidates leases.
"""
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager


class DeliveryQueue:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS deliveries(
              id TEXT PRIMARY KEY, event_id TEXT NOT NULL, target TEXT NOT NULL,
              payload TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
              attempts INTEGER NOT NULL, available REAL NOT NULL, lease TEXT,
              lease_until REAL, result TEXT)''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(deliveries)')}
            if 'uncertain_on_lease_loss' not in columns:
                db.execute('ALTER TABLE deliveries ADD COLUMN uncertain_on_lease_loss INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, event_id, targets, payload, expires, *, uncertain_on_lease_loss=False):
        with self.db() as db:
            return self._enqueue(db,event_id,targets,payload,expires,uncertain_on_lease_loss=uncertain_on_lease_loss)

    def _enqueue(self, db, event_id, targets, payload, expires, *, uncertain_on_lease_loss=False):
        if not isinstance(event_id,str) or not 1<=len(event_id)<=200:
            raise ValueError('Invalid event ID')
        if type(expires) not in (int,float) or not math.isfinite(expires) or not self.clock()<expires<=self.clock()+86400:
            raise ValueError('Delivery expiry must be within 24 hours')
        if not isinstance(targets,list) or not targets or len(targets)>100 or any(not isinstance(t,str) or not 1<=len(t)<=200 for t in targets):
            raise ValueError('Invalid targets')
        encoded=json.dumps(payload,sort_keys=True,allow_nan=False)
        if len(encoded)>16384:
            raise ValueError('Payload exceeds limit')
        if type(uncertain_on_lease_loss) is not bool:
            raise ValueError('Invalid lease loss policy')
        identifiers=[]
        for target in sorted(set(targets)):
            identifier=hashlib.sha256(json.dumps([event_id,target]).encode()).hexdigest()
            old=db.execute('SELECT * FROM deliveries WHERE id=?',(identifier,)).fetchone()
            if old and (old['payload']!=encoded or old['expires']!=expires or bool(old['uncertain_on_lease_loss'])!=uncertain_on_lease_loss):
                raise ValueError('Delivery identity reused with different content')
            db.execute('''INSERT OR IGNORE INTO deliveries(id,event_id,target,payload,expires,state,attempts,available,lease,lease_until,result,uncertain_on_lease_loss)
                          VALUES(?,?,?,?,?,?,0,?,NULL,NULL,NULL,?)''',
                       (identifier,event_id,target,encoded,expires,'pending',self.clock(),int(uncertain_on_lease_loss)))
            identifiers.append(identifier)
        return identifiers

    def claim(self, lease_seconds=30, max_attempts=5):
        if type(lease_seconds) is not int or not 1<=lease_seconds<=120 or type(max_attempts) is not int or not 1<=max_attempts<=10:
            raise ValueError('Invalid delivery policy')
        with self.db() as db:
            return self._claim(db,lease_seconds,max_attempts)

    def _maintain(self, db, max_attempts, *, identifiers=None):
        now=self.clock()
        suffix=''
        params=()
        if identifiers is not None:
            if not identifiers:
                return
            identifiers=sorted(identifiers)
            if len(identifiers)>500:
                for offset in range(0,len(identifiers),500):
                    self._maintain(db,max_attempts,identifiers=identifiers[offset:offset+500])
                return
            suffix=' AND id IN ('+','.join('?' for _ in identifiers)+')'
            params=tuple(identifiers)
        db.execute("""UPDATE deliveries SET state='uncertain',lease=NULL,lease_until=NULL,result='lease_lost_outcome_unknown'
            WHERE state='leased' AND uncertain_on_lease_loss=1 AND (lease_until<=? OR expires<=?)"""+suffix,(now,now,*params))
        db.execute("UPDATE deliveries SET state='expired',lease=NULL,lease_until=NULL WHERE expires<=? AND state IN ('pending','leased')"+suffix,(now,*params))
        db.execute("UPDATE deliveries SET state='pending',lease=NULL,lease_until=NULL WHERE state='leased' AND lease_until<=?"+suffix,(now,*params))
        db.execute("UPDATE deliveries SET state='failed',result='attempt_limit' WHERE state='pending' AND attempts>=?"+suffix,(max_attempts,*params))

    def _claim(self, db, lease_seconds, max_attempts, *, identifiers=None):
        self._maintain(db,max_attempts,identifiers=identifiers)
        now=self.clock()
        rows=db.execute("SELECT * FROM deliveries WHERE state='pending' AND available<=? ORDER BY available,id",(now,)).fetchall()
        row=next((r for r in rows if identifiers is None or r['id'] in identifiers),None)
        if row is None:
            return None
        lease=uuid.uuid4().hex
        db.execute("UPDATE deliveries SET state='leased',attempts=attempts+1,lease=?,lease_until=? WHERE id=?",(lease,min(now+lease_seconds,row['expires']),row['id']))
        result=dict(db.execute('SELECT * FROM deliveries WHERE id=?',(row['id'],)).fetchone())
        result['payload']=json.loads(result['payload'])
        return result

    def acknowledge(self, identifier, lease, success, retryable=True):
        if type(success) is not bool or type(retryable) is not bool:
            raise ValueError('Acknowledgement flags must be boolean')
        return self.acknowledge_outcome(identifier,lease,'delivered' if success else 'retryable_failure' if retryable else 'permanent_failure')

    def acknowledge_outcome(self, identifier, lease, outcome):
        """Only confirmed pre-service failures may retry; unknown execution never does."""
        with self.db() as db:
            return self._acknowledge(db,identifier,lease,outcome)

    def _acknowledge(self, db, identifier, lease, outcome):
        states={'delivered':'delivered','dry_run':'delivered','retryable_failure':'pending',
                'permanent_failure':'failed','service_outcome_unknown':'uncertain'}
        if not isinstance(outcome,str) or outcome not in states:
            raise ValueError('Invalid delivery outcome')
        row=db.execute('SELECT * FROM deliveries WHERE id=?',(identifier,)).fetchone()
        now=self.clock()
        if not row or row['state']!='leased' or row['lease']!=lease or row['lease_until']<=now or row['expires']<=now:
            raise ValueError('Delivery lease expired or superseded')
        delay=min(60,2**min(row['attempts'],10))
        db.execute('UPDATE deliveries SET state=?,lease=NULL,lease_until=NULL,available=?,result=? WHERE id=?',
                   (states[outcome],now+delay,outcome,identifier))
        return dict(db.execute('SELECT * FROM deliveries WHERE id=?',(identifier,)).fetchone())

    def cancel(self, event_id):
        with self.db() as db:
            db.execute("UPDATE deliveries SET state='cancelled',lease=NULL,lease_until=NULL WHERE event_id=? AND state IN ('pending','leased','uncertain')",(event_id,))

    def list(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM deliveries ORDER BY id')]
