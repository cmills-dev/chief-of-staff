"""One local writer per operation, across threads and processes."""

import contextlib
import os
import threading
from pathlib import Path

_LOCKS = {}
_GUARD = threading.Lock()


class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def file_lock(path, *, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _GUARD:
        lock = _LOCKS.setdefault(str(path.resolve()), threading.Lock())
    if not lock.acquire(blocking=blocking):
        raise Busy("Another operation holds " + str(path))
    handle = None
    held = False
    try:
        if path.is_symlink():
            raise ValueError("Lock path is a symlink")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(fd, "r+b")
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(
                    handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1
                )
            except OSError as exc:
                raise Busy(str(path)) from exc
        else:
            import fcntl

            try:
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                )
            except BlockingIOError as exc:
                raise Busy(str(path)) from exc
        held = True
        yield
    finally:
        if handle:
            if held:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()
