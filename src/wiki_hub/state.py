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
                CREATE TABLE IF NOT EXISTS agent_workloads (
                    agent TEXT PRIMARY KEY REFERENCES agents(id), issuer TEXT NOT NULL,
                    client_id TEXT NOT NULL, subject TEXT NOT NULL,
                    UNIQUE(issuer,client_id), UNIQUE(issuer,subject));
                CREATE TABLE IF NOT EXISTS copy_settings (
                    kind TEXT NOT NULL CHECK(kind IN ('user','wiki')), key TEXT NOT NULL,
                    enabled INTEGER NOT NULL, pending INTEGER NOT NULL, updated TEXT NOT NULL,
                    PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS user_status (
                    subject TEXT PRIMARY KEY, suspended INTEGER NOT NULL,
                    pending INTEGER NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drive_people (
                    subject TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drive_devices (
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL REFERENCES drive_people(subject),
                    name TEXT NOT NULL, number INTEGER NOT NULL UNIQUE, created TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT);
                CREATE TABLE IF NOT EXISTS drive_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, declaration in {
                "submission_id": "TEXT", "fingerprint": "TEXT", "candidate_revision": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'ingest'",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS job_submissions ON jobs(wiki,subject,submission_id)")
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

    def drive_generation(self, bump=False):
        with self.connect() as db:
            if bump:
                db.execute("INSERT INTO drive_meta VALUES('generation',1) "
                           "ON CONFLICT(key) DO UPDATE SET value=value+1")
            row = db.execute("SELECT value FROM drive_meta WHERE key='generation'").fetchone()
        return row["value"] if row else 0

    def inbox_person(self, subject):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT number FROM drive_people WHERE subject=?", (subject,)).fetchone()
            if row:
                return row["number"]
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM drive_people").fetchone()[0]
            db.execute("INSERT INTO drive_people VALUES(?,?,?)", (subject, number, now()))
            return number

    def add_drive_device(self, identifier, subject, name):
        # Numbers are never reused, so a revoked device's file-server identity
        # cannot be inherited by a later device or person.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM drive_people WHERE subject=?", (subject,)).fetchone() is None:
                number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM drive_people").fetchone()[0]
                db.execute("INSERT INTO drive_people VALUES(?,?,?)", (subject, number, now()))
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM drive_devices").fetchone()[0]
            db.execute("INSERT INTO drive_devices(id,subject,name,number,created) VALUES(?,?,?,?,?)",
                       (identifier, subject, name, number, now()))
            db.execute("INSERT INTO drive_meta VALUES('generation',1) ON CONFLICT(key) DO UPDATE SET value=value+1")
        return self.drive_device(identifier)

    def drive_device(self, identifier):
        with self.connect() as db:
            row = db.execute("""SELECT d.*, p.number AS person FROM drive_devices d
                JOIN drive_people p ON p.subject=d.subject WHERE d.id=?""", (identifier,)).fetchone()
        return dict(row) if row else None

    def drive_devices(self, subject=None, include_revoked=True):
        query = "SELECT d.*, p.number AS person FROM drive_devices d JOIN drive_people p ON p.subject=d.subject"
        clauses, values = [], []
        if subject is not None:
            clauses.append("d.subject=?")
            values.append(subject)
        if not include_revoked:
            clauses.append("d.revoked=0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY d.number", values)]

    def drive_people(self):
        """People with at least one unrevoked device; only they need file-server access."""
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT subject, number FROM drive_people WHERE subject IN
                (SELECT subject FROM drive_devices WHERE revoked=0) ORDER BY number""")]

    def revoke_drive_device(self, identifier):
        with self.connect() as db:
            changed = db.execute("UPDATE drive_devices SET revoked=1 WHERE id=? AND revoked=0",
                                 (identifier,)).rowcount
            if changed:
                db.execute("UPDATE drive_meta SET value=value+1 WHERE key='generation'")
            return changed > 0

    def revoke_unconfirmed_drive_devices(self):
        """Devices whose enrolment never completed (for example after a restart)."""
        with self.connect() as db:
            rows = [row["number"] for row in db.execute(
                "SELECT number FROM drive_devices WHERE revoked=0 AND confirmed=0")]
            if rows:
                db.execute("UPDATE drive_devices SET revoked=1 WHERE revoked=0 AND confirmed=0")
                db.execute("UPDATE drive_meta SET value=value+1 WHERE key='generation'")
        return rows

    def drive_observed(self, number, *, confirmed=None, seen=False):
        with self.connect() as db:
            if confirmed is not None:
                db.execute("UPDATE drive_devices SET confirmed=? WHERE number=?", (int(confirmed), number))
            if seen:
                db.execute("UPDATE drive_devices SET last_seen=? WHERE number=?", (now(), number))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='uncertain',error=?,updated=? WHERE status='publishing'",
                       ("Publication was interrupted. Checking accepted Git history is required.", now()))
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
        if row is None:
            return None
        result = dict(row)
        binding = self.workload_binding(identifier)
        if binding:
            result["workload"] = binding
        return result

    def add_workload_agent(self, identifier, owner, name, issuer, client_id, subject):
        with self.connect() as db:
            # No local bearer exists for a workload. This cannot match a SHA-256
            # credential hash, including after a rollback to a key-only release.
            db.execute("INSERT INTO agents(id,owner,name,token_hash,created) VALUES(?,?,?,?,?)",
                       (identifier, owner, name, "workload:" + identifier, now()))
            db.execute("INSERT INTO agent_workloads(agent,issuer,client_id,subject) VALUES(?,?,?,?)",
                       (identifier, issuer, client_id, subject))

    def workload_binding(self, identifier):
        with self.connect() as db:
            row = db.execute("SELECT issuer,client_id,subject FROM agent_workloads WHERE agent=?",
                             (identifier,)).fetchone()
        return dict(row) if row else None

    def workload_agent(self, issuer, client_id, subject):
        with self.connect() as db:
            row = db.execute("""SELECT a.id FROM agent_workloads w JOIN agents a ON a.id=w.agent
                WHERE w.issuer=? AND w.client_id=? AND w.subject=? AND a.revoked=0""",
                             (issuer, client_id, subject)).fetchone()
        return row["id"] if row else None

    def is_workload_client(self, issuer, client_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM agent_workloads WHERE issuer=? AND client_id=?",
                              (issuer, client_id)).fetchone() is not None

    def token_agent(self, token):
        with self.connect() as db:
            row = db.execute("SELECT id FROM agents WHERE token_hash=? AND revoked=0",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return row["id"] if row else None

    def agents(self, owner):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,owner,name,revoked,created FROM agents WHERE owner=? ORDER BY created,id", (owner,),
            )]
        for row in rows:
            binding = self.workload_binding(row["id"])
            if binding:
                row["workload"] = binding
        return rows

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

    def add_job(self, job_id, wiki, subject, filename, source_path, instructions,
                submission_id=None, fingerprint=None, kind="ingest"):
        stamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,wiki,subject,filename,source_path,instructions,status,created,updated,"
                "submission_id,fingerprint,kind) VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
                (job_id, wiki, subject, filename, source_path, instructions, stamp, stamp,
                 submission_id, fingerprint, kind),
            )

    def submission(self, subject, wiki, identifier):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE subject=? AND wiki=? AND submission_id=?",
                             (subject, wiki, identifier)).fetchone()
        return dict(row) if row else None

    def pending_jobs(self):
        with self.connect() as db:
            query = "SELECT count(*) FROM jobs WHERE status IN ('queued','running','publishing')"
            return db.execute(query).fetchone()[0]

    def jobs(self, wiki, status=None, limit=50, before=None):
        query, values = "SELECT * FROM jobs WHERE wiki=?", [wiki]
        with self.connect() as db:
            if status:
                query += " AND status=?"
                values.append(status)
            if before:
                cursor = db.execute("SELECT created,id FROM jobs WHERE wiki=? AND id=?", (wiki, before)).fetchone()
                if cursor is None:
                    raise ValueError("Invalid job cursor")
                query += " AND (created,id) < (?,?)"
                values.extend(cursor)
            return [dict(row) for row in db.execute(query + " ORDER BY created DESC,id DESC LIMIT ?",
                                                    [*values, limit])]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def drive_jobs(self, wiki, subject):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE wiki=? AND subject=? AND kind='drive-edit' "
                "ORDER BY created DESC,id DESC LIMIT 10", (wiki, subject))]

    def update_job(self, job_id, status, revision=None, error=None, candidate_revision=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?,revision=COALESCE(?,revision),candidate_revision="
                "COALESCE(?,candidate_revision),error=?,updated=? WHERE id=?",
                (status, revision, candidate_revision, error, now(), job_id),
            )

    def public_job(self, job_id):
        row = self.job(job_id)
        if row:
            return {k: v for k, v in row.items() if k not in ("source_path", "subject", "instructions", "fingerprint")}
        return None


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
