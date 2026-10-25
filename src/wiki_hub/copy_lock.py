"""OS-held file locks for copies and shared credential transactions."""

import os
import time
from contextlib import contextmanager
from pathlib import Path


class CopyBusyError(Exception):
    pass


class FileLockTimeoutError(Exception):
    pass


@contextmanager
def file_lock(path: Path, *, timeout: float = 0):
    # Keep the inode after releasing: unlinking permits two different lock files.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    # Byte-range locks can cover the first byte of an empty file.
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if os.name != "nt" and not isinstance(exc, BlockingIOError):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockTimeoutError("Another operation holds this file lock.") from exc
                time.sleep(min(0.05, remaining))
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def copy_lock(path: Path):
    try:
        with file_lock(path):
            yield
    except FileLockTimeoutError as exc:
        raise CopyBusyError("Another wiki sync is using this copy. Retry after it finishes.") from exc
