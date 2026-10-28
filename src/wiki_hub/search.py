"""Disposable per-wiki FTS5 indexes, bound to an accepted Git revision."""

import sqlite3


def passages(path, revision, text, terms, score):
    lines = text.splitlines()
    if not lines:
        return None
    best = max(range(len(lines)), key=lambda n: sum(term in lines[n].casefold() for term in terms))
    start = max(0, best - 2)
    return {"path": path, "revision": revision, "score": score, "line": start + 1,
            "snippet": "\n".join(lines[start:start + 10])[:2000]}


class SearchIndex:
    def __init__(self, settings, slug):
        self.path = settings.state_dir / "indexes" / (slug + ".db")

    def discard(self):
        # Only derived state is discarded; accepted Git/LFS and the job journal remain intact.
        self.path.unlink(missing_ok=True)

    def search(self, git, repo, revision, terms, limit):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Derived search index must not be a symlink")
        db = sqlite3.connect(self.path)
        try:
            self.path.chmod(0o600)
            db.execute("CREATE TABLE IF NOT EXISTS indexed_revision (revision TEXT NOT NULL)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents USING fts5(path UNINDEXED, content)")
            current = db.execute("SELECT revision FROM indexed_revision").fetchone()
            if not current or current[0] != revision:
                names = [path for path in git.list_files(repo)
                         if path.startswith(("wiki/", "reference/")) and path.endswith((".md", ".txt"))]
                with db:
                    db.execute("DELETE FROM documents")
                    db.executemany("INSERT INTO documents(path,content) VALUES(?,?)", git.texts(repo, names, revision))
                    db.execute("DELETE FROM indexed_revision")
                    db.execute("INSERT INTO indexed_revision VALUES(?)", (revision,))
            query = " OR ".join('"' + term + '"' for term in terms)
            rows = db.execute("SELECT path,content,bm25(documents) AS score FROM documents "
                              "WHERE documents MATCH ? ORDER BY score,path LIMIT ?", (query, limit))
            result = []
            for path, content, score in rows:
                hit = passages(path, revision, content, terms, -score)
                if hit:
                    result.append(hit)
            return result
        finally:
            db.close()
