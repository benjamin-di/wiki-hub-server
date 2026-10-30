"""Disposable per-wiki FTS5 indexes, bound to an accepted Git revision."""

import posixpath
import re
import sqlite3

from .gitstore import ContentError, content_path

SCHEMA = 4
TOKEN = re.compile(r"[^\W_]+")
CLAUSE = re.compile(r'(-?)(?:"([^"]*)"|(\S+))')
HEADING = re.compile(r" {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
SNIPPET_LINES = 8


def searchable(path):
    # A name that is not UTF-8 arrives surrogate-escaped from Git; readers cannot address it.
    if (not path.startswith(("wiki/", "reference/")) or not path.endswith((".md", ".txt"))
            or any("\udc80" <= char <= "\udcff" for char in path)):
        return False
    try:
        return content_path(path) == path
    except ContentError:
        return False


def lines(text):
    """Lines as Git and editors count them: only \\n ends a line."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def outline(text):
    """ATX headings outside fenced code, with 1-based line numbers."""
    headings, fence = [], None
    for number, line in enumerate(lines(text), 1):
        line = line.rstrip("\r\n")
        marker = FENCE.match(line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not line.strip()[len(marker[1]):]:
                fence = None
            continue
        heading = None if fence else HEADING.match(line)
        if heading and heading[2]:
            headings.append({"line": number, "level": len(heading[1]), "heading": heading[2]})
    return headings


def title(path, text):
    headings = outline(text[:20000])
    if headings:
        return headings[0]["heading"][:200]
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:200] or posixpath.basename(path)


def summary(text):
    """The opening paragraph of an index page: what a wiki covers."""
    content = text.splitlines()
    if content and content[0].lstrip("\ufeff").strip() == "---":
        # Existing wiki maps may start with YAML metadata. It is not the overview.
        end = next((n for n, line in enumerate(content[1:], 1) if line.strip() in ("---", "...")), None)
        if end is None:
            return ""
        content = content[end + 1:]
    paragraph = []
    for line in content:
        if HEADING.match(line) or line.lstrip().startswith(("-", "*", "|", ">")):
            if paragraph:
                break
            continue
        if not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line.strip())
    return " ".join(paragraph)[:300]


def normalized(text):
    return " " + " ".join(TOKEN.findall(text.lower())) + " "


def stem(word):
    # A conservative prefix, approximating the index's Porter stemming for snippet selection.
    return word if len(word) <= 4 else word[:max(4, len(word) - 3)]


def bounded(score):
    # Bound scores for merged results; per-wiki BM25 remains dependent on each corpus.
    # Common terms have very small BM25 values; rounding erases their ranking.
    return score / (1 + score)


class Query:
    """Quoted phrases, -exclusions and plain terms, compiled without FTS5 operator injection.

    Each clause reaches FTS5 as one quoted string, so the index tokenizer splits query and
    content identically. Python-side tokens only select snippets and serve the fallback scan.
    """

    def __init__(self, text):
        self.include, self.exclude = [], []
        for negated, phrase, word in CLAUSE.findall(text):
            raw = phrase or word
            tokens = TOKEN.findall(raw.lower())
            if tokens:
                clause = (raw, tokens, bool(phrase) or len(tokens) > 1)
                (self.exclude if negated else self.include).append(clause)

    @staticmethod
    def expression(raw, tokens, phrase, prefix=True):
        quoted = '"' + raw.replace('"', '""') + '"'
        return quoted + "*" if prefix and not phrase and len(tokens[0]) >= 4 else quoted

    def fts(self, any_term=False):
        positive = (" OR " if any_term else " AND ").join(self.expression(*clause) for clause in self.include)
        if not self.exclude:
            return positive
        negative = " OR ".join(self.expression(*clause, prefix=False) for clause in self.exclude)
        return f"({positive}) NOT ({negative})"

    @staticmethod
    def found(raw, tokens, phrase, text, prefix=True):
        if phrase:
            return " " + " ".join(tokens) + " " in text
        return " " + (stem(tokens[0]) if prefix else tokens[0] + " ") in text

    def matches(self, text, any_term=False, path=None):
        """A content-scan approximation of fts(), used only while the index is unavailable."""
        fields = [(normalized(text), 1)]
        if path is not None:
            fields.extend([(normalized(posixpath.splitext(posixpath.basename(path))[0]), 1.5),
                           (normalized(title(path, text)), 4)])
        hits = [max((weight for field, weight in fields if self.found(*clause, field)), default=0)
                for clause in self.include]
        if not any(hits) or (not any_term and not all(hits)):
            return 0
        excluded = any(self.found(*clause, field, prefix=False) for clause in self.exclude for field, _ in fields)
        return 0 if excluded else sum(hits)

    def passage(self, path, revision, text, score):
        content = lines(text)
        if not content:
            return None
        scores = [sum(self.found(*clause, normalized(line)) for clause in self.include) for line in content]
        best = max(range(len(content)), key=scores.__getitem__)
        start = max(0, best - 2)
        heading = next((h["heading"] for h in reversed(outline("".join(content[:best + 1])))), None)
        return {"path": path, "revision": revision, "score": bounded(score), "line": start + 1,
                "match_line": best + 1, "heading": heading,
                "snippet": "".join(content[start:start + SNIPPET_LINES])[:1500]}


class SearchIndex:
    def __init__(self, settings, slug):
        self.path = settings.state_dir / "indexes" / (slug + ".db")

    def discard(self):
        # Only derived state is discarded; accepted Git/LFS and the job journal remain intact.
        self.path.unlink(missing_ok=True)

    def connect(self, git, repo, revision):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError("Derived search index must not be a symlink")
        db = sqlite3.connect(self.path)
        try:
            self.path.chmod(0o600)
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
                with db:
                    db.execute("DROP TABLE IF EXISTS documents")
                    db.execute("DROP TABLE IF EXISTS indexed_revision")
                    db.execute("CREATE TABLE indexed_revision (revision TEXT NOT NULL)")
                    # Only the file name is searchable: folder names and extensions match every page.
                    db.execute("CREATE VIRTUAL TABLE documents USING fts5(path UNINDEXED, name, title, body, "
                               "lines UNINDEXED, tokenize='porter unicode61')")
                    db.execute(f"PRAGMA user_version = {SCHEMA}")
            current = db.execute("SELECT revision FROM indexed_revision").fetchone()
            if not current or current[0] != revision:
                names = [path for path in git.list_files(repo, revision) if searchable(path)]
                with db:
                    db.execute("DELETE FROM documents")
                    db.executemany("INSERT INTO documents(path,name,title,body,lines) VALUES(?,?,?,?,?)", (
                        (path, posixpath.splitext(posixpath.basename(path))[0], title(path, text), text,
                         len(lines(text)))
                        for path, text in git.texts(repo, names, revision) if text is not None))
                    db.execute("DELETE FROM indexed_revision")
                    db.execute("INSERT INTO indexed_revision VALUES(?)", (revision,))
            return db
        except BaseException:
            db.close()
            raise

    def search(self, git, repo, revision, query, limit, any_term=False):
        db = self.connect(git, repo, revision)
        try:
            rows = db.execute("SELECT path,body,bm25(documents,0,1.5,4.0,1.0,0) AS score FROM documents "
                              "WHERE documents MATCH ? ORDER BY score,path LIMIT ?",
                              (query.fts(any_term), limit)).fetchall()
        finally:
            db.close()
        return [hit for path, body, score in rows if (hit := query.passage(path, revision, body, -score))]

    def files(self, git, repo, revision):
        db = self.connect(git, repo, revision)
        try:
            return [{"path": path, "title": title, "lines": count} for path, title, count in db.execute(
                "SELECT path,title,lines FROM documents ORDER BY path")]
        finally:
            db.close()
