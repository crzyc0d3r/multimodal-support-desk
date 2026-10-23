"""SQLite persistence; every object lookup is scoped to the authenticated company."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


def identifier():
    return uuid.uuid4().hex


class Database:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "support.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                    created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, document_id TEXT NOT NULL,
                    page INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                    image_path TEXT, embedding TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_tenant ON chunks(company, kind);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, user_id TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT, status TEXT NOT NULL,
                    citations TEXT NOT NULL DEFAULT '[]', created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    question TEXT NOT NULL, rejected TEXT NOT NULL, status TEXT NOT NULL,
                    reply TEXT, saved_document TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL,
                    created TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS thresholds (
                    company TEXT PRIMARY KEY, text_relevance REAL NOT NULL DEFAULT .42,
                    text_answerability REAL NOT NULL DEFAULT .55,
                    image_relevance REAL NOT NULL DEFAULT .46,
                    image_answerability REAL NOT NULL DEFAULT .60);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                    status TEXT NOT NULL, completed INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                    error TEXT, document_id TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def event(self, company, kind, payload):
        self.execute("INSERT INTO events(company,kind,payload) VALUES(?,?,?)",
                     (company, kind, json.dumps(payload)))

    def thresholds(self, company):
        self.execute("INSERT OR IGNORE INTO thresholds(company) VALUES(?)", (company,))
        return self.one("SELECT * FROM thresholds WHERE company=?", (company,))
