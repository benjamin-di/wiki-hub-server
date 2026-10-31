"""Git/LFS accepted state and isolated ingest copies, using normal Git merges."""

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from .seed import initial_files

CONTENT_DIRS = {"wiki", "raw", "reference", "output"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,62}")
REVISION = re.compile(r"[0-9a-f]{7,40}")
LFS_POINTER = re.compile(rb"version https://git-lfs.github.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize ([0-9]+)\n")
log = logging.getLogger(__name__)


class ContentError(ValueError):
    pass


class GitError(Exception):
    pass


class PushRejected(GitError):
    """Git received an explicit rejection for the requested remote ref."""


PACK_LIMITS = {"pack.threads": "2", "index.threads": "2", "pack.windowMemory": "64m",
               "pack.deltaCacheSize": "32m", "core.bigFileThreshold": "16m"}


def validate_slug(slug):
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ContentError("Use a wiki name beginning with a lowercase letter, followed by letters, digits or hyphens.")
    return slug


def content_path(path):
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        raise ContentError("Invalid content path.")
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or not value.parts or value.parts[0] not in CONTENT_DIRS:
        raise ContentError("Only wiki, raw, reference and output content is accessible.")
    if any(part.startswith(".") or ":" in part for part in value.parts):
        raise ContentError("Hidden files and platform-specific paths are unsupported.")
    return str(value)


