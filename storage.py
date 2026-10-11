import json
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'monitor.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS updates (id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                reply TEXT, chat_id TEXT, handled INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL, revision INTEGER NOT NULL, kind TEXT NOT NULL,
                sent INTEGER DEFAULT 0);
        ''')
        self.db.commit()
        if self.get('runtime') is None:
            self.put('runtime', {'enabled': False, 'revision': 0, 'seen': [], 'last': None,
                                 'error': None, 'error_notified': False, 'completed': False,
                                 'last_notification': None})
        if self.get('webhook_secret') is None:
            self.put('webhook_secret', secrets.token_urlsafe(32))

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key, value):
        self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key, json.dumps(value, ensure_ascii=False)))

    def put(self, key, value):
        with self.transaction():
            self.set(key, value)

    @contextmanager
    def transaction(self):
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def accept_update(self, update):
        if type(update.get('update_id')) is not int or update['update_id'] < 0:
            raise ValueError('Invalid update')
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO updates(id,payload) VALUES (?,?)',
                            (update['update_id'], json.dumps(update, ensure_ascii=False)))

    def next_update(self):
        return self.db.execute('SELECT * FROM updates WHERE done=0 ORDER BY id LIMIT 1').fetchone()

    def handled(self, update_id, chat_id, reply):
        self.db.execute('UPDATE updates SET handled=1, chat_id=?, reply=?, done=? WHERE id=?',
                        (str(chat_id) if chat_id else None, reply, int(reply is None), update_id))

    def update_done(self, update_id):
        with self.transaction():
            self.db.execute('UPDATE updates SET done=1 WHERE id=?', (update_id,))
            self.db.execute('DELETE FROM updates WHERE done=1 AND id < ?', (update_id - 10000,))

    def queue(self, messages, revision, kind='slots'):
        self.db.executemany('INSERT INTO outbox(text,revision,kind) VALUES (?,?,?)',
                            [(message, revision, kind) for message in messages])

    def next_notice(self, revision):
        with self.transaction():
            self.db.execute('DELETE FROM outbox WHERE revision != ?', (revision,))
        return self.db.execute('SELECT * FROM outbox WHERE sent=0 ORDER BY id LIMIT 1').fetchone()

    def sent_notice(self, notice_id):
        self.db.execute('DELETE FROM outbox WHERE id=?', (notice_id,))

    def close(self):
        self.db.close()