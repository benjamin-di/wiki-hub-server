"""An OS-held lock shared by manual and scheduled sync of one physical copy."""

import os
from contextlib import contextmanager
from pathlib import Path


class CopyBusyError(Exception):
    pass


@contextmanager
def copy_lock(path: Path):
    # Keep the inode after releasing: unlinking permits two different lock files.
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            # Windows byte-range locks can cover the first byte of an empty file.
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CopyBusyError("Another wiki sync is using this copy. Retry after it finishes.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CopyBusyError("Another wiki sync is using this copy. Retry after it finishes.") from exc
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
