"""Scoped EchoMuse wire protocol with durable, at-most-once dispatch admission.

The ledger stores neither audio URLs nor keys. A committed dispatch whose outcome
was not committed is quarantined after restart; it is never retried implicitly.
MA workflow completion is not acoustic proof. Only a dedicated HMAC key is accepted.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

from aiohttp import web


class NativeMode(ValueError):
    """Trusted assistant is unpaired: choose native only before any turn action."""


class CommittedCancellation(asyncio.CancelledError):
    """Executor commit finished; preserve its result for scoped cleanup only."""
    def __init__(self,result):
        super().__init__()
        self.result=result


@dataclass(frozen=True)
class Source:
    key: bytes
    device: str
    assistant: str
    controller_device: str
    enabled: bool = False


@dataclass(frozen=True)
class Binding:
    device: str
    assistant: str
    area: str
    player: str
    aliases: tuple[str, ...]
    ducks: tuple[str, ...]
    volume: float = .5
    provider_entry_id: str = ''
    native_player_id: str = ''

    @property
    def identity(self):
        # Playing/idle competitors may change; the admitted output must not.
        return self.device, self.assistant, self.area, self.player, self.aliases,self.provider_entry_id,self.native_player_id


class Ledger:
    """Single-runtime SQLite ledger; all callers serialize through endpoint.lock."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute() or self.path.is_symlink():
            raise ValueError('A private absolute ledger path is required')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('Corrupt EchoMuse ledger')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError('Unsupported EchoMuse ledger')
            if version==0 and db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Unknown EchoMuse ledger schema')
            db.execute('CREATE TABLE IF NOT EXISTS nonces (key_id TEXT, nonce TEXT, seen REAL, PRIMARY KEY(key_id,nonce))')
            db.execute('CREATE TABLE IF NOT EXISTS turns (key_id TEXT, turn TEXT, created REAL, state TEXT, player TEXT, provider_entry_id TEXT NOT NULL DEFAULT \'\', native_player_id TEXT NOT NULL DEFAULT \'\', PRIMARY KEY(key_id,turn))')
            if version==1:
                db.execute("ALTER TABLE turns ADD COLUMN provider_entry_id TEXT NOT NULL DEFAULT ''")
                db.execute("ALTER TABLE turns ADD COLUMN native_player_id TEXT NOT NULL DEFAULT ''")
            db.execute('PRAGMA user_version=2')
            # A lost receiver cannot resume an old turn. A committed dispatch
            # might already have reached MA; only an administrator can clear it.
            db.execute("UPDATE turns SET state='unknown' WHERE state='dispatched'")
            db.execute("UPDATE turns SET state='ended' WHERE state IN ('admitted','completed')")
        self.path.chmod(0o600)

    def reserve(self, key_id, nonce, turn, operation, now, player='', aliases=(), provider_entry_id='', native_player_id=''):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM nonces WHERE seen < ?', (now-120,))
            db.execute("DELETE FROM turns WHERE created < ? AND state='ended'", (now-86400,))
            if db.execute('SELECT 1 FROM nonces WHERE key_id=? AND nonce=?', (key_id,nonce)).fetchone():
                return 'replay'
            if db.execute('SELECT count(*) FROM nonces').fetchone()[0] >= 4096:
                return 'busy'
            db.execute('INSERT INTO nonces VALUES (?,?,?)', (key_id,nonce,now))
            row = db.execute('SELECT * FROM turns WHERE key_id=? AND turn=?', (key_id,turn)).fetchone()
            if operation == 'begin':
                if row or db.execute("SELECT 1 FROM turns WHERE key_id=? AND state IN ('admitted','dispatched','completed','unknown')", (key_id,)).fetchone():
                    return 'conflict'
                targets={player,*aliases}- {''}
                if any(item['player'] in targets or (provider_entry_id and native_player_id and (item['provider_entry_id'],item['native_player_id'])==(provider_entry_id,native_player_id)) for item in db.execute("SELECT player,provider_entry_id,native_player_id FROM turns WHERE state='unknown'")):
                    return 'conflict'
                if db.execute('SELECT count(*) FROM turns').fetchone()[0] >= 10000:
                    return 'busy'
                db.execute('INSERT INTO turns VALUES (?,?,?,?,?,?,?)', (key_id,turn,now,'admitted',player,provider_entry_id,native_player_id))
            elif operation == 'play':
                if row is None or row['state'] != 'admitted' or now-row['created'] > 120:
                    return 'expired'
                db.execute("UPDATE turns SET state='dispatched' WHERE key_id=? AND turn=?", (key_id,turn))
            elif row is None:
                return 'expired'
            return 'reserved'

    def finish(self, key_id, turn, state):
        with self._db() as db:
            # End/cancel cannot clear a possible remote output.
            if state == 'completed':
                db.execute("UPDATE turns SET state='completed' WHERE key_id=? AND turn=? AND state='dispatched'", (key_id,turn))
            elif state == 'unknown':
                db.execute("UPDATE turns SET state='unknown' WHERE key_id=? AND turn=? AND state='dispatched'", (key_id,turn))
            elif state == 'not_invoked':
                db.execute("UPDATE turns SET state='ended' WHERE key_id=? AND turn=? AND state='dispatched'", (key_id,turn))
            else:
                db.execute("UPDATE turns SET state=CASE WHEN state='dispatched' THEN 'unknown' ELSE 'ended' END WHERE key_id=? AND turn=? AND state!='unknown'", (key_id,turn))

    def rows(self):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT key_id,turn,created,state,player,provider_entry_id,native_player_id FROM turns ORDER BY created')]

    def recover(self, key_id):
        with self._db() as db:
            db.execute("UPDATE turns SET state='ended' WHERE key_id=? AND state='unknown'", (key_id,))


