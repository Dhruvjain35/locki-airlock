"""AIRLOCK_* configuration from an env file (KEY=VALUE, no shell expansion) + the process environment.

Environment variables win over the file. Secrets are never logged: the token is only read from a
chmod-600 file (or, discouraged, AIRLOCK_EDGE_AUTH inline), and only its PUBLIC identity is shown.

Runtime labeling guard: AIRLOCK_EDGE_RUNTIME=jetson refuses to start unless /etc/nv_tegra_release
exists, and mac_dev refuses anywhere but macOS. Mac capture can't be published as Jetson evidence.

Python 3.6 compatible on purpose.
"""
import json
import os
import platform
import stat

from .contract import Contract, DEVICE_ID
from .observers import parse_zones
from .stdb import resolve_server, token_identity

DEFAULTS = {
    "AIRLOCK_EDGE_RUNTIME": "",
    "AIRLOCK_CAMERA_SOURCE": "0",
    "AIRLOCK_DEVICE_ID": "",
    "AIRLOCK_WORKSTATION_ID": "",
    "AIRLOCK_SESSION_ID": "auto",
    "AIRLOCK_SESSION_POLL_S": "2",
    "AIRLOCK_DATABASE": "locki-w91bw",
    "AIRLOCK_SERVER": "maincloud",
    "AIRLOCK_EDGE_AUTH_FILE": "~/.config/locki-edge/device_token",
    "AIRLOCK_EDGE_AUTH": "",
    "AIRLOCK_ZONE_CONFIG": "",
    "AIRLOCK_ZONES": "desk_01:0.25,0.35,0.75,1.0",
    "AIRLOCK_OBSERVER": "baseline",
    "AIRLOCK_OBSERVER_PLUGIN": "",
    "AIRLOCK_OBSERVER_PATH": "",
    "AIRLOCK_OBSERVER_KIND_MAP": "",
    "AIRLOCK_LOCAL_ALARM": "off",
    "AIRLOCK_ALARM_WAV": "",
    "AIRLOCK_ALARM_DEVICE": "",
    "AIRLOCK_CONTRACT_FILE": "",
    "AIRLOCK_STATE_DIR": "~/.local/state/locki-edge",
    "AIRLOCK_API_HOST": "127.0.0.1",
    "AIRLOCK_API_PORT": "8765",
    "AIRLOCK_API_TOKEN": "",
    "AIRLOCK_PUBLISH": "auto",
    "AIRLOCK_FORBIDDEN_IDENTITIES": "",
    "AIRLOCK_HEALTH_INTERVAL_S": "5",   # module marks a device offline after 15 s without a heartbeat
    "AIRLOCK_WIDTH": "640",
    "AIRLOCK_HEIGHT": "480",
    "AIRLOCK_FPS": "",
    "AIRLOCK_ENTER_FRAMES": "3",
    "AIRLOCK_EXIT_S": "2.0",
    "AIRLOCK_COOLDOWN_S": "10",
    "AIRLOCK_UNAVAILABLE_AFTER_S": "2.0",
    "AIRLOCK_RESTART_ON_STALL_S": "0",
    "AIRLOCK_MAX_PENDING": "1000",
    "AIRLOCK_MAX_EVENT_AGE_S": "900",
    "AIRLOCK_DETECTNET_THRESHOLD": "0.5",
    "AIRLOCK_DETECTNET_MODEL": "ssd-mobilenet-v2",
    "AIRLOCK_PERSON_THRESHOLD": "0.5",
}


class ConfigError(Exception):
    pass


def norm_identity(value):
    """SQL views print identities as 0x-prefixed hex; the JWT hex_identity claim has no prefix."""
    s = str(value).strip().lower()
    return s[2:] if s.startswith("0x") else s


def parse_env_file(path):
    out = {}
    with open(os.path.expanduser(path)) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, sep, value = line.partition("=")
            if not sep or not key.strip().replace("_", "").isalnum():
                raise ConfigError("%s:%d: expected KEY=VALUE" % (path, n))
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key.strip()] = value
    return out


def detect_platform(root=""):
    if os.path.exists(root + "/etc/nv_tegra_release"):
        return "jetson"
    if platform.system() == "Darwin":
        return "macos"
    return platform.system().lower() or "unknown"


