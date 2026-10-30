"""Network-drive projection of accepted wiki content, with editable wiki pages.

The application owns every projected tree. One shared tree per wiki holds the
accepted revision; each person gets a private view built from hard links to it,
containing only the wikis they may read. People with write access get a private,
writable copy of `wiki/`; everything else stays read-only. Saved pages are
published as attributed commits after a quiet period, merged per file, and kept
as conflict copies rather than overwriting newer accepted changes. A file's owner
needs no privilege to set its ACL, so access changes apply synchronously with the
grant change that requires them. The file server's account agent only manages
device accounts.
"""

import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import struct
import tempfile
import time
import uuid
from pathlib import Path

from .gitstore import CONTENT_DIRS, LFS_POINTER, content_path

log = logging.getLogger(__name__)

# File-server identities. Device accounts are wd<number>; people are wp<number>.
DEVICE_UID_BASE = 600000
PERSON_GID_BASE = 700000
DRIVE_GROUP_GID = 699999
DRIVE_GROUP = "wikidrive"
BUILD_XATTR = "user.wiki-hub.build"
ACL_XATTR = "system.posix_acl_access"
LFS_BATCH_LIMIT = 1024
# Explorer acts on these when a folder is merely opened (custom names, icons from
# network paths). A wiki writer must not be able to make readers' computers do so.
SHELL_FILES = re.compile(r"(?i)(desktop\.ini|autorun\.inf|.*\.(lnk|url|scf|library-ms|searchconnector-ms))")
DEFAULT_ACL_XATTR = "system.posix_acl_default"
# Only Markdown pages under wiki/ are published from the drive; nothing ingests.
EDITABLE = re.compile(r"(?i)[^/]+\.md")
# Editor temporaries, hidden files and our own conflict copies are never published.
IGNORED = re.compile(r"(?i)(~\$.*|\..*|.*\.(tmp|bak|swp|swx)|.*~|4913|.* \(your edit, conflict [^)]*\)\.md)")
# Entries the application set aside for a person; never published, even as folders.
SET_ASIDE = re.compile(r".* \(your file, conflict [^)]*\)")
PARK_SECONDS = 600
READ_ONLY_TOPS = ("raw", "reference", "output")
UNDEFINED = 0xFFFFFFFF


def acl_value(users=(), groups=(), owner=7):
    """POSIX ACL xattr (version 2) for owner, named users/groups and no one else."""
    entries = [(0x01, owner, UNDEFINED), *[(0x02, perm, uid) for uid, perm in sorted(users)],
               (0x04, 0, UNDEFINED), *[(0x08, perm, gid) for gid, perm in sorted(groups)]]
    if users or groups:
        mask = 0
        for _ident, perm in (*users, *groups):
            mask |= perm
        entries.append((0x10, mask, UNDEFINED))
    entries.append((0x20, 0, UNDEFINED))
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)


def blob_oid(data):
    """Git's object ID for file content, so edits compare directly with accepted blobs."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def open_beneath(root, relative):
    """Open a regular file below root without following any symbolic link.

    Pages in a writer's view are controlled by that person; a link swapped in
    after a scan must never make the application read (and publish) another file.
    """
    parts = Path(relative).parts
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    stream = os.fdopen(handle, "rb")
    if not stat.S_ISREG(os.fstat(handle).st_mode):
        stream.close()
        raise DriveError("Only regular files are read from a drive view.")
    return stream


def read_beneath(root, relative, limit=None):
    """Read a regular file below root without following links (see open_beneath).

    Beyond limit bytes, only the Git blob ID is computed, in bounded chunks.
    """
    with open_beneath(root, relative) as stream:
        info = os.fstat(stream.fileno())
        if limit is None or info.st_size <= limit:
            return stream.read()
        digest = hashlib.sha1(b"blob %d\0" % info.st_size)
        size = 0
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
        if size != info.st_size:
            raise DriveError("The file changed while it was read.")
        return LargeFile(digest.hexdigest())


class LargeFile(str):
    """The blob ID of a file too large to hold in memory; it is never published."""


def current_oid(root, relative):
    """Blob ID of a regular file below root, or None when absent or not a regular file."""
    try:
        data = read_beneath(root, relative, limit=1 << 20)
    except (OSError, DriveError):
        return None
    return str(data) if isinstance(data, LargeFile) else blob_oid(data)


class DriveError(Exception):
    pass


class DriveUnavailable(DriveError):
    """The file server did not confirm a requested account change in time."""


def device_account(number):
    return f"wd{number}"


def person_group(number):
    return f"wp{number}"


def read_acl_groups(path):
    try:
        data = os.getxattr(path, ACL_XATTR)
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return set()
        raise
    groups = set()
    for offset in range(4, len(data), 8):
        tag, perm, ident = struct.unpack_from("<HHI", data, offset)
        if tag == 0x08 and perm & 4:
            groups.add(ident - PERSON_GID_BASE)
    return groups


def exchange(source, target):
    """Atomically swap two paths (directories or files), so readers never see a partial state."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise DriveError("Atomic directory exchange is unavailable on this platform.")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    at_fdcwd, rename_exchange = -100, 2
    if renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(target), rename_exchange) != 0:
        code = ctypes.get_errno()
        raise DriveError(f"Atomic exchange failed ({errno.errorcode.get(code, code)}).")


