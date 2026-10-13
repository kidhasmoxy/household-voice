"""Durable SQLite restoration snapshots. No HA registration or device I/O.

The caller chooses one absolute file inside an existing private writable directory.
This module never creates directories or follows symlink/reparse-point paths.
SQLite rollback-journal transactions use synchronous=FULL. A successful save means
commit completed; filesystem/hardware durability still depends on the host. A
cancelled async caller waits for its worker transaction before releasing the lock.

Only the executor's bounded version-1 volume/ownership schema is accepted. There
are no URL, audio, transcript, or credential fields; generation IDs are restricted
opaque ASCII tokens. Corrupt/foreign/future-version files are rejected, preserved,
and never silently reset. SQLite's own hot-journal recovery is still permitted.

The containing directory must not be writable by untrusted processes: Python's
sqlite3 API does not expose race-free directory-relative/O_NOFOLLOW database open.
Path and sidecar checks reject existing links, but cannot defeat a privileged
process replacing paths between checks and SQLite open. One executor owns logical
read/modify/write state; multiple instances serialize atomic snapshots via SQLite,
but do not provide application-level compare-and-swap or merge semantics.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat


MAX_JSON_BYTES = 262144
MAX_DATABASE_BYTES = 4 * 1024 * 1024
SCHEMA = 'CREATE TABLE restoration (id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL)'
FIELDS = {'baseline', 'ducked', 'generation', 'operation', 'status'}
EMPTY = {'version': 1, 'players': {}}


class JournalError(ValueError):
    """Sanitized journal error; deliberately omits paths and snapshot contents."""


def _validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'players'}:
        raise JournalError('Invalid restoration snapshot')
    if type(value['version']) is not int or value['version'] != 1:
        raise JournalError('Unsupported restoration snapshot version')
    players = value['players']
    if not isinstance(players, dict) or len(players) > 256:
        raise JournalError('Invalid restoration players')
    for player, record in players.items():
        if not isinstance(player, str) or len(player) > 200 or not re.fullmatch(r'media_player\.[a-z0-9_]+', player):
            raise JournalError('Invalid restoration player')
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise JournalError('Invalid restoration record')
        for field in ('baseline', 'ducked'):
            number = record[field]
            if type(number) not in (float, int) or not math.isfinite(number) or not 0 <= number <= 1:
                raise JournalError('Invalid restoration volume')
        if not isinstance(record['generation'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', record['generation']):
            raise JournalError('Invalid opaque generation')
        if not isinstance(record['operation'], str) or not re.fullmatch(r'[a-f0-9]{32}', record['operation']):
            raise JournalError('Invalid operation identity')
        if not isinstance(record['status'], str) or record['status'] not in {'pending', 'confirmed', 'restoring'}:
            raise JournalError('Invalid restoration status')
    return value


def _encode(value):
    _validate(value)
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_JSON_BYTES:
        raise JournalError('Restoration snapshot exceeds limit')
    return encoded


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError('Duplicate restoration field')
        result[key] = value
    return result


def _decode(encoded):
    if not isinstance(encoded, str) or len(encoded.encode('utf-8')) > MAX_JSON_BYTES:
        raise JournalError('Restoration snapshot exceeds limit')
    def reject_constant(value):
        raise JournalError('Invalid JSON number')
    try:
        return _validate(json.loads(encoded, object_pairs_hook=_pairs, parse_constant=reject_constant))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise JournalError('Invalid stored restoration snapshot') from None


class SQLiteJournal:
    def __init__(self, path):
        if not isinstance(path, (str, os.PathLike)):
            raise JournalError('An explicit absolute journal path is required')
        self.path = Path(path)
        if not self.path.is_absolute() or '..' in self.path.parts or not self.path.name:
            raise JournalError('An explicit absolute journal path is required')
        self._lock = asyncio.Lock()

    def _check_path(self):
        try:
            for path in (*reversed(self.path.parents), self.path):
                if not path.exists() and not path.is_symlink():
                    if path == self.path:
                        continue
                    raise JournalError('Journal parent directory must exist')
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise JournalError('Journal paths must not contain links')
                if path == self.path:
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_DATABASE_BYTES:
                        raise JournalError('Invalid journal target file')
                elif not stat.S_ISDIR(info.st_mode):
                    raise JournalError('Journal parent directory must exist')
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    info = sidecar.lstat()
                    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                            or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        raise JournalError('Invalid journal sidecar')
        except OSError:
            raise JournalError('Journal path is unavailable') from None

    def _read(self, db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name LIMIT 3").fetchall()
        if version != 1 or schema != [('table', 'restoration', SCHEMA)]:
            raise JournalError('Unsupported or corrupt journal schema')
        rows = db.execute('SELECT id,length(CAST(snapshot AS BLOB)) FROM restoration LIMIT 2').fetchall()
        if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int or not 1 <= rows[0][1] <= MAX_JSON_BYTES:
            raise JournalError('Invalid journal snapshot row')
        return _decode(db.execute('SELECT snapshot FROM restoration WHERE id=1').fetchone()[0])

    def _commit(self, db):
        """Separate method permits deterministic disk/commit fault injection."""
        db.commit()

    def _transaction(self, encoded=None):
        self._check_path()
        db = None
        try:
            # SQLite creates a missing file. An existing empty file is treated
            # as corruption, never permission to overwrite unknown contents.
            new = not self.path.exists()
            db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA trusted_schema=OFF')
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                raise JournalError('Unexpected journal mode; explicit migration required')
            db.execute('BEGIN IMMEDIATE')
            if new:
                # Another instance may have initialized between exists/open.
                if db.execute('PRAGMA user_version').fetchone()[0] == 0 and not db.execute(
                        "SELECT name FROM sqlite_master LIMIT 1").fetchone():
                    db.execute(SCHEMA)
                    db.execute('PRAGMA user_version=1')
                    db.execute('INSERT INTO restoration VALUES(1,?)', (_encode(EMPTY),))
            previous = self._read(db)
            if encoded is not None:
                db.execute('UPDATE restoration SET snapshot=? WHERE id=1', (encoded,))
            self._commit(db)
            return previous
        except JournalError:
            if db is not None:
                db.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError):
            if db is not None:
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
            raise JournalError('Restoration journal transaction failed') from None
        finally:
            if db is not None:
                db.close()

    async def _run(self, encoded=None):
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(self._transaction, encoded))
            cancelled = False
            while True:
                try:
                    result = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    cancelled = True
                except Exception:
                    if cancelled:
                        raise asyncio.CancelledError() from None
                    raise
            if cancelled:
                raise asyncio.CancelledError()
            return result

    async def load(self):
        return await self._run()

    async def save(self, snapshot):
        # Freeze and validate before yielding; callers cannot mutate the object
        # behind the worker while it waits for the serialized transaction.
        encoded = _encode(snapshot)
        await self._run(encoded)
