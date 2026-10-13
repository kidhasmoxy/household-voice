"""Durable original-recording fan-out. Authenticated HA owns target selection."""
import hashlib
import json
import re
import uuid

from .api import APIError
from .delivery import DeliveryQueue


class BroadcastService:
    def __init__(self, path, recordings):
        self.recordings = recordings
        self.queue = DeliveryQueue(path, clock=recordings.clock)
        self.clock = recordings.clock
        with self.queue.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS broadcasts(
                id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,signature TEXT NOT NULL,
                recording_id TEXT NOT NULL,expires REAL NOT NULL,cancelled INTEGER NOT NULL DEFAULT 0)''')

    def enqueue(self, recording_id, request_id, targets):
        if (not isinstance(recording_id,str) or not re.fullmatch('[a-f0-9]{32}',recording_id)
                or not isinstance(request_id,str) or not 1<=len(request_id)<=200
                or not isinstance(targets,list) or not 1<=len(targets)<=100):
            raise APIError('Invalid broadcast arguments')
        keys = set()
        for target in targets:
            if not isinstance(target,dict) or target.get('kind') not in ('speaker','phone'):
                raise APIError('Invalid broadcast target')
            fields = {'kind','target'} | ({'device_tracker'} if target['kind']=='phone' else set())
            if set(target)!=fields or any(not isinstance(v,str) for v in target.values()):
                raise APIError('Invalid broadcast target fields')
            pattern = r'media_player\.[a-z0-9_]+' if target['kind']=='speaker' else r'notify\.mobile_app_[a-z0-9_]+'
            if not re.fullmatch(pattern,target['target']) or target['target'] in keys:
                raise APIError('Invalid or duplicate broadcast target')
            if target['kind']=='phone' and not re.fullmatch(r'device_tracker\.[a-z0-9_]+',target['device_tracker']):
                raise APIError('Invalid phone tracker')
            keys.add(target['target'])
        signature = hashlib.sha256(json.dumps([recording_id,sorted(targets,key=lambda t:t['target'])],sort_keys=True).encode()).hexdigest()
        with self.queue.db() as db:
            old = db.execute('SELECT * FROM broadcasts WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['signature']!=signature:
                    raise APIError('Broadcast request reused with different content',409)
                return self._status(db,old['id'])
            try:
                recording = self.recordings.metadata(recording_id)
            except (ValueError, FileNotFoundError):
                raise APIError('Recording unavailable',410) from None
            expires = min(self.clock()+120,recording['expires_at'])
            if expires<=self.clock():
                raise APIError('Recording expired',410)
            identifier = uuid.uuid4().hex
            db.execute('INSERT INTO broadcasts VALUES(?,?,?,?,?,0)',(identifier,request_id,signature,recording_id,expires))
            for target in targets:
                self.queue._enqueue(db,'broadcast:'+identifier,[target['target']],
                    {'recording_id':recording_id,'target':target},expires,uncertain_on_lease_loss=True)
            return self._status(db,identifier)

    def _identifiers(self, db):
        return {r['id'] for r in db.execute("SELECT d.id FROM deliveries d JOIN broadcasts b ON d.event_id='broadcast:'||b.id")}

    def _status(self, db, identifier):
        row = db.execute('SELECT * FROM broadcasts WHERE id=?',(identifier,)).fetchone()
        if not row:
            raise APIError('Unknown broadcast',404)
        ids = self._identifiers(db)
        self.queue._maintain(db,3,identifiers=ids)
        deliveries = [dict(r) for r in db.execute('SELECT id,target,state,result,attempts FROM deliveries WHERE event_id=?',('broadcast:'+identifier,))]
        return {'broadcast_id':identifier,'expires':row['expires'],'cancelled':bool(row['cancelled']),
                'deliveries':deliveries,'physical_delivery_confirmed':False}

    def status(self, broadcast_id):
        with self.queue.db() as db:
            return self._status(db,broadcast_id)

    def cancel(self, broadcast_id):
        with self.queue.db() as db:
            self._status(db,broadcast_id)
            db.execute('UPDATE broadcasts SET cancelled=1 WHERE id=?',(broadcast_id,))
            db.execute("UPDATE deliveries SET state='cancelled',lease=NULL,lease_until=NULL WHERE event_id=? AND state IN ('pending','leased','uncertain')",('broadcast:'+broadcast_id,))
            return self._status(db,broadcast_id)

    def pull(self):
        with self.queue.db() as db:
            job = self.queue._claim(db,90,3,identifiers=self._identifiers(db))
            if job is None:
                return {'delivery':None}
            try:
                job['recording'] = self.recordings.metadata(job['payload']['recording_id'])
            except (ValueError,FileNotFoundError):
                self.queue._acknowledge(db,job['id'],job['lease'],'permanent_failure')
                return {'delivery':None}
            return {'delivery':job}

    def _owned(self, db, identifier, lease):
        row = db.execute("SELECT d.*,b.cancelled FROM deliveries d JOIN broadcasts b ON d.event_id='broadcast:'||b.id WHERE d.id=?",(identifier,)).fetchone()
        return row if row and not row['cancelled'] and row['state']=='leased' and row['lease']==lease and min(row['lease_until'],row['expires'])>self.clock() else None

    def validate(self, identifier, lease):
        with self.queue.db() as db:
            return {'valid':self._owned(db,identifier,lease) is not None}

    def acknowledge(self, identifier, lease, outcome):
        outcomes = {'service_completed':'delivered','service_accepted':'delivered','skipped':'delivered',
            'not_invoked':'retryable_failure','permanent_failure':'permanent_failure','service_outcome_unknown':'service_outcome_unknown'}
        if outcome not in outcomes:
            raise APIError('Invalid broadcast outcome')
        with self.queue.db() as db:
            if self._owned(db,identifier,lease) is None:
                raise APIError('Broadcast lease expired or superseded',409)
            result = self.queue._acknowledge(db,identifier,lease,outcomes[outcome])
            # Preserve truthful transport result rather than calling acceptance
            # verified delivery. Queue state is terminal, not acoustic evidence.
            db.execute('UPDATE deliveries SET result=? WHERE id=?',(outcome,identifier))
            return {'state':result['state'],'outcome':outcome,'physical_delivery_confirmed':False}

    def dispatch(self, operation, payload):
        schemas = {'enqueue':{'recording_id','request_id','targets'},'pull':set(),'validate':{'identifier','lease'},
            'ack':{'identifier','lease','outcome'},'status':{'broadcast_id'},'cancel':{'broadcast_id'}}
        if operation not in schemas or not isinstance(payload,dict) or set(payload)!=schemas[operation]:
            raise APIError('Invalid broadcast operation fields')
        if operation!='enqueue' and any(not isinstance(v,str) or not 1<=len(v)<=200 for v in payload.values()):
            raise APIError('Invalid broadcast argument')
        return getattr(self,{'ack':'acknowledge'}.get(operation,operation))(**payload)