def remove_tree(path):
    def writable(function, name, _info):
        os.chmod(os.path.dirname(name), 0o700)
        function(name)

    if path.exists() or path.is_symlink():
        shutil.rmtree(path, onexc=writable)


def desktop_ini(title):
    # Explorer shows this name for a read-only folder while the path stays stable.
    clean = re.sub(r"[\x00-\x1f]", " ", title).strip() or "Wiki"
    return ("\ufeff[.ShellClassInfo]\r\nLocalizedResourceName=" + clean + "\r\n").encode("utf-16-le")


class Drive:
    def __init__(self, settings, git):
        self.settings, self.git = settings, git
        self.enabled = settings.drive_enabled
        if not self.enabled:
            return
        self.root = Path(settings.drive_dir)
        # Accepted trees, never served directly; people's views link to them.
        self.shared = self.root / "shared"
        self.views = self.root / "views"
        self.inbox = self.root / "inbox"
        self.staging = self.root / "staging"
        # A writer's pages moved out of reach while their access changes.
        self.parked = self.root / "parked"
        # Derived LFS objects live with the trees, outside backed-up state.
        self.lfs_root = self.root / "lfs"
        self.control = Path(settings.drive_control_dir)
        self.meta = settings.state_dir / "drive-manifests"
        # Backed up: edits that could not be published stay recoverable here.
        self.unpublished = settings.state_dir / "drive-unpublished"

    # Filesystem layout ---------------------------------------------------

    def prepare(self):
        for path, mode in ((self.root, 0o755), (self.inbox, 0o711), (self.shared, 0o700), (self.views, 0o711),
                           (self.staging, 0o700), (self.lfs_root, 0o700), (self.parked, 0o700),
                           (self.meta, 0o700), (self.meta / "views", 0o700), (self.meta / "parked", 0o700),
                           (self.unpublished, 0o700), (self.control, 0o700), (self.control / "secrets", 0o700)):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        for leftover in self.staging.iterdir():
            # A crash while restoring a parked writer can leave their only copy
            # in an unfinished view. Retain it before cleaning derived staging.
            match = re.fullmatch(r"view-([0-9]+)-([a-z][a-z0-9-]*)-[a-f0-9]{32}", leftover.name)
            if match and not leftover.is_symlink() and (leftover / "wiki").is_dir():
                self.retain(int(match[1]), match[2], "View creation was interrupted.", live=leftover / "wiki")
            remove_tree(leftover)

    def prepare_inbox(self, number):
        path = self.inbox / person_group(number)
        if path.is_symlink():
            raise DriveError("The inbox must not be a symlink.")
        path.mkdir(mode=0o700, exist_ok=True)
        # The service owns the folder. Only this person's primary Samba group
        # may use it; defaults preserve that boundary for new files/directories.
        value = acl_value(users=[(os.getuid(), 7)], groups=[(PERSON_GID_BASE + number, 7)])
        os.setxattr(path, ACL_XATTR, value)
        os.setxattr(path, DEFAULT_ACL_XATTR, value)
        return path.name

    def manifest(self, slug):
        path = self.meta / (slug + ".json")
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        try:
            build = os.getxattr(self.shared / slug, BUILD_XATTR).decode()
        except OSError:
            return None
        # A crash between exchange and manifest update must not let a later
        # build link files whose recorded identities describe another tree.
        return data if data.get("build") == build else None

    def _save_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)

    def projected(self, slug):
        data = self.manifest(slug)
        return {"revision": data["revision"]} if data else None

    # Accepted content ----------------------------------------------------

    def snapshot(self, wiki, projected=None):
        """Return the accepted revision and its readable content entries.

        Entries are omitted when the revision is already ``projected``.
        """
        repo = self.git.sync(wiki)
        revision = self.git.revision(repo)
        if revision == projected:
            return {"revision": revision, "entries": None, "repo": str(repo)}
        entries = {}
        raw = self.git.run(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            path = path.decode("utf-8", "surrogateescape")
            if kind != "blob" or mode not in ("100644", "100755") or path.split("/", 1)[0] not in CONTENT_DIRS:
                continue
            try:
                content_path(path)
            except ValueError:
                log.warning("Drive skipped an unsupported path in %s", wiki["slug"])
                continue
            if SHELL_FILES.fullmatch(path.rsplit("/", 1)[-1]):
                log.warning("Drive skipped a file Windows Explorer would act on in %s", wiki["slug"])
                continue
            entries[path] = oid
        return {"revision": revision, "entries": entries, "repo": str(repo)}

    def _write_blobs(self, wiki, snapshot, target, needed):
        """Write Git blobs; LFS pointers become their verified original bytes."""
        by_oid = {}
        for path, oid in needed.items():
            by_oid.setdefault(oid, []).append(path)
        pointers = {}
        if not by_oid:
            return
        repo = snapshot["repo"]
        requests = "".join(oid + "\n" for oid in by_oid).encode()
        with tempfile.TemporaryFile(dir=self.settings.state_dir) as stream:
            self.git.run(repo, "cat-file", "--batch", input_data=requests, output_file=stream, timeout=3600)
            stream.seek(0)
            for oid, paths in by_oid.items():
                header = stream.readline().split()
                if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
                    raise DriveError("Git returned an unexpected object for the drive projection.")
                size = int(header[2])
                head = stream.read(min(size, LFS_BATCH_LIMIT))
                match = LFS_POINTER.fullmatch(head) if size <= LFS_BATCH_LIMIT else None
                if match:
                    pointers[oid] = (match[1].decode(), int(match[2]), paths)
                else:
                    first = target / paths[0]
                    with open(first, "xb") as output:
                        output.write(head)
                        remaining = size - len(head)
                        while remaining:
                            chunk = stream.read(min(remaining, 1 << 20))
                            if not chunk:
                                raise DriveError("Git returned incomplete content for the drive projection.")
                            output.write(chunk)
                            remaining -= len(chunk)
                    for other in paths[1:]:
                        os.link(first, target / other)
                if stream.read(1) != b"\n":
                    raise DriveError("Git returned incomplete content for the drive projection.")
        if pointers:
            self._write_originals(wiki, repo, snapshot["revision"], target, pointers)

    def _write_originals(self, wiki, repo, revision, target, pointers):
        """Link verified originals from this wiki's drive object store, fetching missing ones."""
        storage = self.lfs_root / wiki["slug"]
        cache = Path(repo) / ".git" / "lfs" / "objects"
        for digest, size, paths in pointers.values():
            stored = storage / "objects" / digest[:2] / digest[2:4] / digest
            if not (stored.is_file() and stored.stat().st_size == size):
                stored.parent.mkdir(parents=True, exist_ok=True)
                cached = cache / digest[:2] / digest[2:4] / digest
                existed = cached.exists()
                pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{digest}\nsize {size}\n"
                fd, temporary = tempfile.mkstemp(dir=stored.parent)
                try:
                    with os.fdopen(fd, "w+b") as stream:
                        self.git.lfs(repo, wiki, "smudge", "--", paths[0], input_data=pointer.encode(), smudge=True,
                                     timeout=3600, output_file=stream)
                        stream.flush()
                        stream.seek(0)
                        if (os.fstat(stream.fileno()).st_size != size
                                or hashlib.file_digest(stream, "sha256").hexdigest() != digest):
                            raise DriveError("An original's stored bytes did not match its recorded hash and size.")
                    os.replace(temporary, stored)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                    if not existed:
                        # Smudging caches the object; the drive keeps its own copy instead.
                        cached.unlink(missing_ok=True)
            for path in paths:
                os.link(stored, target / path)

    def build(self, wiki, snapshot):
        """Build a complete shared tree beside the live one; unchanged files are hard links."""
        slug = wiki["slug"]
        current = self.manifest(slug)
        live = self.shared / slug
        build = uuid.uuid4().hex
        target = self.staging / f"{slug}.{build}"
        target.mkdir(mode=0o700)
        try:
            needed = {}
            known = current["files"] if current else {}
            directories = set()
            for path, oid in sorted(snapshot["entries"].items()):
                destination = target / path
                parent = destination.parent
                if parent not in directories:
                    parent.mkdir(parents=True, exist_ok=True)
                    directories.add(parent)
                if known.get(path) == oid:
                    try:
                        os.link(live / path, destination)
                        continue
                    except OSError:
                        pass
                needed[path] = oid
            self._write_blobs(wiki, snapshot, target, needed)
            (target / "desktop.ini").write_bytes(desktop_ini(wiki["title"]))
            for directory, subdirectories, files in os.walk(target):
                for name in files:
                    os.chmod(os.path.join(directory, name), 0o444)
                for name in subdirectories:
                    os.chmod(os.path.join(directory, name), 0o755)
            os.setxattr(target, BUILD_XATTR, build.encode())
            return {"build": build, "path": target, "revision": snapshot["revision"],
                    "files": snapshot["entries"], "title": wiki["title"]}
        except BaseException:
            remove_tree(target)
            raise

    def install(self, slug, built):
        """Publish a finished shared tree atomically; views are relinked afterwards."""
        live = self.shared / slug
        if live.exists():
            exchange(built["path"], live)
        else:
            os.rename(built["path"], live)
        self._save_json(self.meta / (slug + ".json"), {
            "build": built["build"], "revision": built["revision"], "title": built["title"],
            "files": built["files"]})
        remove_tree(built["path"])

    def collect(self, slug):
        """Delete fetched originals that no projected tree links to any more."""
        store = self.lfs_root / slug / "objects"
        if not store.is_dir():
            return
        for directory, _subdirectories, files in os.walk(store):
            for name in files:
                path = os.path.join(directory, name)
                if re.fullmatch(r"[0-9a-f]{64}", name) and os.lstat(path).st_nlink == 1:
                    os.unlink(path)


    # People's views -------------------------------------------------------

    def person_root(self, number):
        return self.views / person_group(number)

    def _state_path(self, number, slug):
        return self.meta / "views" / person_group(number) / (slug + ".json")

    def view_state(self, number, slug):
        return self._read_view_state(self._state_path(number, slug))

    def _read_view_state(self, path):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if (not isinstance(state, dict) or state.get("mode") not in ("read", "write")
                or not isinstance(state.get("base"), dict) or not isinstance(state.get("stat", {}), dict)):
            return None
        return state

    def save_view_state(self, number, slug, state):
        self._save_json(self._state_path(number, slug), state)

    def view_people(self):
        return sorted(int(path.name[2:]) for path in self.views.iterdir()
                      if re.fullmatch(r"wp[0-9]+", path.name) and not path.is_symlink())

    def view_wikis(self, number):
        root = self.person_root(number)
        if not root.is_dir():
            return []
        return sorted(path.name for path in root.iterdir() if not path.is_symlink() and path.is_dir())

    def _ensure_person(self, number):
        root = self.person_root(number)
        if root.is_symlink():
            raise DriveError("A drive view must not be a symlink.")
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        # Only this person's devices may enter; they cannot add top-level entries.
        os.setxattr(root, ACL_XATTR, acl_value(groups=[(PERSON_GID_BASE + number, 5)]))
        return root

    def _writable_acl(self, number, directory):
        perm = 7 if directory else 6
        value = acl_value(users=[(os.getuid(), perm)], groups=[(PERSON_GID_BASE + number, perm)],
                          owner=7 if directory else 6)
        return value

    def _link_subtree(self, slug, top, target):
        """Hard-link a read-only part of the shared tree into a staged directory."""
        source = self.shared / slug / top
        # Explicit modes: mkdir's mode is reduced by the process umask.
        target.mkdir()
        os.chmod(target, 0o755)
        if not source.is_dir():
            return
        for directory, subdirectories, files in os.walk(source):
            relative = Path(directory).relative_to(source)
            for name in subdirectories:
                (target / relative / name).mkdir()
                os.chmod(target / relative / name, 0o755)
            for name in files:
                os.link(Path(directory) / name, target / relative / name)

    def _copy_writable(self, number, slug, target, files=None):
        """A private, writable copy of wiki/ for one writer."""
        source = self.shared / slug / "wiki"
        target.mkdir(mode=0o770)
        self._set_writable(number, target, True)
        base = {}
        if source.is_dir():
            for directory, subdirectories, names in os.walk(source):
                relative = Path(directory).relative_to(source)
                for name in subdirectories:
                    (target / relative / name).mkdir(mode=0o770)
                    self._set_writable(number, target / relative / name, True)
                for name in names:
                    path = "wiki/" + (relative / name).as_posix() if str(relative) != "." else "wiki/" + name
                    self._place(number, Path(directory) / name, target / relative / name, path)
                    if files is not None:
                        base[path] = files.get(path)
        return base

    def _set_writable(self, number, path, directory):
        os.chmod(path, 0o770 if directory else 0o660)
        os.setxattr(path, ACL_XATTR, self._writable_acl(number, directory))
        if directory:
            os.setxattr(path, DEFAULT_ACL_XATTR, self._writable_acl(number, True))

    def _place(self, number, source, destination, path):
        """Copy an accepted file into a writer's view; only pages become writable."""
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".drive-")
        try:
            with os.fdopen(fd, "wb") as output, open(source, "rb") as stream:
                shutil.copyfileobj(stream, output)
            if EDITABLE.fullmatch(destination.name):
                self._set_writable(number, temporary, False)
            else:
                os.chmod(temporary, 0o444)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _make_parents(self, number, slug, path):
        """Create missing folders inside a writer's wiki/ with the writable ACL."""
        live = self.person_root(number) / slug
        folder = live
        for part in Path(path).parts[:-1]:
            folder = folder / part
            if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
                self._set_aside(folder)  # The person's own file where accepted content needs a folder.
            if not folder.exists():
                folder.mkdir(mode=0o770)
                self._set_writable(number, folder, True)

    def _set_aside(self, path):
        """Keep a person's conflicting entry under a visible, never-published name."""
        stamp = time.strftime("%Y-%m-%d %H%M%S", time.gmtime())
        for counter in range(100):
            suffix = f" (your file, conflict {stamp}{'' if not counter else f' {counter}'})"
            target = path.with_name(path.name + suffix)
            if not target.exists() and not target.is_symlink():
                os.rename(path, target)
                return target
        raise DriveError("Too many conflicting copies of one drive entry.")

    def _swap_in(self, staged, live):
        if live.exists():
            exchange(staged, live)
            remove_tree(staged)
        else:
            os.rename(staged, live)

    def materialize(self, number, wiki, mode):
        """Bring one person's view of one wiki to the shared revision and access mode."""
        slug = wiki["slug"]
        shared = self.manifest(slug)
        if shared is None:
            return False
        root = self._ensure_person(number)
        live = root / slug
        if mode != "write":
            self.settle(number, slug)
        state = self.view_state(number, slug) if live.is_dir() else None
        if state is None and live.exists():
            self.withdraw(number, slug)
        files = shared["files"]
        if not live.exists():
            staged = self.staging / f"view-{number}-{slug}-{uuid.uuid4().hex}"
            staged.mkdir()
            os.chmod(staged, 0o755)
            try:
                os.link(self.shared / slug / "desktop.ini", staged / "desktop.ini")
                for top in READ_ONLY_TOPS:
                    self._link_subtree(slug, top, staged / top)
                base, parked = {}, None
                if mode == "write":
                    parked = self._unpark(number, slug, staged / "wiki")
                    if parked is None:
                        base = self._copy_writable(number, slug, staged / "wiki", files)
                else:
                    self._link_subtree(slug, "wiki", staged / "wiki")
                os.rename(staged, live)
            except BaseException:
                if parked is not None and (staged / "wiki").is_dir():
                    self._park_path(number, slug).mkdir(mode=0o700)
                    self._save_json(self._park_state_path(number, slug), parked)
                    os.rename(staged / "wiki", self._park_path(number, slug) / "wiki")
                remove_tree(staged)
                raise
            state = parked or {"base": base, "stat": {}}
            state.pop("parked", None)
            state.update(mode=mode, revision=shared["revision"], build=shared["build"])
            if parked is not None:
                # Access came back (a role or setting change in progress): the
                # person's pages return unchanged and follow accepted content.
                self._sync_writable(number, slug, state, files)
            self.save_view_state(number, slug, state)
            return True
        # Any rebuild of the shared tree, even at the same revision, relinks views.
        stale = state.get("build") != shared["build"]
        if stale:
            for top in READ_ONLY_TOPS:
                staged = self.staging / f"view-{number}-{slug}-{top}-{uuid.uuid4().hex}"
                self._link_subtree(slug, top, staged)
                self._swap_in(staged, live / top)
            temporary = live / f".desktop-{uuid.uuid4().hex}"
            os.link(self.shared / slug / "desktop.ini", temporary)
            os.replace(temporary, live / "desktop.ini")
        if mode == "read" and (stale or state["mode"] == "write"):
            if state["mode"] == "write":
                self.retain(number, slug, "Write access ended before these edits were published.")
            staged = self.staging / f"view-{number}-{slug}-wiki-{uuid.uuid4().hex}"
            self._link_subtree(slug, "wiki", staged)
            self._swap_in(staged, live / "wiki")
            state["base"], state["stat"] = {}, {}
        elif mode == "write" and state["mode"] == "read":
            staged = self.staging / f"view-{number}-{slug}-wiki-{uuid.uuid4().hex}"
            base = self._copy_writable(number, slug, staged, files)
            self._swap_in(staged, live / "wiki")
            state["base"], state["stat"] = base, {}
        elif mode == "write":
            self._sync_writable(number, slug, state, files)
        state["mode"], state["revision"], state["build"] = mode, shared["revision"], shared["build"]
        self.save_view_state(number, slug, state)
        return True

    def _sync_writable(self, number, slug, state, files):
        """Follow accepted changes for pages this person has not changed; keep their edits."""
        live = self.person_root(number) / slug
        current = self.scan(number, slug, state)["files"]
        accepted = {path: oid for path, oid in files.items() if path.startswith("wiki/")}
        moved = []  # Folders set aside for an accepted file; the person's copy keeps their pages.
        for path in sorted(set(accepted) | set(state["base"])):
            if path.startswith(tuple(moved)):
                state["base"].pop(path, None)
                state["stat"].pop(path, None)
                continue
            new, base, mine = accepted.get(path), state["base"].get(path), current.get(path)
            page = EDITABLE.fullmatch(path.rsplit("/", 1)[-1])
            if mine != base and page:
                continue  # The person's own page edit (or deletion); the publisher merges it.
            try:
                destination = live / path
                if mine != new:
                    # Re-check just before replacing: a save may have landed since the scan.
                    if current_oid(live, path) != mine:
                        continue
                    if mine is not None and base is None and not page:
                        self._set_aside(destination)  # Their own file at a path accepted content now uses.
                    elif destination.is_dir() and not destination.is_symlink():
                        self._set_aside(destination)
                        moved.append(path + "/")
                    if new is None:
                        if destination.is_file() and not destination.is_symlink():
                            destination.unlink()
                    else:
                        self._make_parents(number, slug, path)
                        self._place(number, self.shared / slug / path, destination, path)
                    state["stat"].pop(path, None)
            except OSError:
                log.warning("A drive view of %s could not follow one accepted file; retrying later", slug)
                continue
            if new is None:
                state["base"].pop(path, None)
            else:
                state["base"][path] = new

    def scan(self, number, slug, state, live=None):
        """Current pages in a writer's view, hashing only files whose metadata changed.

        Hidden folders, editor temporaries and links are skipped. A page that could
        not be published as it is (an invalid path, not UTF-8, too large) is reported
        instead of blocking the person's other edits, and is not taken as a deletion.
        """
        live = live or self.person_root(number) / slug / "wiki"
        root = live.parent
        cache = state.setdefault("stat", {})
        rejected = state.get("rejected", {})
        seen, files, ignored, unpublishable, invalid, present = {}, {}, [], [], {}, set()
        linked = []  # Folders replaced by links: their pages are neither read nor deleted.
        real = live.is_dir() and not live.is_symlink()
        if real:
            for directory, subdirectories, names in os.walk(live):
                kept = []
                for name in subdirectories:
                    full = Path(directory) / name
                    if full.is_symlink():
                        linked.append("wiki/" + full.relative_to(live).as_posix() + "/")
                    elif not name.startswith(".") and not SET_ASIDE.fullmatch(name):
                        kept.append(name)
                subdirectories[:] = kept
                for name in names:
                    full = Path(directory) / name
                    path = "wiki/" + full.relative_to(live).as_posix()
                    present.add(path)
                    info = full.lstat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if IGNORED.fullmatch(name) or SET_ASIDE.fullmatch(name):
                        ignored.append(path)
                        continue
                    key = [info.st_size, info.st_mtime_ns, info.st_ino]
                    cached = cache.get(path)
                    if cached and cached[:3] == key:
                        oid, problem = cached[3], cached[4] if len(cached) > 4 else None
                    else:
                        try:
                            data = read_beneath(root, path, limit=self.settings.upload_limit)
                        except (OSError, DriveError):
                            continue
                        if isinstance(data, LargeFile):
                            oid, problem = str(data), None
                            if EDITABLE.fullmatch(name):
                                problem = "This page is larger than the server accepts."
                        else:
                            oid, problem = blob_oid(data), None
                            if EDITABLE.fullmatch(name):
                                problem = self._page_problem(path, data)
                    seen[path] = [*key, oid, problem]
                    files[path] = oid
                    if not EDITABLE.fullmatch(name):
                        if state["base"].get(path) != oid:
                            unpublishable.append(path)
                    elif problem and state["base"].get(path) != oid:
                        invalid[path] = problem
                    elif rejected.get(path) == oid:
                        invalid[path] = "Publication was refused; change the page to retry."
        state["stat"] = seen
        edits = {path: oid for path, oid in files.items()
                 if EDITABLE.fullmatch(path.rsplit("/", 1)[-1]) and state["base"].get(path) != oid
                 and path not in invalid}
        # Only a page that is really gone is a deletion; a link or unreadable
        # entry in its place (or in place of a folder above it) is not.
        if real:
            edits.update({path: None for path in state["base"]
                          if path not in present and EDITABLE.fullmatch(path.rsplit("/", 1)[-1])
                          and not path.startswith(tuple(linked))})
        # Track content, including deletions, independently of the stat cache.
        # Rehashing after publication or changing another page must not delay it.
        pending, stamp = state.get("pending", {}), time.time()
        state["pending"] = {
            path: pending[path] if path in pending and pending[path][0] == oid else [oid, stamp]
            for path, oid in edits.items()
        }
        return {"files": files, "edits": edits, "ignored": ignored, "unpublishable": sorted(unpublishable),
                "invalid": invalid}

    def ready_edits(self, scan, state, force=False):
        cutoff = time.time() - self.settings.drive_edit_quiet_seconds
        return {path: oid for path, oid in scan["edits"].items()
                if force or state["pending"][path][1] <= cutoff}

    def _page_problem(self, path, data):
        try:
            path.encode("utf-8")
            content_path(path)
        except (ValueError, UnicodeError):
            return "This page name or folder cannot be published."
        if len(data) > self.settings.upload_limit:
            return "This page is larger than the server accepts."
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return "Save this page as UTF-8 to publish it."
        return None

    def read_page(self, number, slug, path):
        return read_beneath(self.person_root(number) / slug, path)

    def page_oid(self, number, slug, path):
        return current_oid(self.person_root(number) / slug, path)

    def write_page(self, number, slug, path, data, expected=False):
        """Replace one page in a writer's view (merged result or accepted version).

        With expected (a blob ID, or None for absent), nothing changes unless the
        page still holds exactly that: a newer save by the person always wins.
        """
        if expected is not False and self.page_oid(number, slug, path) != expected:
            return False
        destination = self.person_root(number) / slug / path
        if data is None:
            if destination.is_file() and not destination.is_symlink():
                destination.unlink()
            return True
        self._make_parents(number, slug, path)
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".drive-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            self._set_writable(number, temporary, False)
            if expected is None or expected is False:
                os.replace(temporary, destination)
                return True
            # Swap atomically, then confirm the page moved out is the one checked: a save
            # landing between the check and the swap is put back instead of overwritten.
            try:
                exchange(temporary, destination)
            except DriveError:
                if not destination.exists():
                    return False  # Deleted meanwhile; the person's change wins.
                raise
            if current_oid(destination.parent, temporary.rsplit("/", 1)[-1]) != expected:
                exchange(temporary, destination)
                return False
            return True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def conflict_copy(self, number, slug, path, data, stamp):
        stem = path.rsplit("/", 1)[-1][:-3]
        # Keep the copy's name within the 255-byte file name limit.
        while len(f"{stem} (your edit, conflict {stamp} 99).md".encode()) > 255:
            stem = stem[:-1]
        folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        for counter in range(100):
            name = f"{stem} (your edit, conflict {stamp}{'' if not counter else f' {counter}'}).md"
            if self.page_oid(number, slug, folder + name) is None and not (
                    self.person_root(number) / slug / (folder + name)).exists():
                break
        else:
            raise DriveError("Too many conflict copies of one page.")
        self.write_page(number, slug, folder + name, data)
        self._keep(number, slug, stamp, {folder + name: data})
        return folder + name

    def _keep(self, number, slug, stamp, pages):
        folder = self.unpublished / person_group(number) / slug / (stamp.replace(" ", "T") + "-" + uuid.uuid4().hex)
        for path, data in pages.items():
            destination = folder / path
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_bytes(data)
            os.chmod(destination, 0o600)

    def retain(self, number, slug, reason, live=None, state=None):
        """Keep everything a writer saved and did not publish in backed-up state.

        That is changed pages, pages that could not be published and any other
        file they saved under wiki/, before their folder becomes read-only or goes.
        """
        if state is None and live is None:
            state = self.view_state(number, slug)
        if state is not None and state.get("mode") != "write":
            return []
        live = live or self.person_root(number) / slug / "wiki"
        if not live.is_dir() or live.is_symlink():
            return []
        has_deletions = False
        if state is None:
            # With no trustworthy base, every regular file may be someone's work.
            # Never infer edits/deletions or republish this recovery copy.
            paths = set()
            for directory, subdirectories, names in os.walk(live):
                subdirectories[:] = [name for name in subdirectories if not (Path(directory) / name).is_symlink()]
                for name in names:
                    full = Path(directory) / name
                    if stat.S_ISREG(full.lstat().st_mode):
                        paths.add("wiki/" + full.relative_to(live).as_posix())
            reason = "View metadata was missing or invalid; retained files require manual recovery. " + reason
        else:
            scan = self.scan(number, slug, state, live)
            paths = {path for path, oid in scan["edits"].items() if oid is not None}
            paths.update(scan["invalid"], scan["unpublishable"])
            has_deletions = any(oid is None for oid in scan["edits"].values())
        if not paths and not has_deletions:
            return []
        stamp = time.strftime("%Y-%m-%d %H%M%S", time.gmtime())
        folder = self.unpublished / person_group(number) / slug / (stamp.replace(" ", "T") + "-" + uuid.uuid4().hex)
        kept = []
        for path in sorted(paths):
            destination = folder / path
            try:
                with open_beneath(live.parent, path) as stream:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with open(destination, "wb") as output:
                        shutil.copyfileobj(stream, output)
                os.chmod(destination, 0o600)
                kept.append(path)
            except (OSError, DriveError) as exc:
                raise DriveError("Unpublished drive files could not be retained; retry before rebuilding.") from exc
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        (folder / "REASON.txt").write_text(reason + "\n")
        self._save_json(folder / "STATE.json", state)
        log.warning("Retained %d unpublished drive files for %s", len(kept), slug)
        return kept

    def _park_path(self, number, slug):
        return self.parked / f"{person_group(number)}-{slug}"

    def _park_state_path(self, number, slug):
        return self.meta / "parked" / person_group(number) / (slug + ".json")

    def _park(self, number, slug):
        """Move a writer's pages out of reach, kept briefly in case access returns."""
        live = self.person_root(number) / slug / "wiki"
        state = self.view_state(number, slug)
        if not state or state.get("mode") != "write" or live.is_symlink() or not live.is_dir():
            return False
        self.settle(number, slug)
        target = self._park_path(number, slug)
        target.mkdir(mode=0o700)
        self._save_json(self._park_state_path(number, slug), {**state, "parked": time.time()})
        os.rename(live, target / "wiki")
        return True

    def _unpark(self, number, slug, destination):
        """Move parked pages to destination; returns their view state, or None."""
        target, path = self._park_path(number, slug), self._park_state_path(number, slug)
        state = self._read_view_state(path)
        if state is None:
            self.settle(number, slug)
            return None
        if not (target / "wiki").is_dir():
            self.settle(number, slug)
            return None
        os.rename(target / "wiki", destination)
        path.unlink()
        remove_tree(target)
        return state

    def parked_views(self):
        views = {(int(path.parent.name[2:]), path.stem) for path in (self.meta / "parked").glob("wp*/*.json")}
        for path in self.parked.iterdir():
            match = re.fullmatch(r"wp([0-9]+)-([a-z][a-z0-9-]*)", path.name)
            if match and path.is_dir() and not path.is_symlink():
                views.add((int(match[1]), match[2]))
        return sorted(views)

    def settle(self, number, slug, older_than=None):
        """Retain parked pages whose access did not return, then remove them."""
        target, path = self._park_path(number, slug), self._park_state_path(number, slug)
        state = self._read_view_state(path)
        if state is None and not target.exists():
            path.unlink(missing_ok=True)
            return
        if state and older_than is not None and time.time() - state.get("parked", 0) < older_than:
            return
        if (target / "wiki").is_dir():
            self.retain(number, slug, "Access to this wiki ended before these edits were published.",
                        live=target / "wiki", state=state)
        if target.exists():
            remove_tree(target)
        path.unlink(missing_ok=True)

    def withdraw(self, number, slug, park=True):
        """Remove a wiki from one person's view; held directory handles stop resolving.

        A writer's pages are parked out of reach first: an access change in
        progress (such as a promotion) restores them; otherwise they are retained.
        """
        live = self.person_root(number) / slug
        if live.exists() or live.is_symlink():
            if not live.is_symlink() and not (park and self._park(number, slug)):
                self.retain(number, slug, "Access to this wiki ended before these edits were published.")
            retired = self.staging / f"withdrawn-{number}-{slug}-{uuid.uuid4().hex}"
            os.rename(live, retired)
            remove_tree(retired)
        self._state_path(number, slug).unlink(missing_ok=True)

    def remove_person(self, number):
        for slug in self.view_wikis(number):
            self.withdraw(number, slug, park=False)
        for parked, slug in self.parked_views():
            if parked == number:
                self.settle(number, slug)
        root = self.person_root(number)
        if root.exists():
            retired = self.staging / f"person-{number}-{uuid.uuid4().hex}"
            os.rename(root, retired)
            remove_tree(retired)

    # Device accounts -----------------------------------------------------

    def publish_accounts(self, generation, people, devices):
        lines = [f"generation\t{generation}"]
        lines += [f"person\t{PERSON_GID_BASE + p['number']}\t{person_group(p['number'])}" for p in people]
        for device in devices:
            lines.append("device\t{}\t{}\t{}\t{}".format(
                DEVICE_UID_BASE + device["number"], device_account(device["number"]),
                PERSON_GID_BASE + device["person"], "active" if device["active"] else "disabled"))
        fd, temporary = tempfile.mkstemp(dir=self.control, prefix=".accounts-")
        with os.fdopen(fd, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, self.control / "accounts.tsv")

    def write_secret(self, number, password):
        path = self.control / "secrets" / device_account(number)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(password + "\n")

    def discard_secret(self, number):
        try:
            os.unlink(self.control / "secrets" / device_account(number))
        except FileNotFoundError:
            pass

    def secret_pending(self, number):
        return (self.control / "secrets" / device_account(number)).exists()

    def observed(self):
        """The agent's last applied state, or None if it has not reported recently."""
        path = self.settings.drive_observed_file or self.control / "observed.tsv"
        try:
            text = path.read_text()
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None
        result = {"generation": -1, "devices": {}, "age": age}
        for line in text.splitlines():
            fields = line.split("\t")
            if fields[0] == "generation" and len(fields) == 2 and fields[1].isdigit():
                result["generation"] = int(fields[1])
            elif fields[0] == "device" and len(fields) == 5 and re.fullmatch(r"wd[0-9]+", fields[1]):
                result["devices"][fields[1]] = {"password": fields[2] == "1", "enabled": fields[3] == "1",
                                                "connected": fields[4] == "1"}
        return result
