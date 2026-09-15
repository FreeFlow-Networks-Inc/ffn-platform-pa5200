#!/usr/bin/env python3
"""Durable single-owner FE100 session intent journal (no hardware access)."""
import fcntl
import json
import sqlite3


class Journal:
    def __init__(self, path):
        # All managers of this journal serialize for their entire lifetime.
        # Hardware adapters must additionally hold the shared FE100 table lock.
        self.lock = open(str(path)+'.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(path)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS sessions '
                            '(id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
            self.db.commit()
        except BaseException:
            self.lock.close()
            raise

    def load(self):
        sessions = {}
        for ident, body in self.db.execute('SELECT id, body FROM sessions'):
            value = json.loads(body)
            value['entries'] = tuple(bytes.fromhex(e) for e in value['entries'])
            if (len(value['entries']) != 2 or any(len(e) != 64 for e in value['entries'])
                    or value['state'] not in ('installing', 'installed', 'removing', 'unknown')):
                raise RuntimeError('invalid FE100 journal; manual recovery required')
            sessions[ident] = value
        return sessions

    def put(self, ident, value):
        body = {**value, 'entries': [e.hex() for e in value['entries']]}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO sessions VALUES (?, ?)',
                            (ident, json.dumps(body, sort_keys=True)))

    def delete(self, ident):
        with self.db:
            self.db.execute('DELETE FROM sessions WHERE id=?', (ident,))

    def close(self):
        self.db.close()
        self.lock.close()
