"""Explicit recording import and dry-run fan-out. No microphone or notifications."""
import base64
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid
import wave

from .routing import home_phones


def validate_wav(encoded):
    """Validate an upload before reserving storage; return decoded PCM WAV and frames."""
    if not isinstance(encoded,(str,bytes,bytearray)) or len(encoded)>1_500_000:
        raise ValueError("Recording limit exceeded")
    raw=base64.b64decode(encoded, validate=True)
    with wave.open(io.BytesIO(raw),"rb") as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2 or w.getframerate()!=16000:
            raise ValueError("Use mono 16-bit PCM WAV at 16000 Hz")
        frames=w.getnframes()
        if not 1600 <= frames <= 480000 or w.getcomptype()!="NONE":
            raise ValueError("Audio must be 0.1..30 seconds of PCM")
        if len(w.readframes(frames)) != frames*2:
            raise ValueError("Truncated audio")
    return raw,frames


class RecordingStore:
    def __init__(self, root, signing_key, clock=time.time):
        self.root=Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if len(signing_key)<32:
            raise ValueError("Audio signing key must have at least 32 characters")
        self.key, self.clock=signing_key.encode(), clock
        self._write_lock=threading.RLock()

    def _path(self, identifier, suffix):
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid recording ID")
        return self.root/(identifier+suffix)

    def import_wav(self, encoded, source_room, transcript="", retention=86400, *, identifier=None, created_at=None):
        """Import once; optional identity/time come only from a durable trusted reservation.

        Existing files are never replaced. A retry verifies a completed pair at
        the API layer or waits for the owned-orphan sweep before writing again.
        """
        if type(retention) not in (int,float) or not math.isfinite(retention) or not 0 < retention <= 86400:
            raise ValueError("Recording limit exceeded")
        raw,frames=validate_wav(encoded)
        if not isinstance(source_room,str) or not 1<=len(source_room)<=120 or not isinstance(transcript,str) or len(transcript)>8000:
            raise ValueError("Invalid metadata")
        source_room.encode('utf-8'); transcript.encode('utf-8')
        identifier=uuid.uuid4().hex if identifier is None else identifier
        now=self.clock()
        created_at=now if created_at is None else created_at
        if type(created_at) not in (int,float) or not math.isfinite(created_at) or created_at>now or created_at+retention<=now:
            raise ValueError("Recording reservation expired or invalid")
        meta={"id":identifier,"source_room":source_room,"transcript":transcript,
              "created_at":created_at,"expires_at":created_at+retention,
              "duration":frames/16000,"sha256":hashlib.sha256(raw).hexdigest(),
              "kind":"original_uploaded_audio","capture_verified":False}
        # Metadata is the commit marker: readers cannot discover a recording
        # until both complete files exist. A crash between the two renames is
        # recovered by purge's orphan sweep. One service process owns this root.
        audio_tmp=self._path(identifier,".wav.tmp")
        meta_tmp=self._path(identifier,".json.tmp")
        audio_path=self._path(identifier,".wav")
        meta_path=self._path(identifier,".json")
        with self._write_lock:
            owned=set()
            try:
                if any(path.exists() or path.is_symlink() for path in (audio_tmp,meta_tmp,audio_path,meta_path)):
                    raise FileExistsError("Recording identifier already exists")
                for path, content in ((audio_tmp,raw),(meta_tmp,json.dumps(meta).encode("utf-8"))):
                    with path.open("xb") as output:
                        owned.add(path)
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                os.replace(audio_tmp,audio_path)
                owned.discard(audio_tmp)
                owned.add(audio_path)
                os.replace(meta_tmp,meta_path)
                owned.discard(meta_tmp)
                owned.add(meta_path)
            except BaseException:
                # Preserve the original error. If cleanup itself cannot write,
                # maintenance can retry once the filesystem becomes available.
                for path in owned:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
        return meta

    def metadata(self, identifier):
        meta=json.loads(self._path(identifier,".json").read_text(encoding="utf-8"))
        if meta["expires_at"]<=self.clock():
            raise ValueError("Recording expired")
        return meta

    def audio(self, identifier):
        self.metadata(identifier)
        return self._path(identifier,".wav").read_bytes()

    def signed_query(self, identifier, ttl=120):
        meta=self.metadata(identifier)
        if not 1<=ttl<=300:
            raise ValueError("Link lifetime must be 1..300 seconds")
        expiry=int(min(meta["expires_at"],self.clock()+ttl))
        signature=hmac.new(self.key,f"{identifier}:{expiry}".encode(),hashlib.sha256).hexdigest()
        return f"expires={expiry}&sig={signature}"

    def authorized_audio(self, identifier, expiry, signature):
        # A URL token permits only this one audio object until expiry.
        if not isinstance(expiry,int) or expiry <= self.clock() or expiry > self.clock()+301:
            raise ValueError("Audio link expired or invalid")
        expected=hmac.new(self.key,f"{identifier}:{expiry}".encode(),hashlib.sha256).hexdigest()
        if not isinstance(signature,str) or not re.fullmatch(r"[a-f0-9]{64}",signature) or not hmac.compare_digest(expected,signature):
            raise ValueError("Invalid audio signature")
        return self.audio(identifier)

    def purge(self, orphan_grace=300):
        """Remove expired recordings and stale incomplete writes owned by us.

        The private recording directory must have one service writer. Grace
        protects recent incomplete files after restart; the local lock protects
        imports against the maintenance thread. Unrelated files and symlinks
        are never swept. Return the number of recording groups removed.
        """
        if type(orphan_grace) not in (int,float) or not math.isfinite(orphan_grace) or orphan_grace<0:
            raise ValueError("Invalid orphan grace period")
        count=0
        with self._write_lock:
            groups={}
            for path in self.root.iterdir():
                match=re.fullmatch(r"([a-f0-9]{32})(?:\.wav|\.json|\.tmp|\.wav\.tmp|\.json\.tmp)",path.name)
                if match:
                    groups.setdefault(match[1],[]).append(path)
            now=self.clock()
            for identifier,paths in groups.items():
                try:
                    if any(path.is_symlink() or not path.is_file() for path in paths):
                        continue
                    meta_path=self._path(identifier,".json")
                    try:
                        meta=json.loads(meta_path.read_text(encoding="utf-8"))
                        expiry=meta.get("expires_at") if isinstance(meta,dict) else None
                        valid=(isinstance(meta,dict) and meta.get("id")==identifier
                               and type(expiry) in (int,float) and math.isfinite(expiry))
                    except (ValueError,OSError):
                        valid=False
                    if valid and expiry<=now:
                        remove=True
                    elif valid and self._path(identifier,".wav").is_file():
                        remove=False
                    else:
                        remove=now-max(path.stat().st_mtime for path in paths)>=orphan_grace
                    if remove:
                        for path in paths:
                            path.unlink(missing_ok=True)
                        count+=1
                except OSError:
                    # A later maintenance pass retries filesystem failures.
                    continue
        return count


def plan_delivery(meta, registry, phones, now, rooms=None, group=None, notify_home_phones=True):
    if meta["expires_at"]<=now:
        raise ValueError("Expired announcement")
    return {"announcement_id":meta["id"],"mode":"dry_run","audio_kind":meta["kind"],
            "speakers":registry.announcement_targets(rooms,group),"phones":home_phones(phones,now) if notify_home_phones else [],
            "notification_text":meta.get('transcript',''),
            "sent":False,"capture_verified":meta["capture_verified"],
            "note":"Plan only: no speakers or phones were contacted"}

