from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import BinaryIO


class ProfileOwnerError(RuntimeError):
    pass


class ProfileOwnerLock:
    """Cross-process advisory lock held for a persistent profile's lifetime."""

    def __init__(self, profile_dir: Path) -> None:
        self.path = profile_dir.parent / f".{profile_dir.name}.owner.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise ProfileOwnerError(
                f"浏览器 Profile 已由另一个进程持有：{self.path.parent.name}/{self.path.name}"
            ) from exc
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProfileOwnerLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


_PROCESS_LOCKS: list[ProfileOwnerLock] = []


def hold_profile_lock_for_process(profile_dir: Path) -> ProfileOwnerLock:
    """Hold a profile lock until an isolated CLI process exits."""
    lock = ProfileOwnerLock(profile_dir)
    lock.acquire()
    _PROCESS_LOCKS.append(lock)
    atexit.register(lock.release)
    return lock
