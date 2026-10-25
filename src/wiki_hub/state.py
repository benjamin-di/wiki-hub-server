"""Small durable catalog and job journal. Content remains in Git."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "wiki-hub.db"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS wikis (
                    slug TEXT PRIMARY KEY, title TEXT NOT NULL, owner TEXT NOT NULL,
                    repo_owner TEXT NOT NULL, repo_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'initializing', created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, wiki TEXT NOT NULL REFERENCES wikis(slug),
                    subject TEXT NOT NULL, filename TEXT NOT NULL, source_path TEXT NOT NULL,
                    instructions TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    revision TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS grants (
                    wiki TEXT NOT NULL REFERENCES wikis(slug), subject TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('read','contribute','write','admin','none')),
                    pending INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL,
                    PRIMARY KEY(wiki,subject));
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, revoked INTEGER NOT NULL DEFAULT 0,
                    created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS copy_settings (
                    kind TEXT NOT NULL CHECK(kind IN ('user','wiki')), key TEXT NOT NULL,
                    enabled INTEGER NOT NULL, pending INTEGER NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS user_status (
                    subject TEXT PRIMARY KEY, suspended INTEGER NOT NULL,
                    pending INTEGER NOT NULL, updated TEXT NOT NULL);
            """)
            schema = db.execute("SELECT sql FROM sqlite_master WHERE name='grants'").fetchone()[0]
            if "'admin'" not in schema:
                # A transaction preserves every grant while widening the role check.
                db.execute("BEGIN IMMEDIATE")
                db.execute("""CREATE TABLE grants_next (
                    wiki TEXT NOT NULL REFERENCES wikis(slug), subject TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('read','contribute','write','admin','none')),
                    pending INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL,
                    PRIMARY KEY(wiki,subject))""")
                db.execute("INSERT INTO grants_next SELECT wiki,subject,role,pending,updated FROM grants")
                db.execute("DROP TABLE grants")
                db.execute("ALTER TABLE grants_next RENAME TO grants")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def all_wikis(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM wikis ORDER BY slug")]

    def copy_setting(self, kind, key):
        with self.connect() as db:
            row = db.execute("SELECT enabled,pending FROM copy_settings WHERE kind=? AND key=?",
                             (kind, key)).fetchone()
        return {"local_copies": bool(row["enabled"]) if row else True,
                "pending": bool(row["pending"]) if row else False}

    def set_copy_setting(self, kind, key, enabled):
        with self.connect() as db:
            db.execute("""INSERT INTO copy_settings(kind,key,enabled,pending,updated) VALUES(?,?,?,1,?)
                ON CONFLICT(kind,key) DO UPDATE SET enabled=excluded.enabled,pending=1,updated=excluded.updated""",
                       (kind, key, int(enabled), now()))

    def copy_projected(self, kind, key):
        with self.connect() as db:
            db.execute("UPDATE copy_settings SET pending=0 WHERE kind=? AND key=?", (kind, key))

    def copies_allowed(self, subject, wiki, *, ready=True):
        flags = [self.copy_setting("user", subject), self.copy_setting("wiki", wiki)]
        status = self.user_status(subject)
        return (not status["suspended"] and (not ready or not status["pending"])
                and all(flag["local_copies"] and (not ready or not flag["pending"]) for flag in flags))

    def user_status(self, subject):
        with self.connect() as db:
            row = db.execute("SELECT suspended,pending FROM user_status WHERE subject=?", (subject,)).fetchone()
        return {"suspended": bool(row["suspended"]) if row else False,
                "pending": bool(row["pending"]) if row else False}

    def set_user_status(self, subject, suspended):
        with self.connect() as db:
            db.execute("""INSERT INTO user_status VALUES(?,?,1,?) ON CONFLICT(subject) DO UPDATE SET
                suspended=excluded.suspended,pending=1,updated=excluded.updated""", (subject, int(suspended), now()))

    def user_projected(self, subject):
        with self.connect() as db:
            db.execute("UPDATE user_status SET pending=0 WHERE subject=?", (subject,))

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='interrupted', error=?, updated=? WHERE status IN ('queued','running')",
                ("Server restarted. Inspect this job before resubmitting.", now()),
            )

    def wiki(self, slug):
        with self.connect() as db:
            row = db.execute("SELECT * FROM wikis WHERE slug=?", (slug,)).fetchone()
        return dict(row) if row else None

    def list_wikis(self, subject):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT wikis.* FROM wikis WHERE owner=? OR slug IN "
                "(SELECT wiki FROM grants WHERE subject=? AND role!='none' AND pending=0) ORDER BY slug",
                (subject, subject),
            )]

    def role(self, subject, slug):
        with self.connect() as db:
            row = db.execute(
                "SELECT role FROM grants WHERE wiki=? AND subject=? AND role!='none' AND pending=0",
                (slug, subject),
            ).fetchone()
        return row["role"] if row else None

    def grants(self, slug):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT subject,role,pending,updated FROM grants WHERE wiki=? "
                "AND (role!='none' OR pending=1) ORDER BY subject", (slug,),
            )]

    def set_grant(self, slug, subject, role):
        with self.connect() as db:
            db.execute(
                "INSERT INTO grants(wiki,subject,role,pending,updated) VALUES(?,?,?,1,?) "
                "ON CONFLICT(wiki,subject) DO UPDATE SET role=excluded.role,pending=1,updated=excluded.updated",
                (slug, subject, role, now()),
            )

    def projected(self, slug, subject):
        with self.connect() as db:
            db.execute("UPDATE grants SET pending=0 WHERE wiki=? AND subject=?", (slug, subject))

    def add_agent(self, identifier, owner, name, token):
        with self.connect() as db:
            db.execute("INSERT INTO agents(id,owner,name,token_hash,created) VALUES(?,?,?,?,?)",
                       (identifier, owner, name, hashlib.sha256(token.encode()).hexdigest(), now()))

    def agent(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT id,owner,name,revoked,created FROM agents WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def token_agent(self, token):
        with self.connect() as db:
            row = db.execute("SELECT id FROM agents WHERE token_hash=? AND revoked=0",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return row["id"] if row else None

    def agents(self, owner):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,owner,name,revoked,created FROM agents WHERE owner=? ORDER BY created,id", (owner,),
            )]

    def revoke_agent(self, owner, identifier):
        with self.connect() as db:
            return db.execute("UPDATE agents SET revoked=1 WHERE owner=? AND id=?", (owner, identifier)).rowcount > 0

    def add_wiki(self, slug, title, subject, repo_owner, repo_id):
        with self.connect() as db:
            db.execute(
                "INSERT INTO wikis(slug,title,owner,repo_owner,repo_id,created) VALUES(?,?,?,?,?,?)",
                (slug, title, subject, repo_owner, repo_id, now()),
            )

    def ready(self, slug):
        with self.connect() as db:
            db.execute("UPDATE wikis SET status='ready' WHERE slug=?", (slug,))

    def archive_transition(self, slug, status):
        """Close publication and interrupt outstanding jobs in the same transaction."""
        with self.connect() as db:
            db.execute("UPDATE wikis SET status=? WHERE slug=?", (status, slug))
            db.execute(
                "UPDATE jobs SET status='interrupted',error=?,updated=? "
                "WHERE wiki=? AND status IN ('queued','running')",
                ("Wiki archived. Inspect this job before resubmitting after reopening.", now(), slug),
            )

    def archive_projected(self, slug, archived):
        with self.connect() as db:
            db.execute("UPDATE wikis SET status=? WHERE slug=?", ("archived" if archived else "ready", slug))

    def add_job(self, job_id, wiki, subject, filename, source_path, instructions):
        stamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,wiki,subject,filename,source_path,instructions,status,created,updated) "
                "VALUES(?,?,?,?,?,?,'queued',?,?)",
                (job_id, wiki, subject, filename, source_path, instructions, stamp, stamp),
            )

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id, status, revision=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,revision=COALESCE(?,revision),error=?,updated=? WHERE id=?",
                (status, revision, error, now(), job_id),
            )

    def public_job(self, job_id):
        row = self.job(job_id)
        if row:
            return {k: v for k, v in row.items() if k not in ("source_path", "subject", "instructions")}
        return None


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
