"""Durable admission for a trusted conversation request, before any side effect.

No utterance is stored. Admitted but unfinished requests never automatically
execute again. Identities are retained, rather than expiring and authorizing an
old request again. Capacity exhaustion fails closed for operator investigation.
"""
import json
from pathlib import Path
import sqlite3


class ScheduleJournal:
    LIMIT = 100_000

    def __init__(self, path):
        self.path = str(path) if path is not None else None
        # Only pure unit fixtures use this branch; HA always supplies a path.
        self.memory = {}
        self.dialogues = set()

    def _open(self):
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if exists:
                if version != 1 or db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Invalid schedule request journal')
                columns = [row[1] for row in db.execute('PRAGMA table_info(requests)')]
                if columns != ['id', 'fingerprint', 'state', 'response']:
                    raise ValueError('Invalid schedule request journal schema')
                if [row[1] for row in db.execute('PRAGMA table_info(dialogues)')] != ['scope','conversation']:
                    raise ValueError('Invalid schedule dialogue journal schema')
            else:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE requests(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, response TEXT)')
                db.execute('CREATE TABLE dialogues(scope TEXT NOT NULL, conversation TEXT NOT NULL, PRIMARY KEY(scope,conversation))')
                db.execute('PRAGMA user_version=1')
                db.commit()
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _existing(row, fingerprint):
        if row[0] != fingerprint:
            return 'conflict', None
        if row[1] == 'admitted' and row[2] is None:
            return 'incomplete', None
        if row[1] != 'completed' or not isinstance(row[2], str):
            raise ValueError('Invalid schedule request journal row')
        value = json.loads(row[2])
        if value is not None and (not isinstance(value, dict)
                or set(value) != {'speech', 'conversation_id', 'continue_conversation'}
                or not isinstance(value['speech'], str)
                or value['conversation_id'] is not None and not isinstance(value['conversation_id'], str)
                or type(value['continue_conversation']) is not bool):
            raise ValueError('Invalid schedule request journal response')
        return 'completed', value

    def admit(self, identifier, fingerprint):
        if self.path is None:
            if identifier in self.memory:
                return self._existing(self.memory[identifier], fingerprint)
            if len(self.memory) >= self.LIMIT:
                raise ValueError('Schedule request journal capacity reached')
            self.memory[identifier] = (fingerprint, 'admitted', None)
            return 'admitted', None
        db = self._open()
        try:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT fingerprint,state,response FROM requests WHERE id=?', (identifier,)).fetchone()
            if old is not None:
                result = self._existing(old, fingerprint)
                db.rollback()
                return result
            if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] >= self.LIMIT:
                raise ValueError('Schedule request journal capacity reached')
            db.execute('INSERT INTO requests VALUES(?,?,?,NULL)', (identifier, fingerprint, 'admitted'))
            db.commit()
            return 'admitted', None
        finally:
            db.close()

    def lookup(self, identifier, fingerprint):
        if self.path is None:
            row=self.memory.get(identifier)
            return self._existing(row,fingerprint) if row is not None else None
        if not Path(self.path).exists():
            return None
        db=self._open()
        try:
            row=db.execute('SELECT fingerprint,state,response FROM requests WHERE id=?',(identifier,)).fetchone()
            return self._existing(row,fingerprint) if row is not None else None
        finally:
            db.close()

    def known_dialogue(self, scope, conversation):
        if self.path is None:
            return (scope,conversation) in self.dialogues
        if not Path(self.path).exists():return False
        db=self._open()
        try:
            return db.execute('SELECT 1 FROM dialogues WHERE scope=? AND conversation=?',(scope,conversation)).fetchone() is not None
        finally:
            db.close()

    def complete(self, identifier, fingerprint, response, dialogue_scope=None):
        encoded = json.dumps(response, allow_nan=False, separators=(',', ':'))
        dialogue=(dialogue_scope,response['conversation_id']) if (dialogue_scope and response is not None
            and response['continue_conversation'] and response['conversation_id']) else None
        if self.path is None:
            if self.memory.get(identifier) != (fingerprint, 'admitted', None):
                raise ValueError('Schedule request reservation changed')
            self.memory[identifier] = (fingerprint, 'completed', encoded)
            if dialogue:self.dialogues.add(dialogue)
            return
        db = self._open()
        try:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE requests SET state='completed',response=? WHERE id=? AND fingerprint=? AND state='admitted'",
                                 (encoded, identifier, fingerprint)).rowcount
            if changed != 1:
                raise ValueError('Schedule request reservation changed')
            if dialogue:db.execute('INSERT OR IGNORE INTO dialogues VALUES(?,?)',dialogue)
            db.commit()
        finally:
            db.close()
