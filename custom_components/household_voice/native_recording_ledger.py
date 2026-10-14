"""Draft table in existing scheduler SQLite authority; no new service or DB.

Invoke through the owning embedded authority's serialized executor. The table
stores identity/phase/targets only; audio/text remain in existing RecordingStore.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import math
import re
import base64
from pathlib import Path


def recording_transport_policy(scheduler_path,capture_path,recording_id):
    """Read durable origin under the existing embedded authority's state lock.

    No metadata/schema migration. Native handoff associations and capture nonce
    receipts predate this policy. Absence of a native row alone proves nothing:
    only one completed, non-native capture receipt may authorize legacy routing.
    """
    if not isinstance(recording_id,str) or not re.fullmatch('[a-f0-9]{32}',recording_id):
        raise ValueError('recording_origin_unverified')
    scheduler=captures=None
    try:
        scheduler=sqlite3.connect(Path(scheduler_path).resolve().as_uri()+'?mode=ro',uri=True)
        native=scheduler.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_capture_handoffs'").fetchone()
        if native and scheduler.execute('SELECT 1 FROM native_capture_handoffs WHERE recording_id=? LIMIT 1',(recording_id,)).fetchone():
            return 'verified_native'
        captures=sqlite3.connect(Path(capture_path).resolve().as_uri()+'?mode=ro',uri=True)
        rows=captures.execute('SELECT nonce,status FROM capture_uploads WHERE recording_id=? LIMIT 3',(recording_id,)).fetchall()
        if any(isinstance(nonce,str) and nonce.startswith('native_') for nonce,_ in rows):
            return 'verified_native'
        if native and any(scheduler.execute('SELECT 1 FROM native_capture_handoffs WHERE nonce=? LIMIT 1',(nonce,)).fetchone() for nonce,_ in rows):
            return 'verified_native'
        if (len(rows)==1 and isinstance(rows[0][0],str)
                and re.fullmatch('[A-Za-z0-9_-]{16,128}',rows[0][0]) and rows[0][1]=='complete'):
            return 'legacy_ma'
        raise ValueError('recording_origin_unverified')
    except (OSError,sqlite3.Error,TypeError):
        raise ValueError('recording_origin_unverified') from None
    finally:
        if captures is not None:captures.close()
        if scheduler is not None:scheduler.close()


class HandoffLedger:
    def __init__(self,path,clock=time.time):
        self.path=str(path)
        self.clock=clock
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_capture_handoffs(
                request_id TEXT PRIMARY KEY, signature TEXT NOT NULL, source TEXT NOT NULL,
                nonce TEXT NOT NULL UNIQUE, targets TEXT NOT NULL, phase TEXT NOT NULL,
                recording_id TEXT, broadcast_id TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL)''')
            if 'created' not in {row['name'] for row in db.execute('PRAGMA table_info(native_capture_handoffs)')}:
                # Draft-schema upgrade: unknown old creation times expire safely.
                db.execute('ALTER TABLE native_capture_handoffs ADD COLUMN created REAL NOT NULL DEFAULT 0')
            if 'upload_signature' not in {row['name'] for row in db.execute('PRAGMA table_info(native_capture_handoffs)')}:
                db.execute('ALTER TABLE native_capture_handoffs ADD COLUMN upload_signature TEXT')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
            yield db;db.commit()
        except BaseException: db.rollback();raise
        finally: db.close()

    def _row(self,row):
        if row is None:return None
        result=dict(row);result['targets']=json.loads(result['targets']);return result

    def get(self,request_id):
        with self.db() as db:
            return self._row(db.execute('SELECT * FROM native_capture_handoffs WHERE request_id=?',(request_id,)).fetchone())

    def latest(self,source):
        with self.db() as db:
            return self._row(db.execute('SELECT * FROM native_capture_handoffs WHERE source=? ORDER BY rowid DESC LIMIT 1',(source,)).fetchone())

    def begin(self,request_id,source,targets):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:raise ValueError('invalid_request')
        signature=hashlib.sha256(json.dumps([source.__dict__,targets],sort_keys=True).encode()).hexdigest()
        nonce='native_'+hashlib.sha256(request_id.encode()).hexdigest()
        with self.db() as db:
            old=db.execute('SELECT * FROM native_capture_handoffs WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['signature']!=signature:raise ValueError('request_reused')
                return self._row(old),False
            if db.execute('SELECT COUNT(*) FROM native_capture_handoffs').fetchone()[0]>=10000:
                raise ValueError('handoff_capacity')
            db.execute('INSERT INTO native_capture_handoffs VALUES(?,?,?,?,?,?,NULL,NULL,0,?,NULL)',
                (request_id,signature,source.device_id,nonce,json.dumps(targets,sort_keys=True),'reserved',self.clock()))
            return self._row(db.execute('SELECT * FROM native_capture_handoffs WHERE request_id=?',(request_id,)).fetchone()),True

    def advance(self,request_id,expected,phase,*,recording_id=None,broadcast_id=None,upload_signature=None):
        allowed={('reserved','capturing'),('capturing','uploading'),('uploading','ready'),
                 ('ready','queued'),('reserved','cancelled'),('capturing','cancelled')}
        if (expected,phase) not in allowed:raise ValueError('invalid_transition')
        with self.db() as db:
            row=db.execute('SELECT * FROM native_capture_handoffs WHERE request_id=?',(request_id,)).fetchone()
            if row is None or row['phase']!=expected:raise ValueError('handoff_conflict')
            db.execute('UPDATE native_capture_handoffs SET phase=?,recording_id=COALESCE(?,recording_id),broadcast_id=COALESCE(?,broadcast_id),upload_signature=COALESCE(?,upload_signature) WHERE request_id=?',
                (phase,recording_id,broadcast_id,upload_signature,request_id))

    def cancel(self,request_id):
        with self.db() as db:
            db.execute('UPDATE native_capture_handoffs SET cancel_requested=1 WHERE request_id=?',(request_id,))
            return self._row(db.execute('SELECT * FROM native_capture_handoffs WHERE request_id=?',(request_id,)).fetchone())

    def pending(self):
        with self.db() as db:
            return [self._row(row) for row in db.execute("SELECT * FROM native_capture_handoffs WHERE phase IN ('uploading','ready','queued')")]

    def broadcast_for_request(self,request_id):
        with self.db() as db:
            row=db.execute('SELECT id FROM broadcasts WHERE request_id=?',(request_id,)).fetchone()
            return row['id'] if row else None


class HandoffAuthority:
    """All methods use the existing embedded authority's serialized executor.

    The same outer authority lock must cover enqueue/cancel and normal broadcast
    pulls. Do not run this through an unrelated executor or a separate server.
    """
    def __init__(self,ledger,broadcasts,captures=None,binding_snapshot=None):
        self.ledger,self.broadcasts=ledger,broadcasts
        self.captures,self.binding_snapshot=captures,binding_snapshot

    def resolve_upload(self,request_id,source):
        """Identity-only receipt reconciliation under existing authority lock.

        No recapture, PCM retransmission, reservation completion or file creation.
        Missing/pending/corrupt/mismatched receipts remain unknown.
        """
        row=self.ledger.get(request_id)
        if row is None or row['phase']!='uploading' or row['cancel_requested'] or self.captures is None:return None
        now=self.ledger.clock()
        if not row['created']<=now<row['created']+120:return None
        expected=hashlib.sha256(json.dumps([source.__dict__,row['targets']],sort_keys=True).encode()).hexdigest()
        if row['source']!=source.device_id or row['signature']!=expected:return None
        signature=row['upload_signature']
        if not isinstance(signature,str) or not re.fullmatch('[a-f0-9]{64}',signature):return None
        bindings=self.binding_snapshot() if self.binding_snapshot else self.captures.bindings
        if bindings.get(source.profile_id)!={'source_device':source.device_id,'source_room':source.area_id}:return None
        connection=None
        try:
            connection=sqlite3.connect(Path(self.captures.path).resolve().as_uri()+'?mode=ro',uri=True)
            receipt=connection.execute('SELECT signature,recording_id,first_seen,status FROM capture_uploads WHERE nonce=?',(row['nonce'],)).fetchone()
            if receipt is None:return None
            signed,identifier,first_seen,status=receipt
            if (status!='complete' or signed!=signature or not re.fullmatch('[a-f0-9]{32}',identifier)
                    or type(first_seen) not in (int,float) or not math.isfinite(first_seen)
                    or not row['created']<=first_seen<=now<first_seen+86400):return None
            metadata=self.captures.recordings.metadata(identifier)
            if (metadata.get('id')!=identifier or metadata.get('source_room')!=source.area_id
                    or metadata.get('created_at')!=first_seen or metadata.get('expires_at')!=first_seen+86400
                    or metadata.get('kind')!='original_uploaded_audio' or metadata.get('capture_verified') is not False):return None
            raw=self.captures.recordings.audio(identifier)
            if hashlib.sha256(raw).hexdigest()!=metadata.get('sha256'):return None
            original={'profile_id':source.profile_id,'source_device':source.device_id,'nonce':row['nonce'],
                      'wav_base64':base64.b64encode(raw).decode('ascii'),'transcript':metadata.get('transcript')}
            if hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=signature:return None
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError):return None
        finally:
            if connection is not None:connection.close()
        self.ledger.advance(request_id,'uploading','ready',recording_id=identifier)
        return {'recording_id':identifier}

    def recover_upload_and_enqueue(self,request_id,source):
        # One owning executor call holds the existing outer state lock across
        # receipt validation, CAS and conditional enqueue/cancel check.
        resolved=self.resolve_upload(request_id,source)
        return self.enqueue(request_id) if resolved is not None else None

    def enqueue(self,request_id):
        row=self.ledger.get(request_id)
        if row is None or row['phase'] not in ('ready','queued'):raise ValueError('handoff_not_ready')
        if row['cancel_requested']:return self.cancel(request_id)
        if row['phase']=='ready' and not row['created']<=self.ledger.clock()<row['created']+120:
            return self.cancel(request_id)
        result=self.broadcasts.enqueue(row['recording_id'],request_id,row['targets'])
        if row['phase']=='ready':
            self.ledger.advance(request_id,'ready','queued',broadcast_id=result['broadcast_id'])
        return result

    def cancel(self,request_id):
        row=self.ledger.cancel(request_id)
        identifier=self.ledger.broadcast_for_request(request_id)
        if identifier:return self.broadcasts.cancel(identifier)
        return {'cancelled':True,'broadcast_id':None,'physical_delivery_confirmed':False}
