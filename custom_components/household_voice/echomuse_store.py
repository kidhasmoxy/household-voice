"""Strict private credential store; a failed commit never confirms provisioning.

HA Store deliberately logs some write errors and resets corrupt JSON. Dedicated
source authentication needs an explicit success/failure boundary instead.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3

from .echomuse_endpoint import _object


class PrivateSourceStore:
    def __init__(self,path):
        self.path=Path(path)
        if not self.path.is_absolute():
            raise ValueError('Private absolute path required')

    def _open(self,*,create=False):
        if self.path.is_symlink():
            raise ValueError('Private store must not be a symlink')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if not self.path.exists():
            if not create:
                return None
            descriptor=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            os.close(descriptor)
            new=True
        else:
            new=False
        db=sqlite3.connect(self.path,timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            if new:
                with db:
                    db.execute('CREATE TABLE credentials (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)')
                    db.execute('PRAGMA user_version=1')
            elif db.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise ValueError('Unsupported private store schema')
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('Corrupt private store')
            return db
        except BaseException:
            db.close()
            raise

    def _load(self):
        db=self._open()
        if db is None:
            return None
        try:
            row=db.execute('SELECT value FROM credentials WHERE id=1').fetchone()
            if row is None:
                raise ValueError('Incomplete private store; preserve and recover it')
            return json.loads(row[0],object_pairs_hook=_object)
        finally:
            db.close()

    def _save(self,data):
        raw=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
        if len(raw)>65536:
            raise ValueError('Private registration store is full')
        db=self._open(create=True)
        try:
            with db:
                db.execute('INSERT INTO credentials VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',(raw,))
        finally:
            db.close()

    async def async_load(self):
        return await asyncio.to_thread(self._load)

    async def async_save(self,data):
        return await asyncio.to_thread(self._save,data)
