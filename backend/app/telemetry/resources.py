"""Memory of the processes that run the local AI, for the dashboard (runtime.ai_diagnostics).

Physical footprint is what Activity Monitor shows and includes Metal buffers; it comes from
macOS ``footprint`` and falls back to the resident set size elsewhere. Sampling runs in a
thread at most every ``every_s`` seconds; the dashboard shows how old the sample is.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable


def footprint_mb(pid: int) -> tuple[float | None, str]:
    try:
        out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG]B)", out) or re.search(r"Footprint:\s*([\d.]+)\s*([KMG]B)", out)
        if m:
            return round(float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)], 1), "footprint"
    except Exception:
        pass
    try:
        import psutil

        return round(psutil.Process(pid).memory_info().rss / 2**20, 1), "rss"
    except Exception:
        return None, "unavailable"


class MemorySampler:
    def __init__(self, pids: Callable[[], dict[str, int | None]], every_s: float = 15.0):
        self.pids, self.every_s = pids, every_s
        self.sample: dict = {}
        self.at: float | None = None
        self._busy = False

    def _run(self) -> None:
        try:
            out = {}
            for name, pid in self.pids().items():
                if pid:
                    mb, how = footprint_mb(pid)
                    out[name] = {"mb": mb, "measure": how}
            self.sample, self.at = out, time.monotonic()
        finally:
            self._busy = False

    def snapshot(self) -> dict:
        """The last sample (refreshed in the background when it is older than ``every_s``)."""
        if not self._busy and (self.at is None or time.monotonic() - self.at > self.every_s):
            self._busy = True
            threading.Thread(target=self._run, daemon=True, name="memory-sampler").start()
        return {"processes": self.sample, "age_s": None if self.at is None else round(time.monotonic() - self.at, 1)}
