"""One user of a device at a time, across processes: the AIRLOCK camera and the macOS USB monitor.

A demo or an armed session in the backend holds the lock while it uses the device; a script (the bench, the zone
tool, the watcher) that finds it held refuses with the holder's pid instead of opening the device a second time.
The operating system drops the lock when the holding process exits, so a crash never leaves it stuck.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class DeviceBusy(RuntimeError):
    pass


class DeviceLock:
    def __init__(self, path: Path, what: str):
        self.path = Path(path)
        self.what = what
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                holder = os.read(fd, 32).decode(errors="ignore").strip() or "another process"
            finally:
                os.close(fd)
            raise DeviceBusy(f"the {self.what} is already in use by pid {holder}; stop that first") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> DeviceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def camera_lock(data_dir: Path) -> DeviceLock:
    return DeviceLock(Path(data_dir) / "airlock_vision" / "camera.lock", "AIRLOCK camera")


def usb_monitor_lock(data_dir: Path) -> DeviceLock:
    return DeviceLock(Path(data_dir) / "security" / "usb-monitor.lock", "macOS USB monitor")
