"""Encrypted, expiring browser state kept outside cookies and application source."""

import base64
import hashlib
import json
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class BrowserState:
    def __init__(self, state, key):
        if len(key) < 32:
            raise ValueError("WIKI_WEB_SESSION_KEY must contain at least 32 characters.")
        self.state = state
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
        with state.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS browser_logins (
                    id TEXT PRIMARY KEY, binding TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL,
                    payload TEXT NOT NULL, expires REAL NOT NULL, touched REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS identity_profiles (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL, name TEXT NOT NULL,
                    email TEXT NOT NULL, username TEXT NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(issuer,subject));
                CREATE TABLE IF NOT EXISTS browser_logout_events (
                    issuer TEXT NOT NULL, jti TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(issuer,jti));
                CREATE TABLE IF NOT EXISTS browser_revocations (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL, sid TEXT NOT NULL, issued REAL NOT NULL,
                    PRIMARY KEY(issuer,subject,sid));
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(browser_sessions)")}
            for name, definition in (("sid", "TEXT NOT NULL DEFAULT ''"), ("issued", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE browser_sessions ADD COLUMN {name} {definition}")

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        try:
            return json.loads(self.cipher.decrypt(value.encode()))
        except (InvalidToken, ValueError):
            return None

    def login(self, return_to):
        transaction = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                       "verifier": secrets.token_urlsafe(48), "return_to": return_to}
        binding = secrets.token_urlsafe(32)
        with self.state.connect() as db:
            db.execute("DELETE FROM browser_logins WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM browser_sessions WHERE expires<? OR touched<?",
                       (time.time(), time.time() - 3600))
            if db.execute("SELECT count(*) FROM browser_logins").fetchone()[0] >= 2000:
                raise ValueError("Too many unfinished sign-ins. Try again shortly.")
            db.execute("INSERT INTO browser_logins VALUES(?,?,?,?)",
                       (digest(transaction["state"]), digest(binding), self.encrypt(transaction), time.time() + 600))
        return transaction, binding

    def consume_login(self, state, binding):
        if not state or not binding or max(len(state), len(binding)) > 128:
            return None
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM browser_logins WHERE id=? AND binding=? AND expires>?",
                             (digest(state), digest(binding), time.time())).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM browser_logins WHERE id=?", (row["id"],))
        return self.decrypt(row["payload"])

    def create_session(self, issuer, subject, token, old=None):
        identifier = secrets.token_urlsafe(32)
        payload = {"token": token, "csrf": secrets.token_urlsafe(32)}
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            issued, sid = token.get("_issued_at", time.time()), token.get("_sid", "")
            revoked = db.execute("SELECT 1 FROM browser_revocations WHERE issuer=? AND issued>=? AND "
                                 "((subject=? AND sid='') OR (sid!='' AND sid=?))",
                                 (issuer, issued, subject, sid)).fetchone()
            if revoked:
                raise ValueError("This provider session has been revoked. Sign in again.")
            if old:
                db.execute("DELETE FROM browser_sessions WHERE id=?", (digest(old),))
            db.execute("INSERT INTO browser_sessions(id,issuer,subject,payload,expires,touched,sid,issued) "
                       "VALUES(?,?,?,?,?,?,?,?)", (digest(identifier), issuer, subject, self.encrypt(payload),
                                                 time.time() + 43200, time.time(), sid, issued))
        return identifier, payload

    def session(self, identifier, issuer):
        if not identifier or len(identifier) > 128:
            return None
        with self.state.connect() as db:
            row = db.execute("SELECT * FROM browser_sessions WHERE id=? AND issuer=? AND expires>? AND touched>?",
                             (digest(identifier), issuer, time.time(), time.time() - 3600)).fetchone()
            if not row:
                return None
            payload = self.decrypt(row["payload"])
            if not payload:
                return None
            db.execute("UPDATE browser_sessions SET touched=? WHERE id=?", (time.time(), row["id"]))
        return {**dict(row), **payload}

    def update_session(self, identifier, token, csrf):
        with self.state.connect() as db:
            db.execute("UPDATE browser_sessions SET payload=? WHERE id=?",
                       (self.encrypt({"token": token, "csrf": csrf}), digest(identifier)))

    def delete_session(self, identifier):
        if identifier and len(identifier) <= 128:
            with self.state.connect() as db:
                db.execute("DELETE FROM browser_sessions WHERE id=?", (digest(identifier),))

    def put_profile(self, issuer, value):
        with self.state.connect() as db:
            db.execute("INSERT INTO identity_profiles VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(issuer,subject) DO UPDATE SET name=excluded.name,email=excluded.email,"
                       "username=excluded.username,updated=excluded.updated",
                       (issuer, value["subject"], value["name"], value["email"], value["username"], time.time()))

    def profile(self, issuer, subject):
        with self.state.connect() as db:
            row = db.execute("SELECT subject,name,email,username FROM identity_profiles WHERE issuer=? AND subject=?",
                             (issuer, subject)).fetchone()
        return dict(row) if row else {"subject": subject, "name": "Unknown person", "email": "", "username": ""}

    def search_profiles(self, issuer, query):
        # Literal substring search, not SQL LIKE wildcards supplied by a caller.
        with self.state.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT subject,name,email,username FROM identity_profiles WHERE issuer=? "
                "AND (instr(lower(name),?) OR instr(lower(email),?) OR instr(lower(username),?)) "
                "ORDER BY name,subject LIMIT 20", (issuer, query.lower(), query.lower(), query.lower()))]

    def provider_logout(self, issuer, claims):
        """Consume once and fence concurrent login completion in the same transaction."""
        subject, sid, issued = claims.get("sub", ""), claims.get("sid", ""), claims["iat"]
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM browser_logout_events WHERE expires<?", (time.time(),))
            if db.execute("SELECT 1 FROM browser_logout_events WHERE issuer=? AND jti=?",
                          (issuer, claims["jti"])).fetchone():
                return
            db.execute("INSERT INTO browser_logout_events VALUES(?,?,?)", (issuer, claims["jti"], issued + 330))
            # sid narrows a logout even when sub is also supplied.
            key_subject = "" if sid else subject
            db.execute("INSERT INTO browser_revocations VALUES(?,?,?,?) ON CONFLICT(issuer,subject,sid) "
                       "DO UPDATE SET issued=max(issued,excluded.issued)", (issuer, key_subject, sid, issued))
            db.execute("DELETE FROM browser_sessions WHERE issuer=? AND issued<=? AND "
                       "(?='' OR subject=?) AND (?='' OR sid=?)", (issuer, issued, subject, subject, sid, sid))

    def people(self, issuer):
        with self.state.connect() as db:
            rows = db.execute("SELECT subject FROM identity_profiles WHERE issuer=? "
                              "UNION SELECT subject FROM grants WHERE subject NOT LIKE 'agent:%' "
                              "UNION SELECT subject FROM user_status "
                              "UNION SELECT key FROM copy_settings WHERE kind='user' "
                              "UNION SELECT owner FROM wikis UNION SELECT owner FROM agents ORDER BY 1", (issuer,))
            subjects = [row[0] for row in rows]
        return [self.profile(issuer, subject) for subject in subjects]
