"""Pre-cached local alarm playback. No network, no model: works when everything else is down."""
import os
import platform
import subprocess
import threading
import time


class LocalAlarm(object):
    def __init__(self, wav_path, cooldown_s=10.0, device=None):
        self.wav_path = wav_path
        self.cooldown_s = cooldown_s
        self.device = device  # ALSA device for aplay, e.g. "plughw:1,0"
        self.last_played = 0.0
        self.lock = threading.Lock()

    def command(self):
        if platform.system() == "Darwin":
            return ["afplay", self.wav_path]
        cmd = ["aplay", "-q"]
        if self.device:
            cmd += ["-D", self.device]
        return cmd + [self.wav_path]

    def play(self, force=False):
        with self.lock:
            if not self.wav_path or not os.path.exists(self.wav_path):
                return {"played": False, "reason": "alert wav missing: %s" % self.wav_path}
            now = time.time()
            if not force and now - self.last_played < self.cooldown_s:
                return {"played": False, "reason": "cooldown"}
            try:
                subprocess.Popen(self.command(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as err:
                return {"played": False, "reason": "player failed: %s" % err}
            self.last_played = now
            return {"played": True, "player": self.command()[0]}
