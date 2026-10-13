"""Durable coordinator hosted directly inside Home Assistant.

No listener, bearer token, MCP or web-server dependency is required. The same
validated contracts and SQLite authority back the existing VoiceClient methods.
All filesystem, database and public-source access runs in HA's executor. Public
research has its own lock so a slow source cannot block alarm maintenance.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from functools import partial
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time

from .api import CoordinatorAuthError, CoordinatorConflict, CoordinatorError, VoiceClient
from .contracts import normalize_url

_CLIENTS = 'household_voice_embedded_audio_clients'
_VIEW_REGISTERED = 'household_voice_embedded_audio_view'
_URL = '/api/household_voice/{entry_id}/audio/{recording_id}'


def _private_directory(root):
    """Fail closed on links/special files before opening private mutable state."""
    if root.parent.is_symlink() or root.is_symlink():
        raise CoordinatorError('Household Voice storage must not be a symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
                raise CoordinatorError('Household Voice storage contains an unsafe file')
            os.chmod(path, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    os.chmod(root, 0o700)


class _StateLock:
    """OS-released exclusive ownership prevents duplicate coordinator processes."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b'0')
                    os.fsync(self.fd)
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise CoordinatorError('Another coordinator already owns Household Voice storage') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _signing_key(root):
    path = root / 'audio-signing-key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        value = path.read_text(encoding='ascii')
    else:
        value = secrets.token_hex(32)
        with os.fdopen(fd, 'w', encoding='ascii') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            directory = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    if not re.fullmatch('[a-f0-9]{64}', value):
        # Never silently rotate an unreadable/partial key and invalidate all URLs.
        raise CoordinatorError('Household Voice signing key is damaged; restore its backup')
    return value


class _Authority:
    """Constructed and called only in the executor; no persistent DB handles."""
    def __init__(self, root, cache_root, research_options):
        # Import scheduler here because its boot-ID discovery reads /proc.
        from ._core.api import APIError, ScheduleAPI
        from ._core.announcements import RecordingStore
        from ._core.broadcast import BroadcastService
        from ._core.brave_search import BraveRecipeSearch
        from ._core.capture_api import CaptureAPI
        from ._core.research import Forecasts
        from ._core.runtime import RuntimeAuthority, alarm_wav
        from ._core.scheduler import Scheduler

        _private_directory(root)
        _private_directory(cache_root)
        self.ownership = _StateLock(root / 'authority.lock')
        try:
            self.api_error = APIError
            self.scheduler = Scheduler(root / 'scheduler.sqlite3')
            self.runtime = RuntimeAuthority(self.scheduler)
            self.recordings = RecordingStore(cache_root / 'recordings', _signing_key(root))
            os.chmod(cache_root / 'recordings', 0o700)
            self.captures = CaptureAPI(root / 'captures.sqlite3', self.recordings, {})
            self.broadcasts = BroadcastService(root / 'scheduler.sqlite3', self.recordings)
            self.schedules = ScheduleAPI(self.scheduler)
            self.forecasts = Forecasts()
            self.recipe_search = BraveRecipeSearch(root / 'research.sqlite3', **research_options)
            self.alarm = alarm_wav()
            self.maintain()
        except BaseException:
            self.ownership.close()
            raise

    def maintain(self):
        self.scheduler.tick()
        self.recordings.purge()

    def close(self):
        self.ownership.close()

    def request(self, method, path, payload):
        from ._core.capture_api import room_capture_bindings

        if path == '/v1/schedules' and method == 'GET':
            result = self.schedules.snapshot()
            config = self.runtime.config()
            result.update(rooms=[{'id': key, 'name': value.get('name', key.replace('_', ' ').title())}
                                 for key, value in config['rooms'].items()],
                          runtime_revision=config['revision'], ring_execution_enabled=config['ring_enabled'])
        elif path.startswith('/v1/schedules/') and method == 'POST':
            result = self.schedules.dispatch(path.rsplit('/', 1)[-1], payload)
            result['ring_execution_enabled'] = self.runtime.config()['ring_enabled']
        elif path == '/v1/runtime' and method in ('GET', 'POST'):
            return self.runtime.config() if method == 'GET' else self.runtime.configure(payload)
        elif path == '/v1/captures' and method == 'POST':
            self.captures.bindings = room_capture_bindings(self.runtime.config()['rooms'])
            result = self.captures.upload(payload)
        elif path.startswith('/v1/recordings/') and method == 'GET':
            return self.captures.resolve(path.rsplit('/', 1)[-1])
        elif path.startswith('/v1/ring/') and method == 'POST':
            return self.runtime.dispatch(path.rsplit('/', 1)[-1], payload)
        elif path.startswith('/v1/broadcasts/') and method == 'POST':
            return self.broadcasts.dispatch(path.rsplit('/', 1)[-1], payload)
        else:
            raise self.api_error('Unknown coordinator operation', 404)
        result['ha_connected'] = True
        result['mode'] = 'embedded'
        return result

    def research(self, operation, payload):
        from ._core.research import extract_page, find_places

        schemas = {'places': ({'query'}, {'country_code'}),
                   'forecast': ({'latitude', 'longitude'}, {'days', 'units'}),
                   'page': ({'url'}, set()), 'recipe_search': ({'query'}, set())}
        if operation not in schemas:
            raise self.api_error('Unknown research operation', 404)
        required, optional = schemas[operation]
        if not isinstance(payload, dict) or required - payload.keys() or payload.keys() - required - optional:
            raise self.api_error('Invalid research fields')
        if operation == 'forecast':
            if (any(type(payload[k]) not in (int, float) for k in ('latitude', 'longitude'))
                    or type(payload.get('days', 3)) is not int
                    or not isinstance(payload.get('units', 'fahrenheit'), str)):
                raise self.api_error('Invalid forecast arguments')
            return self.forecasts.get(**payload)
        if any(not isinstance(value, str) for value in payload.values()):
            raise self.api_error('Invalid public-source arguments')
        if operation == 'places':
            return find_places(**payload)
        if operation == 'page':
            return extract_page(**payload)
        return self.recipe_search.search(payload['query'])

    def audio(self, identifier, expires, signature, range_header, head):
        from ._core.capture_api import audio_slice

        raw = (self.alarm if identifier == 'alarm.wav' else
               self.recordings.authorized_audio(identifier, int(expires), signature))
        status, content, headers = audio_slice(raw, None if head else range_header)
        return status, b'' if head else content, headers


