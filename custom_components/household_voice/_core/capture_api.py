"""Authenticated adapter upload contract; not an LLM tool or capture attestation.

The transport must authenticate before entry. Device/profile bindings come from
private deployment configuration. A nonce prevents duplicate storage on retries;
metadata remains capture_verified=False until a physical capture path is proven.
No announcement or phone action is implicit in an upload.
"""
import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import closing

from .api import APIError
from .announcements import validate_wav


def room_capture_bindings(rooms):
    bindings = {}
    for room, config in rooms.items():
        profiles = config.get('capture_profiles', {})
        if not isinstance(profiles, dict):
            raise ValueError('Invalid room capture profiles')
        for profile, device in profiles.items():
            if profile in bindings or device not in config.get('satellites', []):
                raise ValueError('Ambiguous or unregistered capture source')
            bindings[profile] = {'source_device': device, 'source_room': room}
    return bindings


class CaptureAPI:
    def __init__(self, path, recordings, bindings):
        self.path, self.recordings = str(path), recordings
        self.bindings = dict(bindings)
        for profile, value in self.bindings.items():
            if (not isinstance(profile, str) or not profile or not isinstance(value, dict)
                    or set(value) != {'source_device', 'source_room'}
                    or any(not isinstance(x, str) or not x for x in value.values())):
                raise ValueError('Invalid capture profile binding')
        try:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS capture_uploads(nonce TEXT PRIMARY KEY, signature TEXT NOT NULL, recording_id TEXT NOT NULL)')
                columns={row[1] for row in db.execute('PRAGMA table_info(capture_uploads)')}
                if 'first_seen' not in columns:
                    db.execute('ALTER TABLE capture_uploads ADD COLUMN first_seen REAL')
                if 'status' not in columns:
                    # Old rows represented completed files; a missing old object must
                    # never be recreated with an invented retention start time.
                    db.execute("ALTER TABLE capture_uploads ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'")
                db.commit()
        except sqlite3.Error:
            raise APIError('Capture storage unavailable',503) from None

    def upload(self, payload):
        if not isinstance(payload, dict) or set(payload) != {'profile_id', 'source_device', 'nonce', 'wav_base64', 'transcript'}:
            raise APIError('Invalid capture fields')
        if any(not isinstance(v, str) for v in payload.values()):
            raise APIError('Invalid capture field types')
        binding = self.bindings.get(payload['profile_id'])
        if binding is None or binding['source_device'] != payload['source_device']:
            raise APIError('Capture source is not configured', 403)
        if (not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', payload['nonce'])
                or len(payload['wav_base64']) > 1_500_000 or len(payload['transcript']) > 8000):
            raise APIError('Capture exceeds limits')
        try:
            payload['transcript'].encode('utf-8')
            raw,frames=validate_wav(payload['wav_base64'])
            signature=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        except (ValueError,UnicodeError,TypeError,EOFError):
            raise APIError('Invalid recording') from None
        except Exception:
            # WAV parser errors must not reveal audio/transcript contents.
            raise APIError('Invalid recording') from None
        reservation=self._reserve(payload['nonce'],signature)
        identifier,first_seen,status=reservation
        if status not in ('reserved','complete'):
            raise APIError('Capture storage unavailable',503)
        now=self.recordings.clock()
        if first_seen is not None and (type(first_seen) not in (int,float) or not math.isfinite(first_seen)):
            raise APIError('Capture storage unavailable',503)
        if first_seen is not None and now>=first_seen+86400:
            raise APIError('Capture has expired',410)
        try:
            metadata=self._existing(identifier,raw,frames,binding['source_room'],payload['transcript'],first_seen)
        except FileNotFoundError:
            if status=='complete' or first_seen is None:
                raise APIError('Capture unavailable or expired',410) from None
            # Stable identifier was already committed. Exclusive store writes will
            # refuse any partial files; only its own orphan sweep may remove them.
            try:
                metadata=self.recordings.import_wav(payload['wav_base64'],binding['source_room'],payload['transcript'],
                    identifier=identifier,created_at=first_seen)
            except FileExistsError:
                # A concurrent same-nonce writer may have finished in the meantime.
                try:
                    metadata=self._existing(identifier,raw,frames,binding['source_room'],payload['transcript'],first_seen)
                except Exception:
                    raise APIError('Recording storage unavailable',503) from None
            except Exception:
                raise APIError('Recording storage unavailable',503) from None
        except (OSError,ValueError,KeyError,TypeError):
            raise APIError('Recording storage unavailable',503) from None
        self._complete(payload['nonce'],signature,identifier)
        return {'mode':'dry_run','ha_connected':False,'recording':metadata,'announcement_sent':False}

    def _reserve(self,nonce,signature):
        """Commit identity before creating a file; SQLite commit uncertainty is safe to retry."""
        try:
            with closing(sqlite3.connect(self.path,timeout=10,isolation_level=None)) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT signature,recording_id,first_seen,status FROM capture_uploads WHERE nonce=?',(nonce,)).fetchone()
                if old:
                    if old[0]!=signature:
                        raise APIError('Capture nonce reused with different content',409)
                    result=old[1:]
                else:
                    first_seen=self.recordings.clock()
                    if type(first_seen) not in (int,float) or not math.isfinite(first_seen):
                        raise APIError('Capture storage unavailable',503)
                    identifier=uuid.uuid4().hex
                    db.execute("INSERT INTO capture_uploads(nonce,signature,recording_id,first_seen,status) VALUES(?,?,?,?,'reserved')",
                               (nonce,signature,identifier,first_seen))
                    result=(identifier,first_seen,'reserved')
                db.commit()
                return result
        except sqlite3.Error:
            raise APIError('Capture storage unavailable',503) from None

    def _complete(self,nonce,signature,identifier):
        try:
            with closing(sqlite3.connect(self.path,timeout=10)) as db:
                db.execute('PRAGMA synchronous=FULL')
                db.execute("UPDATE capture_uploads SET status='complete' WHERE nonce=? AND signature=? AND recording_id=?",
                           (nonce,signature,identifier))
                db.commit()
        except sqlite3.Error:
            raise APIError('Capture storage unavailable',503) from None

    def _existing(self,identifier,raw,frames,room,transcript,first_seen):
        metadata=self.recordings.metadata(identifier)
        if not isinstance(metadata,dict):
            raise ValueError('Malformed recording metadata')
        expected={'id':identifier,'source_room':room,'transcript':transcript,'duration':frames/16000,
                  'sha256':hashlib.sha256(raw).hexdigest(),'kind':'original_uploaded_audio','capture_verified':False}
        if first_seen is not None:
            expected.update(created_at=first_seen,expires_at=first_seen+86400)
        if metadata.get('capture_verified') is not False or any(metadata.get(k)!=v for k,v in expected.items()) or self.recordings.audio(identifier)!=raw:
            raise ValueError('Recording does not match reservation')
        return metadata

    def resolve(self, identifier):
        try:
            metadata = self.recordings.metadata(identifier)
            query = self.recordings.signed_query(identifier)
        except (ValueError, FileNotFoundError):
            raise APIError('Recording unavailable', 404) from None
        except OSError:
            raise APIError('Recording storage unavailable',503) from None
        return {'recording': metadata, 'audio_path': '/audio/' + identifier + '?' + query}


def audio_slice(raw, range_header):
    """Single byte range for speaker HTTP clients; no multipart amplification."""
    headers = {'Cache-Control': 'no-store', 'Accept-Ranges': 'bytes'}
    size = len(raw)
    if range_header is None:
        return 200, raw, {**headers, 'Content-Length': str(size)}
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header)
    if not match or not any(match.groups()):
        return 416, b'', {**headers, 'Content-Range': f'bytes */{size}'}
    try:
        if not match[1]:
            count = int(match[2])
            if count <= 0:
                raise ValueError
            start, end = max(0, size-count), size-1
        else:
            start = int(match[1])
            end = min(size-1, int(match[2])) if match[2] else size-1
        if not 0 <= start <= end < size:
            raise ValueError
    except ValueError:
        return 416, b'', {**headers, 'Content-Range': f'bytes */{size}'}
    content = raw[start:end+1]
    return 206, content, {**headers, 'Content-Length': str(len(content)), 'Content-Range': f'bytes {start}-{end}/{size}'}