def _object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def tts_url_validator(allowed_origins):
    origins = frozenset(allowed_origins)
    if not origins:
        raise ValueError('Explicit HA audio origins required')
    def validate(url):
        if not isinstance(url,str) or not url or any(ord(c)<33 for c in url) or '\\' in url:
            raise ValueError('Invalid TTS URL')
        parsed = urlsplit(url)
        if (parsed.username or parsed.password or parsed.fragment or parsed.query
                or f'{parsed.scheme}://{parsed.netloc}' not in origins
                or not re.fullmatch(r'/api/tts_proxy/[A-Za-z0-9_.-]+',parsed.path)):
            raise ValueError('Expected this HA TTS proxy')
        return url
    return validate


class EchoMuseAudioEndpoint:
    def __init__(self, sources, transport, focus, resolve, validate_url, ledger, *, enabled=False, timeout=60, clock=time.time):
        if not 0 < timeout <= 60:
            raise ValueError('Invalid deadline')
        self.sources,self.transport,self.focus,self.resolve = sources,transport,focus,resolve
        self.validate_url,self.ledger,self.enabled = validate_url,ledger,enabled
        self.timeout,self.clock = timeout,clock
        self.lock = asyncio.Lock()
        self.turns = {}
        self.ticker = None
        self.failed = False
        self.handlers = set()

    def active(self):
        return self.focus is not None and not self.failed and (self.enabled() if callable(self.enabled) else self.enabled) is True

    @staticmethod
    def response(turn,outcome,status=200):
        return web.json_response({'turn':turn,'outcome':outcome,'acoustic_delivery_confirmed':False},status=status)

    async def _write(self, method, *args):
        # Don't release lock while an executor transaction is still committing.
        task = asyncio.create_task(asyncio.to_thread(method,*args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            result=await task
            raise CommittedCancellation(result) from None
        except (OSError,sqlite3.Error,ValueError):
            self.failed = True
            raise

    async def start(self):
        await self._write(self.ledger.start)
        async def tick():
            while True:
                await asyncio.sleep(1)
                await self.expire()
        self.ticker = asyncio.create_task(tick())

    async def post(self,request):
        task=asyncio.current_task()
        self.handlers.add(task)
        try:
            return await self._post(request)
        finally:
            self.handlers.discard(task)

    async def _post(self,request):
        if not self.active():
            return self.response('','disabled',503)
        try:
            async with asyncio.timeout(5):
                raw = bytearray()
                while chunk := await request.content.read(min(8193-len(raw),4096)):
                    raw.extend(chunk)
                    if len(raw)>8192:
                        return self.response('','invalid',413)
        except (TimeoutError,ConnectionError,ValueError):
            return self.response('','invalid',400)
        key_id = request.headers.get('X-HV-Key','')
        source = self.sources.get(key_id)
        signature = request.headers.get('X-HV-Signature','')
        if (source is None or source.enabled is not True or not re.fullmatch('[a-f0-9]{64}',signature)
                or not hmac.compare_digest(hmac.new(source.key,raw,hashlib.sha256).hexdigest(),signature)):
            return self.response('','unauthorized',401)
        try:
            packet = json.loads(raw,object_pairs_hook=_object)
            if (not isinstance(packet,dict) or set(packet)!={'version','operation','turn','media_url','at','nonce'}
                    or type(packet['version']) is not int or packet['version']!=1
                    or type(packet['at']) is not int or abs(self.clock()-packet['at'])>30
                    or packet['operation'] not in {'begin','play','end','cancel'}
                    or not re.fullmatch('[a-f0-9]{32}',packet['turn'])
                    or not re.fullmatch('[a-f0-9]{32}',packet['nonce'])
                    or not isinstance(packet['media_url'],str) or len(packet['media_url'])>4096):
                raise ValueError()
            if packet['operation']=='play':
                self.validate_url(packet['media_url'])
            elif packet['media_url']:
                raise ValueError()
        except (TypeError,ValueError,UnicodeError,KeyError):
            return self.response('','invalid',400)
        try:
            return await self._operate(key_id,source,packet)
        except (OSError,sqlite3.Error,ValueError):
            return self.response(packet['turn'],'unavailable',503)

    async def _operate(self,key_id,source,packet):
        turn,operation = packet['turn'],packet['operation']
        ident = key_id,turn
        async with self.lock:
            if not self.active() or self.sources.get(key_id) != source:
                return self.response(turn,'disabled',503)
            state = self.turns.get(ident)
            binding = None
            native = False
            if operation in {'begin','play'}:
                try:
                    binding = self.resolve(source)
                except NativeMode:
                    if operation!='begin':
                        return self.response(turn,'binding_changed',409)
                    native=True
                except (ValueError,LookupError):
                    return self.response(turn,'binding_unavailable',409)
            if operation=='play' and (state is None or binding.identity!=state['binding'].identity):
                return self.response(turn,'binding_changed',409)
            try:
                reserved = await self._write(self.ledger.reserve,key_id,packet['nonce'],turn,operation,self.clock(),binding.player if binding else '',binding.aliases if binding else (),binding.provider_entry_id if binding else '',binding.native_player_id if binding else '')
            except CommittedCancellation as error:
                # The executor transaction completed before _write propagates
                # cancellation. No focus/provider action has started yet.
                if error.result=='reserved' and operation in {'begin','play'}:
                    await self._write(self.ledger.finish,key_id,turn,'ended' if operation=='begin' else 'not_invoked')
                raise
            if reserved!='reserved':
                return self.response(turn,reserved,503 if reserved=='busy' else 409)
            if native:
                await self._write(self.ledger.finish,key_id,turn,'ended')
                result={'turn':turn,'nonce':packet['nonce'],'outcome':'native','acoustic_delivery_confirmed':False}
                raw=json.dumps(result,sort_keys=True,separators=(',',':')).encode()
                signature=hmac.new(source.key,b'response\0'+raw,hashlib.sha256).hexdigest()
                return web.Response(body=raw,content_type='application/json',headers={'X-HV-Response-Signature':signature})
            if operation=='begin':
                state = {'created':self.clock(),'binding':binding,'task':None,'active':True}
                self.turns[ident] = state
                try:
                    await self.focus.begin(source.device,turn,binding)
                except BaseException:
                    self.turns.pop(ident,None)
                    await self._write(self.ledger.finish,key_id,turn,'ended')
                    await self.focus.end(source.device,turn)
                    return self.response(turn,'failed',503)
                return self.response(turn,'completed')
            if operation in {'end','cancel'}:
                if state is None:
                    return self.response(turn,'expired',409)
                state['active']=False
                self.turns.pop(ident,None)
                task=state['task']
                if task and not task.done():
                    task.cancel()
            else:
                async def current():
                    if not self.active() or not state['active'] or self.sources.get(key_id)!=source or self.clock()-state['created']>120:
                        return False
                    try:
                        fresh = self.resolve(source)
                        if fresh.identity!=binding.identity:
                            return False
                        await self.focus.prepare_play(source.device,turn,fresh)
                        return self.active() and state['active'] and self.resolve(source).identity==binding.identity
                    except (ValueError,LookupError):
                        return False
                task=asyncio.create_task(self.transport.play(binding.player,packet['media_url'],volume=binding.volume,chime=False,before_dispatch=current))
                state['task']=task
        if operation in {'end','cancel'}:
            if task:
                await asyncio.gather(task,return_exceptions=True)
            async with self.lock:
                await self._write(self.ledger.finish,key_id,turn,'ended')
            await self.focus.end(source.device,turn)
            return self.response(turn,'completed')
        outcome='unknown'
        try:
            result=await asyncio.wait_for(task,self.timeout)
            if result.get('provider_completed') is True:
                outcome='completed'
            elif result.get('outcome')=='not_invoked':
                outcome='not_invoked'
        except (asyncio.CancelledError,Exception):
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        async with self.lock:
            await self._write(self.ledger.finish,key_id,turn,outcome)
        return self.response(turn,outcome)

    async def expire(self, *, all_turns=False):
        async with self.lock:
            stale=[(ident,state) for ident,state in self.turns.items() if all_turns or self.clock()-state['created']>120]
            for ident,state in stale:
                state['active']=False
                self.turns.pop(ident,None)
                if state['task']:
                    state['task'].cancel()
        for (key_id,turn),state in stale:
            if state['task']:
                await asyncio.gather(state['task'],return_exceptions=True)
            async with self.lock:
                await self._write(self.ledger.finish,key_id,turn,'unknown' if state['task'] and state['task'].cancelled() else 'ended')
            await self.focus.end(state['binding'].device,turn)

    async def close(self):
        self.enabled=False
        if self.ticker:
            self.ticker.cancel()
            await asyncio.gather(self.ticker,return_exceptions=True)
        await self.expire(all_turns=True)
        await self.drain()

    async def drain(self):
        handlers=[task for task in self.handlers if task is not asyncio.current_task()]
        if handlers:
            await asyncio.gather(*handlers,return_exceptions=True)