class EmbeddedVoiceClient(VoiceClient):
    """The existing client contract backed by one in-process durable authority."""

    @classmethod
    async def async_create(cls, hass, entry_id, base_url, *, research_options=None,
                           register_view=True, maintenance_interval=1):
        if not isinstance(entry_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', entry_id):
            raise CoordinatorError('Invalid Household Voice config entry')
        self = cls.__new__(cls)
        self.hass = hass
        self.entry_id = entry_id
        self.url = normalize_url(base_url) + '/api/household_voice/' + entry_id
        self._closed = False
        self._backup_paused = False
        self._close_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._research_lock = threading.RLock()
        self._pending = set()
        self._maintenance_task = None
        self._maintenance_interval = maintenance_interval
        self.maintenance_last_success = None
        self.maintenance_error = None
        root = Path(hass.config.path('.storage', 'household_voice'))
        cache_root = Path(hass.config.path('.cache', 'household_voice'))
        # Defaults deliberately disable paid requests. Only explicitly supported
        # administrator options can enable Brave; no environment/session scraping.
        options = {'enabled': False, 'api_key': '', 'daily_limit': 20, 'monthly_limit': 300}
        options.update(research_options or {})
        if options.keys() - {'enabled', 'api_key', 'daily_limit', 'monthly_limit'}:
            raise CoordinatorError('Unsupported research configuration')
        try:
            # Shield initialization so cancellation can release the OS ownership
            # after the executor has actually finished, not while it still writes.
            future = asyncio.ensure_future(hass.async_add_executor_job(_Authority, root, cache_root, options))
            try:
                self._authority = await asyncio.shield(future)
            except asyncio.CancelledError:
                authority = await future
                await hass.async_add_executor_job(authority.close)
                raise
            self.maintenance_last_success = time.time()
            if register_view:
                register_audio_view(hass)
            clients = hass.data.setdefault(_CLIENTS, {})
            if entry_id in clients:
                raise CoordinatorError('Household Voice entry is already loaded')
            clients[entry_id] = self
            self._maintenance_task = asyncio.create_task(self._maintenance(), name='household-voice-maintenance')
            return self
        except BaseException as error:
            if hasattr(self, '_authority'):
                await hass.async_add_executor_job(self._authority.close)
            if isinstance(error, (OSError, sqlite3.Error, ValueError)):
                raise CoordinatorError('Household Voice durable storage is unavailable') from None
            raise

    @property
    def maintenance_health(self):
        healthy = (not self._closed and not self._backup_paused and self.maintenance_error is None
                   and self.maintenance_last_success is not None
                   and time.time() - self.maintenance_last_success < 15)
        return {'healthy': healthy, 'last_success': self.maintenance_last_success,
                'error': 'backup_in_progress' if self._backup_paused else self.maintenance_error}

    async def _run(self, operation, *args, research=False, allow_backup=False):
        if self._closed:
            raise CoordinatorError('Household Voice coordinator is stopped')
        if self._backup_paused and not allow_backup:
            raise CoordinatorError('Household Voice backup is in progress')
        lock = self._research_lock if research else self._state_lock

        def call():
            with lock:
                try:
                    return operation(*args)
                except self._authority.api_error as error:
                    if error.status == 409:
                        raise CoordinatorConflict('State changed; refresh and retry the intended action') from None
                    if error.status in (401, 403):
                        raise CoordinatorAuthError('Source is not configured for this operation') from None
                    raise CoordinatorError('Coordinator rejected the operation') from None
                except (OSError, sqlite3.Error, ValueError, TypeError):
                    raise CoordinatorError('Coordinator storage or public source is unavailable') from None

        future = asyncio.ensure_future(self.hass.async_add_executor_job(call))
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        # A cancelled caller must not cancel an executor future whose thread is
        # still writing; shutdown must be able to drain it before releasing lock.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Retrieve any eventual failure when no caller remains to observe it.
            future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise

    async def _request(self, method, path, payload=None):
        if path.startswith('/v1/research/') and method == 'POST':
            return await self._run(self._authority.research, path.rsplit('/', 1)[-1], payload, research=True)
        result = await self._run(self._authority.request, method, path, payload)
        if path == '/v1/schedules' and method == 'GET':
            result['maintenance'] = self.maintenance_health
        return result

    async def _maintenance_once(self):
        if self._backup_paused:
            return
        try:
            await self._run(self._authority.maintain)
        except Exception:
            self.maintenance_error = 'durable_storage_unavailable'
        else:
            self.maintenance_last_success = time.time()
            self.maintenance_error = None

    async def _maintenance(self):
        while not self._closed:
            await self._maintenance_once()
            await asyncio.sleep(self._maintenance_interval)

    async def async_audio(self, identifier, expires='0', signature='', range_header=None, head=False):
        return await self._run(self._authority.audio, identifier, expires, signature, range_header, head)

    async def async_pause_for_backup(self):
        """Quiesce every local transaction before HA copies durable state."""
        async with self._close_lock:
            if self._closed:
                return
            self._backup_paused = True
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def async_resume_after_backup(self):
        """Reconcile elapsed deadlines before releasing the temporary write gate."""
        async with self._close_lock:
            if self._closed or not self._backup_paused:
                return
            try:
                await self._run(self._authority.maintain, allow_backup=True)
            except Exception:
                self.maintenance_error = 'durable_storage_unavailable'
            else:
                self.maintenance_last_success = time.time()
                self.maintenance_error = None
            finally:
                # A post-backup disk error must remain visible and retryable,
                # rather than accidentally leaving a permanent backup pause.
                self._backup_paused = False

    async def async_close(self):
        """Stop maintenance, drain all executor work, preserve all durable state."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            clients = self.hass.data.get(_CLIENTS, {})
            if clients.get(self.entry_id) is self:
                clients.pop(self.entry_id)
            if self._maintenance_task is not None:
                self._maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._maintenance_task
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
            await self.hass.async_add_executor_job(self._authority.close)


def register_audio_view(hass):
    """One HA route persists across integration reloads, clients do not."""
    if hass.data.get(_VIEW_REGISTERED):
        return
    from aiohttp import web
    from homeassistant.components.http import HomeAssistantView

    class HouseholdVoiceAudioView(HomeAssistantView):
        url = _URL
        name = 'api:household_voice:audio'
        requires_auth = False  # Signed expiring recording capabilities, never DB/control.

        async def _serve(self, request, entry_id, recording_id, *, head=False):
            client = hass.data.get(_CLIENTS, {}).get(entry_id)
            if client is None:
                return web.Response(status=404, headers={'Cache-Control': 'no-store'})
            try:
                status, content, headers = await client.async_audio(
                    recording_id, request.query.get('expires', '0'), request.query.get('sig', ''),
                    request.headers.get('Range'), head)
            except CoordinatorError:
                return web.Response(status=404, headers={'Cache-Control': 'no-store'})
            return web.Response(status=status, body=content, headers=headers, content_type='audio/wav')

        async def get(self, request, entry_id, recording_id):
            return await self._serve(request, entry_id, recording_id)

        async def head(self, request, entry_id, recording_id):
            return await self._serve(request, entry_id, recording_id, head=True)

    hass.http.register_view(HouseholdVoiceAudioView())
    hass.data[_VIEW_REGISTERED] = True