class GitStore:
    def __init__(self, settings, forgejo):
        self.settings, self.forgejo = settings, forgejo
        self.root = settings.state_dir / "repos"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        settings.workspace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.askpass = settings.state_dir / "git-askpass"
        self.askpass.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' -m wiki_hub.git_auth "$@"\n'
        )
        self.askpass.chmod(0o700)

    def run(self, path, *args, check=True, timeout=120, input_data=None, smudge=False, config=None, output_file=None):
        env = os.environ.copy()
        env.update(
            GIT_ASKPASS=str(self.askpass.resolve()),
            GIT_TERMINAL_PROMPT="0",
            GIT_LFS_SKIP_SMUDGE="1",
            WIKI_GIT_USERNAME=self.settings.forgejo_username,
            WIKI_GIT_TOKEN=self.settings.forgejo_password,
            WIKI_GIT_ALLOWED_URLS=json.dumps([getattr(self.settings, name, "")
                                            for name in ("forgejo_url", "forgejo_public_url")]),
            LC_ALL="C",
        )
        if smudge:
            env.pop("GIT_LFS_SKIP_SMUDGE", None)
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "credential.helper=",
                    # Bound packing memory inside the container: one delta thread per CPU over large,
                    # similar JSON files exceeded a 512 MiB limit. Larger files are stored without deltas.
                    *[part for key, value in PACK_LIMITS.items() for part in ("-c", f"{key}={value}")],
                    *[part for key, value in (config or {}).items() for part in ("-c", f"{key}={value}")],
                    *args,
                ],
                cwd=path,
                env=env,
                stdout=output_file if output_file is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                input=input_data,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"Git {args[0]} could not complete. Inspect the repository/job before retrying.") from exc
        if check and result.returncode:
            if args[0] == "push" and "--porcelain" in args:
                for line in result.stdout.splitlines():
                    fields = line.split(b"\t", 2)
                    if (len(fields) == 3 and fields[0] == b"!"
                            and fields[2].startswith((b"[rejected]", b"[remote rejected]"))):
                        raise PushRejected("Git rejected publication; inspect the retained working copy.")
            # Raw subprocess errors may contain server material; return a bounded operation label only.
            raise GitError(
                f"Git {args[0]} failed (exit {result.returncode}). Inspect the repository/job before retrying."
            )
        return result.stdout or b""

    def lfs(self, path, wiki, *args, extra_config=None, **kwargs):
        # A writer's .lfsconfig must not redirect privileged server requests to
        # another repository or host. File remotes are only used by offline tests.
        url = self.forgejo.repository_url(wiki["repo_owner"], wiki["slug"])
        config = {}
        if urlsplit(url).scheme in ("http", "https"):
            endpoint = url.rstrip("/") + "/info/lfs"
            config = dict.fromkeys(("lfs.url", "lfs.pushurl", "remote.origin.lfsurl", "remote.origin.lfspushurl"),
                                   endpoint)
            config.update({"lfs.remote.autodetect": "false", "lfs.remote.searchall": "false"})
            if getattr(self.settings, "forgejo_public_url", ""):
                public = self.forgejo.repository_url(wiki["repo_owner"], wiki["slug"], public=True)
                if public != url and urlsplit(public).scheme in ("http", "https"):
                    # Forgejo may advertise its public origin in object actions.
                    # Keep both LFS path forms on this repository's backend URL.
                    config["lfs.transfer.enablehrefrewrite"] = "true"
                    for suffix in (".git/info/lfs/", "/info/lfs/"):
                        internal = url.removesuffix(".git") + suffix
                        external = public.removesuffix(".git") + suffix
                        config[f"url.{internal}.insteadOf"] = external
                        config[f"url.{internal}.pushInsteadOf"] = external
        config.update(extra_config or {})
        return self.run(path, "lfs", *args, config=config, **kwargs)

    def repo(self, slug):
        return self.root / validate_slug(slug)

    def seed(self, wiki, purpose=""):
        repo = self.repo(wiki["slug"])
        if not repo.exists():
            self.run(self.root, "clone", self.forgejo.repository_url(wiki["repo_owner"], wiki["slug"]), str(repo))
        self.run(repo, "config", "user.name", "Wiki Hub")
        self.run(repo, "config", "user.email", "wiki-hub@localhost")
        self.run(repo, "lfs", "install", "--local", "--skip-smudge", "--skip-repo")
        if self.run(repo, "rev-parse", "--verify", "HEAD", check=False).strip():
            if not self.run(repo, "ls-remote", "--heads", "origin", "refs/heads/main").strip():
                # A failed first push must be retried before creation is declared ready.
                self.run(repo, "push", "-u", "origin", "HEAD:main")
            else:
                self.sync(wiki)
            return
        self.run(repo, "checkout", "-B", "main")
        for name in CONTENT_DIRS:
            (repo / name).mkdir(exist_ok=True)
        for name, content in initial_files(wiki["title"], purpose).items():
            (repo / name).write_text(content, encoding="utf-8")
        self.run(repo, "add", "AGENTS.md", ".gitattributes", "wiki/index.md")
        self.run(repo, "commit", "-m", "Create wiki")
        self.run(repo, "push", "-u", "origin", "HEAD:main")

    def sync(self, wiki):
        repo = self.repo(wiki["slug"])
        self.run(repo, "fetch", "origin", "main")
        self.run(repo, "merge", "--ff-only", "origin/main")
        return repo

    def revision(self, repo):
        return self.run(repo, "rev-parse", "HEAD").decode().strip()

    def list_files(self, repo, revision="HEAD"):
        # Git paths are bytes; an undecodable name must not break listing or search.
        return [p.decode(errors="surrogateescape")
                for p in self.run(repo, "ls-tree", "-r", "--name-only", "-z", revision).split(b"\0") if p]

    def blobs(self, repo, revision="HEAD"):
        """File paths only: submodule entries have no content to read."""
        result = []
        for entry in self.run(repo, "ls-tree", "-r", "-z", revision).split(b"\0"):
            if entry:
                meta, path = entry.split(b"\t", 1)
                if meta.split()[1] == b"blob":
                    result.append(path.decode(errors="surrogateescape"))
        return result

    def accepted_revision(self, repo, revision=None):
        if revision is None:
            return self.revision(repo)
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ContentError("Use a full Git commit hash from this wiki's accepted history.")
        try:
            self.run(repo, "merge-base", "--is-ancestor", revision, "HEAD")
        except GitError:
            raise ContentError("This revision is not in the wiki's accepted history.") from None
        return revision

    def read(self, repo, path, revision=None):
        target = self.accepted_revision(repo, revision) if revision is not None else "HEAD"
        return self.run(repo, "show", target + ":" + content_path(path))

    def texts(self, repo, paths, revision):
        # One Git process, with output spooled to disk rather than retaining the
        # whole collection in application memory. NUL inputs preserve filenames.
        if not paths:
            return
        requests = b"\0".join((revision + ":" + content_path(path)).encode() for path in paths) + b"\0"
        with tempfile.TemporaryFile(dir=self.settings.state_dir) as stream:
            self.run(repo, "cat-file", "--batch", "-z", input_data=requests, output_file=stream)
            stream.seek(0)
            for path in paths:
                line = stream.readline()
                header = line.split()
                if line.endswith(b" missing\n"):
                    # A submodule or other non-blob entry has no searchable text.
                    yield path, None
                    continue
                if len(header) != 3 or not header[2].isdigit():
                    raise ContentError("Git returned an unexpected searchable entry.")
                data = stream.read(int(header[2]))
                if stream.read(1) != b"\n":
                    raise ContentError("Git returned incomplete searchable content.")
                if header[1] != b"blob":
                    yield path, None
                    continue
                try:
                    yield path, data.decode("utf-8")
                except UnicodeError:
                    # A direct Git push may add non-UTF-8 text; skip it rather than disabling search.
                    yield path, None

    def accepted(self, wiki, revision):
        repo = self.repo(wiki["slug"])
        self.run(repo, "fetch", "origin", "main")
        return self.run(repo, "merge-base", revision, "origin/main").decode().strip() == revision

    def worktree(self, wiki, job_id):
        repo = self.sync(wiki)
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ContentError("Invalid job identifier.")
        path = self.settings.workspace_dir / job_id
        self.run(repo, "worktree", "add", "--detach", str(path.resolve()), "HEAD")
        for name in CONTENT_DIRS:
            # A pushed symlink here would redirect the server's source writes outside this worktree.
            if (path / name).is_symlink() or ((path / name).exists() and not (path / name).is_dir()):
                raise ContentError(f"The wiki's {name}/ folder must be a real directory, not a link or file.")
            (path / name).mkdir(exist_ok=True)
        return repo, path, self.revision(repo)

    def _validate_files(self, work, candidates):
        for raw in candidates:
            if not raw:
                continue
            path = content_path(raw.decode())
            full = work / path
            for parent in (full, *full.parents):
                if parent == work:
                    break
                if parent.is_symlink():
                    raise ContentError("Symlinks are not supported in wiki content.")
            if full.is_file() and full.stat().st_size > self.settings.upload_limit:
                raise ContentError("A changed file exceeds the configured size limit.")
            if full.is_file() and path.endswith((".md", ".txt")):
                try:
                    full.read_text(encoding="utf-8")
                except UnicodeError as exc:
                    raise ContentError("Markdown and text files must contain UTF-8 text.") from exc

    def validate(self, work):
        # HEAD comparison includes staged edits, which `ls-files -m` would miss.
        changed = self.run(work, "diff", "--name-only", "-z", "HEAD").split(b"\0")
        untracked = self.run(work, "ls-files", "-o", "--exclude-standard", "-z").split(b"\0")
        self._validate_files(work, changed + untracked)

    def validate_modes(self, work, *, staged=False):
        if staged:
            entries = self.run(work, "ls-files", "--stage", "-z").split(b"\0")
        else:
            entries = self.run(work, "ls-tree", "-r", "-z", "HEAD").split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            if path.split(b"/", 1)[0].decode() in CONTENT_DIRS and metadata.split()[0] not in (b"100644", b"100755"):
                raise ContentError("Wiki content must contain regular files, without symlinks or nested repositories.")

    def _push_commit(self, wiki, repo, work, base, validate_content=True, prepared=None, job_id=None,
                     merge_concurrent=True):
        edited = [p.decode() for p in self.run(work, "diff", "--name-only", "-z", base, "HEAD").split(b"\0") if p]
        self.lfs(work, wiki, "push", "origin", "HEAD", timeout=600)
        # A concurrent laptop push uses a normal merge; conflicts retain the worktree.
        self.run(work, "fetch", "origin", "main")
        latest = self.run(work, "rev-parse", "origin/main").decode().strip()
        if latest != base:
            if not merge_concurrent:
                raise ContentError("The wiki changed; undo needs owner review of newer work.")
            self.run(work, "merge", "--no-edit", "origin/main")
        if validate_content:
            # Remote root instructions are already accepted and need not be rejected.
            # Check all merged content, including symlinks, rather than an empty HEAD diff.
            files = [p.encode() for p in self.list_files(work) if p.split("/", 1)[0] in CONTENT_DIRS]
            self._validate_files(work, files)
            self.validate_modes(work)
            if job_id:
                self.validate_citations(work, base, job_id, edited)
        revision = self.revision(work)
        if prepared is not None:
            prepared(revision)
        self.run(work, "push", "--porcelain", "origin", "HEAD:main")
        # Canonical readers see a commit only after the remote accepted it.
        try:
            self.run(repo, "fetch", "origin", "main")
            self.run(repo, "merge", "--ff-only", "origin/main")
        except GitError:
            # The remote accepted this revision. A stale reader can retry normal sync.
            log.warning("Published revision %s; canonical refresh needs retry", revision)
        return revision

    def validate_citations(self, work, base, job_id, paths):
        from .citations import validate_edits

        available = set(self.list_files(work))
        changed, previous = {}, {}
        old_paths = {p.decode() for p in self.run(work, "ls-tree", "-r", "--name-only", "-z", base).split(b"\0") if p}
        for path in paths:
            if path in available and path.startswith(("wiki/", "output/")) and path.endswith(".md"):
                changed[path] = self.run(work, "show", "HEAD:" + path).decode("utf-8")
                if path in old_paths:
                    previous[path] = self.run(work, "show", base + ":" + path).decode("utf-8")
        reference = f"reference/{job_id}.txt"
        validate_edits(changed, previous, available, reference if reference in available else None)

    def publish(self, wiki, repo, work, base, subject, job_id, prepared=None, *, client_id=None):
        self.validate(work)
        self.run(work, "add", "--all", "--", *sorted(CONTENT_DIRS))
        self.validate_modes(work, staged=True)
        client = f"\nClient: {client_id}" if client_id else ""
        self.run(work, "commit", "-m", f"Ingest source\n\nPrincipal: {subject}\nJob: {job_id}{client}")
        return self._push_commit(wiki, repo, work, base, prepared=prepared, job_id=job_id)

    def cleanup(self, repo, work):
        # Only successful, application-owned worktrees are removed.
        if work.is_symlink() or work.resolve().parent != self.settings.workspace_dir.resolve():
            raise ContentError("Only application worktrees can be removed.")
        self.run(repo, "worktree", "remove", "--force", str(work.resolve()))

    def history(self, wiki):
        repo = self.sync(wiki)
        raw = self.run(repo, "log", "-30", "--format=%H%x00%aI%x00%s")
        return [
            dict(zip(("revision", "date", "message"), line.split("\x00", 2), strict=True))
            for line in raw.decode().splitlines()
        ]

    def rollback(self, wiki, revision, subject, prepared=None, job_id=None):
        if not isinstance(revision, str) or not REVISION.fullmatch(revision):
            raise ContentError("Use a Git commit hash from this wiki's history.")
        repo = self.sync(wiki)
        self.run(repo, "merge-base", "--is-ancestor", revision, "HEAD")
        repo, work, base = self.worktree(wiki, "rollback-" + uuid4().hex)
        # An owner-requested rollback restores the complete historical wiki, including its instructions.
        self.run(work, "restore", "--source=" + revision, "--staged", "--worktree", ".")
        message = f"Restore {revision}\n\nPrincipal: {subject}"
        if job_id:
            message += f"\nJob: {job_id}"
        self.run(work, "commit", "--allow-empty", "-m", message)
        published = self._push_commit(wiki, repo, work, base, validate_content=False, prepared=prepared)
        try:
            self.cleanup(repo, work)
        except GitError:
            log.warning("Accepted rollback %s retained its working copy for cleanup", published)
        return {"revision": published, "restored": revision}

    def undo_ingest(self, wiki, revision, subject, prepared, job_id):
        """Revert only an unchanged single-parent ingest at the current head."""
        repo, work, base = self.worktree(wiki, "undo-" + uuid4().hex)
        try:
            if base != revision:
                raise ContentError("The wiki changed; undo needs owner review of newer work.")
            parents = self.run(work, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
            if len(parents) != 2:
                raise ContentError("This ingest contains merged work; undo needs owner review.")
            changed = self.run(work, "diff", "--name-only", "-z", parents[1], base).split(b"\0")
            if any(path and path.decode(errors="surrogateescape").split("/", 1)[0] not in CONTENT_DIRS
                   for path in changed):
                raise ContentError("Undo cannot change root instructions or configuration.")
        except ContentError:
            # A refusal does no work, so there is nothing to retain for inspection.
            self.cleanup(repo, work)
            raise
        self.run(work, "revert", "--no-commit", revision)
        self.run(work, "commit", "-m", f"Undo ingest {revision}\n\nPrincipal: {subject}\nJob: {job_id}")
        published = self._push_commit(wiki, repo, work, base, validate_content=False,
                                      prepared=prepared, merge_concurrent=False)
        try:
            self.cleanup(repo, work)
        except GitError:
            log.warning("Accepted undo %s retained its working copy for cleanup", published)
        return {"revision": published, "undone": revision}

    def original(self, wiki, path, revision=None):
        path = content_path(path)
        if not path.startswith("raw/"):
            raise ContentError("Only preserved source documents can be downloaded.")
        repo = self.sync(wiki)
        pointer = self.read(repo, path, revision)
        match = LFS_POINTER.fullmatch(pointer)
        if not match:
            raise ContentError("The source is not a valid Git LFS document.")
        digest, size = match[1].decode(), int(match[2])
        directory = self.settings.state_dir / "originals" / wiki["slug"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / digest
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            # Spool directly to a private file: legacy/bulk originals can exceed
            # the submission limit and must not be buffered in application RAM.
            # Explicit smudging leaves the canonical checkout untouched.
            with os.fdopen(fd, "w+b") as stream:
                self.lfs(repo, wiki, "smudge", "--", path, input_data=pointer, smudge=True,
                         timeout=600, output_file=stream)
                stream.flush()
                stream.seek(0)
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if os.fstat(stream.fileno()).st_size != size or actual != digest:
                    raise ContentError("The source download did not match its stored hash and size.")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target