def read_token_file(path):
    path = os.path.expanduser(path)
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError("token file %s is readable by others (mode %o): chmod 600 it" % (path, st.st_mode & 0o777))
    with open(path) as f:
        token = f.read().strip()
    if not token:
        raise ConfigError("token file %s is empty" % path)
    return token


class EdgeConfig(object):
    def __init__(self, values, platform_name=None):
        v = dict(DEFAULTS)
        v.update({k: s for k, s in values.items() if k.startswith("AIRLOCK_")})
        self.raw = v
        self.platform = platform_name or detect_platform()
        self.runtime = v["AIRLOCK_EDGE_RUNTIME"].strip()
        if self.runtime not in ("mac_dev", "jetson"):
            raise ConfigError("AIRLOCK_EDGE_RUNTIME must be mac_dev or jetson")
        if self.runtime == "jetson" and self.platform != "jetson":
            raise ConfigError("refusing AIRLOCK_EDGE_RUNTIME=jetson on %s: no /etc/nv_tegra_release. "
                              "Mac capture must never be labeled as Jetson evidence" % self.platform)
        if self.runtime == "mac_dev" and self.platform != "macos":
            raise ConfigError("AIRLOCK_EDGE_RUNTIME=mac_dev only runs on macOS (this is %s)" % self.platform)
        self.camera_source = v["AIRLOCK_CAMERA_SOURCE"].strip()
        self.device_id = v["AIRLOCK_DEVICE_ID"].strip()
        self.workstation_id = v["AIRLOCK_WORKSTATION_ID"].strip()
        self.session_id = v["AIRLOCK_SESSION_ID"].strip()
        # "auto" (default): follow the workstation's live armed session (the module rejects anything else).
        # Any other value, including empty, is sent as-is (static).
        self.session_auto = self.session_id == "auto"
        self.session_poll_s = float(v["AIRLOCK_SESSION_POLL_S"])
        self.database = v["AIRLOCK_DATABASE"].strip()
        self.server = resolve_server(v["AIRLOCK_SERVER"])
        self.state_dir = os.path.expanduser(v["AIRLOCK_STATE_DIR"])
        self.observer = v["AIRLOCK_OBSERVER"].strip()
        if self.observer not in ("baseline", "detectnet", "apple_vision", "dhruv_cv", "fixture"):
            raise ConfigError("AIRLOCK_OBSERVER must be baseline|detectnet|apple_vision|dhruv_cv|fixture")
        if self.observer == "apple_vision" and self.runtime != "mac_dev":
            raise ConfigError("AIRLOCK_OBSERVER=apple_vision is a Mac dev baseline: needs AIRLOCK_EDGE_RUNTIME=mac_dev")
        self.observer_plugin = v["AIRLOCK_OBSERVER_PLUGIN"].strip()
        self.observer_path = os.path.expanduser(v["AIRLOCK_OBSERVER_PATH"].strip()) or None
        self.kind_map = json.loads(v["AIRLOCK_OBSERVER_KIND_MAP"]) if v["AIRLOCK_OBSERVER_KIND_MAP"].strip() else {}
        if self.observer == "dhruv_cv" and not self.observer_plugin:
            raise ConfigError("AIRLOCK_OBSERVER=dhruv_cv needs AIRLOCK_OBSERVER_PLUGIN=package.module:factory")
        self.local_alarm = v["AIRLOCK_LOCAL_ALARM"].strip().lower() == "on"
        self.alarm_wav = v["AIRLOCK_ALARM_WAV"].strip() or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "alert.wav")
        self.alarm_device = v["AIRLOCK_ALARM_DEVICE"].strip() or None
        self.api_host = v["AIRLOCK_API_HOST"].strip()
        self.api_port = int(v["AIRLOCK_API_PORT"])
        self.api_token = v["AIRLOCK_API_TOKEN"].strip() or None
        self.health_interval_s = float(v["AIRLOCK_HEALTH_INTERVAL_S"])
        self.width, self.height = int(v["AIRLOCK_WIDTH"]), int(v["AIRLOCK_HEIGHT"])
        self.fps = float(v["AIRLOCK_FPS"]) if v["AIRLOCK_FPS"].strip() else None
        self.enter_frames = int(v["AIRLOCK_ENTER_FRAMES"])
        self.exit_s = float(v["AIRLOCK_EXIT_S"])
        self.cooldown_s = float(v["AIRLOCK_COOLDOWN_S"])
        self.unavailable_after_s = float(v["AIRLOCK_UNAVAILABLE_AFTER_S"])
        self.restart_on_stall_s = float(v["AIRLOCK_RESTART_ON_STALL_S"])
        self.max_pending = int(v["AIRLOCK_MAX_PENDING"])
        self.max_event_age_s = float(v["AIRLOCK_MAX_EVENT_AGE_S"])
        self.detectnet_threshold = float(v["AIRLOCK_DETECTNET_THRESHOLD"])
        self.detectnet_model = v["AIRLOCK_DETECTNET_MODEL"].strip()
        self.person_threshold = float(v["AIRLOCK_PERSON_THRESHOLD"])
        self.contract = (Contract.from_file(os.path.expanduser(v["AIRLOCK_CONTRACT_FILE"]))
                         if v["AIRLOCK_CONTRACT_FILE"].strip() else Contract())
        self.zones = self._zones(v)
        self.forbidden_identities = frozenset(
            norm_identity(x) for x in v["AIRLOCK_FORBIDDEN_IDENTITIES"].split(",") if x.strip())
        self.token, self.identity, self.publish, self.publish_reason = self._auth(v)

    @staticmethod
    def _zones(v):
        if v["AIRLOCK_ZONE_CONFIG"].strip():
            with open(os.path.expanduser(v["AIRLOCK_ZONE_CONFIG"])) as f:
                return parse_zones(json.load(f)["zones"])
        raw = []
        for part in v["AIRLOCK_ZONES"].split(";"):
            part = part.strip()
            if part:
                zid, _, rect = part.partition(":")
                raw.append({"id": zid.strip(), "rect": [float(x) for x in rect.split(",")]})
        return parse_zones(raw)

    def _auth(self, v):
        mode = v["AIRLOCK_PUBLISH"].strip().lower()
        if mode not in ("auto", "on", "off"):
            raise ConfigError("AIRLOCK_PUBLISH must be auto|on|off")
        if mode == "off":
            return None, None, False, "AIRLOCK_PUBLISH=off"
        missing = [k for k, val in (("AIRLOCK_DEVICE_ID", self.device_id),
                                    ("AIRLOCK_WORKSTATION_ID", self.workstation_id),
                                    ("AIRLOCK_DATABASE", self.database)) if not val]
        token = None
        if v["AIRLOCK_EDGE_AUTH"].strip():
            token = v["AIRLOCK_EDGE_AUTH"].strip()
        else:
            path = os.path.expanduser(v["AIRLOCK_EDGE_AUTH_FILE"].strip())
            if path and os.path.exists(path):
                token = read_token_file(path)
            else:
                missing.append("device token (%s)" % path)
        for name, value in (("AIRLOCK_DEVICE_ID", self.device_id), ("AIRLOCK_WORKSTATION_ID", self.workstation_id)):
            if value and not DEVICE_ID.match(value):
                raise ConfigError("%s=%r is not a valid id" % (name, value))
        identity = token_identity(token) if token else None
        if identity and norm_identity(identity) in self.forbidden_identities:
            raise ConfigError("token identity %s is in AIRLOCK_FORBIDDEN_IDENTITIES (owner/admin): "
                              "the edge device must use its own enrolled identity" % identity)
        if missing:
            if mode == "on":
                raise ConfigError("AIRLOCK_PUBLISH=on but missing: %s" % ", ".join(missing))
            return None, identity, False, "local audit only; missing: %s" % ", ".join(missing)
        return token, identity, True, "publishing to %s/%s" % (self.server, self.database)

    def public(self):
        """Config as shown on /health: never includes the token."""
        return {"runtime": self.runtime, "platform": self.platform, "camera_source": self.camera_source,
                "device_id": self.device_id, "workstation_id": self.workstation_id,
                "session_id": self.session_id, "server": self.server, "database": self.database,
                "observer": self.observer, "zones": [z._asdict() for z in self.zones],
                "publish": self.publish, "publish_reason": self.publish_reason, "identity": self.identity,
                "local_alarm": self.local_alarm}


def load_config(env_file=None, environ=None, platform_name=None):
    values = {}
    if env_file:
        values.update(parse_env_file(env_file))
    values.update({k: s for k, s in (os.environ if environ is None else environ).items() if k.startswith("AIRLOCK_")})
    return EdgeConfig(values, platform_name=platform_name)
