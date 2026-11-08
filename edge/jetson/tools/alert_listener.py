"""LOCKI alert listener for the owner's Mac: shows the SAME ntfy messages the owner's phone gets, nothing more.

    AIRLOCK_ALERT_PHONE_CONFIG=~/locked-in/data/notify/phone.json \\
    AIRLOCK_PUSH_URL_FILE=~/.config/locki-edge/push_url \\
        python3 edge/jetson/tools/alert_listener.py run            # or: status | ack <key> | show <level> | --dry-run

One genuine incident gives one consistent owner alert. Incident and alarm alerts come ONLY from the M4 backend's
security dispatch (Dhruv's backend/app/security/dispatch.py: one ntfy message per incident per severity, using
SpacetimeDB's own severity). The Jetson only ever sends presence. This listener invents no level of its own.

LEVELS (source -> presentation)
  presence  jetson-edge: a person_in_zone SpacetimeDB accepted. An observation, not an incident.  Banner only.
  elevated  RESERVED for verified interaction with protected equipment. No producer exists yet, so nothing is
            classified here today. If one appears it gets a banner plus a dialog, with no sound.
  incident  m4-dispatch "LOCKI Medium incident" (also Low/Info if ever sent).                 Banner + dialog.
  alarm     m4-dispatch "LOCKI High/Critical incident".                Banner + dialog + the alert sound, looped.
  test      m4-dispatch "LOCKI test notification" (backend scripts/airlock_notify.py test).    Banner only.
  unknown   anything else on these topics.                         Banner only, marked "unverified source".

CONFIG comes from the environment and files only, never from the command line (argv shows up in ps and history):
  AIRLOCK_ALERT_PHONE_CONFIG   Dhruv's backend data/notify/phone.json (incidents, test). Must be chmod 600.
  AIRLOCK_PUSH_URL_FILE        chmod-600 file with the Jetson presence URL https://<server>/<topic>
  AIRLOCK_PUSH_URL             the same URL inline (not both). At least one source is required.
  AIRLOCK_ALERT_STATE_DIR      default ~/.local/state/locki-alert (0700): state.json + ledger.jsonl (0600)
  AIRLOCK_ALERT_MAX_AGE_S 300  AIRLOCK_ALERT_PRESENCE_COOLDOWN_S 300  AIRLOCK_ALERT_DIALOG_S 120
  AIRLOCK_ALERT_MAX_RING_S 30  AIRLOCK_ALERT_SPEAK 0  AIRLOCK_ALERT_SOUND (default: your macOS alert sound)
  AIRLOCK_ALERT_CONSOLE_URL    default http://127.0.0.1:5174/?product=airlock (loopback http or https only)

RULES
- The topic is the secret. It is never printed or logged; a source is shown as "ntfy …<last4>".
- One stream per ntfy server: <server>/<topic>[,<topic2>]/json?since=... (Bearer token from phone.json if set).
  First start: since=<now>, so no backlog. After that: since=<last ntfy id>. A message older than
  AIRLOCK_ALERT_MAX_AGE_S is "stale" and never shown. Keepalive/open events are ignored. Reconnects back off.
- Dedupe survives restarts: the ntfy id plus a semantic key (incident:<suffix>#<severity>, event:<uuid>).
  Presence from one workstation shows at most once per AIRLOCK_ALERT_PRESENCE_COOLDOWN_S ("suppressed").
- The ledger has one JSON line per transition: received, duplicate, stale, suppressed, displayed, display_failed,
  acknowledged, opened_console, not_acknowledged. "displayed" means handed to Notification Center; Focus or
  notification settings may still hide it. Nothing here can tell whether a person noticed it.
- Volume is never changed. Sounds play at the current output volume; banners follow your notification settings.
- Dialog buttons Later / Open console / Acknowledge: Return = Acknowledge, Esc = Later, and VoiceOver and
  keyboard navigation reach every button. Acknowledging here is local to this Mac. The SpacetimeDB
  acknowledgement is done in the LOCKI console (owner) or the Security Desk.
- Only outbound HTTPS to the ntfy server. No ports are opened; the M4 backend is never called. Nothing is sent
  anywhere, and no text here claims that anyone outside this Mac and the owner's phone was told anything.
- No frames, images or biometrics travel over ntfy: only the short text the producers send.
Stdlib only. Runs on macOS /usr/bin/python3 (3.9) and 3.12; written to the edge's Python 3.6 syntax floor.
"""
import argparse
import json
import os
import platform
import plistlib
import queue
import random
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

IS_MAC = platform.system() == "Darwin"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LEVELS = OrderedDict([   # level -> (banner, dialog, sound)
    ("presence", (True, False, False)),
    ("elevated", (True, True, False)),    # reserved: no producer yet
    ("incident", (True, True, False)),
    ("alarm", (True, True, True)),
    ("test", (True, False, False)),
    ("unknown", (True, False, False)),
])
SEVERITY_LEVEL = {"info": "incident", "low": "incident", "medium": "incident", "high": "alarm", "critical": "alarm"}
STATES = ("received", "duplicate", "stale", "suppressed", "displayed", "display_failed", "acknowledged",
          "opened_console", "not_acknowledged")
DISPATCH_TITLE = re.compile(u"^LOCKI (Info|Low|Medium|High|Critical) incident \u00b7 (.{1,120})$")
DISPATCH_BODY = re.compile(u"^(\\d{2}:\\d{2}:\\d{2}) \u00b7 (.*?) ?Incident (\\S{1,12})\\. "
                           u"Review and acknowledge it in the LOCKI console\\.\\s*$", re.S)
PRESENCE_TITLE = re.compile(u"^LOCKI presence \u00b7 (.{1,120})$")
PRESENCE_WHERE = re.compile(u"Person observed in (.{1,64}?) at (\\d{2}:\\d{2}:\\d{2}Z)\\.")
PRESENCE_EVENT = re.compile(u"Event ([0-9A-Za-z-]{8,64}) \u00b7 device ([A-Za-z0-9_.:-]{1,64})\\.")
TEST_TITLE = "LOCKI test notification"
LOCAL_ACK_NOTE = ("Acknowledging here is local to this Mac. The SpacetimeDB acknowledgement is done in the "
                  "LOCKI console.")
DIALOG_BUTTONS = '{"Later", "Open console", "Acknowledge"}'
KEY_LIMIT = 2000
LEDGER_MAX_BYTES = 5000000
DEFAULT_CONSOLE_URL = "http://127.0.0.1:5174/?product=airlock"
FALLBACK_SOUND = "/System/Library/Sounds/Sosumi.aiff"


class ConfigError(Exception):
    """Unusable configuration. Messages never contain a URL or a topic."""


# ---------------------------------------------------------------------------------------------- config
def _secret_file(path, what):
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except OSError:
        raise ConfigError("%s %s does not exist" % (what, path))
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError("%s %s is readable by other users (mode %o): chmod 600 it" % (what, path, st.st_mode & 0o777))
    with open(path) as f:
        return f.read()


def _check_server(server, what):
    try:
        parts = urlsplit(server)
        host = parts.hostname
    except ValueError:
        raise ConfigError("%s: not a valid server URL" % what)
    if not (parts.scheme == "https" or (parts.scheme == "http" and host in ("127.0.0.1", "localhost"))):
        raise ConfigError("%s: the ntfy server must use https:// (http:// only for 127.0.0.1 or localhost)" % what)
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("%s: no credentials, query or fragment allowed in the server URL" % what)
    return host


def parse_ntfy_url(url, what="the presence URL"):
    """https://<server>/<topic> -> (server, topic)."""
    url = (url or "").strip()
    _check_server(url, what)
    parts = urlsplit(url)
    prefix, _, topic = parts.path.rstrip("/").rpartition("/")
    if not TOPIC_RE.match(topic):
        raise ConfigError("%s must end in an ntfy topic (letters, digits, '-' and '_')" % what)
    return "%s://%s%s" % (parts.scheme, parts.netloc, prefix), topic


def load_phone_config(path):
    """Dhruv's data/notify/phone.json: {"provider":"ntfy","server":...,"topic":...,"token"?}, mode 600."""
    try:
        cfg = json.loads(_secret_file(path, "AIRLOCK_ALERT_PHONE_CONFIG"))
        if cfg.get("provider") != "ntfy":
            raise ConfigError("AIRLOCK_ALERT_PHONE_CONFIG: only provider ntfy is supported")
        topic, server = str(cfg["topic"]), str(cfg.get("server") or "https://ntfy.sh").rstrip("/")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ConfigError("AIRLOCK_ALERT_PHONE_CONFIG is not a valid phone channel (%s)" % type(exc).__name__)
    if not TOPIC_RE.match(topic):
        raise ConfigError("AIRLOCK_ALERT_PHONE_CONFIG: the topic is not a valid ntfy topic")
    _check_server(server, "AIRLOCK_ALERT_PHONE_CONFIG")
    return {"role": "incidents", "server": server, "topic": topic, "token": cfg.get("token") or None}


def load_sources(environ):
    sources = []
    phone = environ.get("AIRLOCK_ALERT_PHONE_CONFIG", "").strip()
    if phone:
        sources.append(load_phone_config(phone))
    inline, path = environ.get("AIRLOCK_PUSH_URL", "").strip(), environ.get("AIRLOCK_PUSH_URL_FILE", "").strip()
    if inline and path:
        raise ConfigError("set AIRLOCK_PUSH_URL or AIRLOCK_PUSH_URL_FILE, not both")
    url = _secret_file(path, "AIRLOCK_PUSH_URL_FILE").strip() if path else inline
    if path and not url:
        raise ConfigError("AIRLOCK_PUSH_URL_FILE is empty")
    if url:
        server, topic = parse_ntfy_url(url, "AIRLOCK_PUSH_URL_FILE" if path else "AIRLOCK_PUSH_URL")
        sources.append({"role": "presence", "server": server, "topic": topic, "token": None})
    if not sources:
        raise ConfigError("no source: set AIRLOCK_ALERT_PHONE_CONFIG and/or AIRLOCK_PUSH_URL_FILE (or AIRLOCK_PUSH_URL)")
    return sources


def check_console_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        host, parts = None, None
    if parts is None or not host or not (parts.scheme == "https" or
                                         (parts.scheme == "http" and host in ("127.0.0.1", "localhost"))):
        raise ConfigError("AIRLOCK_ALERT_CONSOLE_URL must be http://127.0.0.1, http://localhost or https://")
    return url


def resolve_alert_sound():
    """The user's own macOS alert sound (System Settings > Sound), falling back to Sosumi."""
    try:
        with open(os.path.expanduser("~/Library/Preferences/.GlobalPreferences.plist"), "rb") as f:
            path = plistlib.load(f).get("com.apple.sound.beep.sound")
        if path and os.path.isfile(path):
            return path
    except Exception:  # noqa: BLE001 - any unreadable preference falls back
        pass
    return FALLBACK_SOUND


class Settings(object):
    def __init__(self, environ):
        def num(name, default, cast=float):
            try:
                value = cast(environ.get(name, "").strip() or default)
            except ValueError:
                raise ConfigError("%s must be a number" % name)
            if value < 0:
                raise ConfigError("%s must be >= 0" % name)
            return value
        self.state_dir = os.path.expanduser(environ.get("AIRLOCK_ALERT_STATE_DIR", "").strip()
                                            or "~/.local/state/locki-alert")
        self.max_age_s = num("AIRLOCK_ALERT_MAX_AGE_S", 300)
        self.presence_cooldown_s = num("AIRLOCK_ALERT_PRESENCE_COOLDOWN_S", 300)
        self.dialog_s = num("AIRLOCK_ALERT_DIALOG_S", 120, int)
        self.max_ring_s = num("AIRLOCK_ALERT_MAX_RING_S", 30)
        self.speak = environ.get("AIRLOCK_ALERT_SPEAK", "").strip() == "1"
        self.console_url = check_console_url(environ.get("AIRLOCK_ALERT_CONSOLE_URL", "").strip()
                                             or DEFAULT_CONSOLE_URL)
        self.sound = environ.get("AIRLOCK_ALERT_SOUND", "").strip() or None

    def public(self):
        return {"state_dir": self.state_dir, "max_age_s": self.max_age_s,
                "presence_cooldown_s": self.presence_cooldown_s, "dialog_s": self.dialog_s,
                "max_ring_s": self.max_ring_s, "speak": self.speak, "console_url": self.console_url}


class Connection(object):
    """One ntfy stream: every configured topic on one server (and one token) shares it."""

    def __init__(self, server, token):
        self.server, self.token, self.topics, self.roles = server, token, [], {}

    def add(self, src):
        if src["topic"] not in self.roles:
            self.topics.append(src["topic"])
            self.roles[src["topic"]] = set()
        self.roles[src["topic"]].add(src["role"])

    @property
    def host(self):
        return urlsplit(self.server).hostname

    @property
    def label(self):   # the cursor key: no topic-derived data
        return "%s@%s" % ("+".join(sorted(set(r for rs in self.roles.values() for r in rs))), self.host)

    def roles_for(self, topic):
        if topic in self.roles:
            return self.roles[topic]
        return set(next(iter(self.roles.values()))) if len(self.roles) == 1 else set()

    def describe(self):
        return {"server": self.host, "token": bool(self.token),
                "sources": [{"role": "+".join(sorted(self.roles[t])), "destination": u"ntfy \u2026%s" % t[-4:]}
                            for t in self.topics]}


def group_sources(sources):
    conns = OrderedDict()
    for src in sources:
        key = (src["server"], src["token"])
        conns.setdefault(key, Connection(src["server"], src["token"])).add(src)
    return list(conns.values())


# ---------------------------------------------------------------------------------------------- classify
class Alert(object):
    __slots__ = ("ntfy_id", "ntfy_time", "level", "severity", "source", "workstation", "incident_suffix",
                 "event_id", "zone", "clock", "summary", "raw_title", "raw_body", "note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def semantic_key(self):
        if self.level in ("incident", "alarm") and self.incident_suffix and self.severity:
            return "incident:%s#%s" % (self.incident_suffix, self.severity)
        if self.level == "presence" and self.event_id:
            return "event:%s" % self.event_id
        return None

    @property
    def key(self):
        return self.semantic_key or "ntfy:%s" % self.ntfy_id


def classify(ev, roles):
    """ntfy message event + the roles of the topic it came on -> Alert. Formats are matched exactly; a known
    format on a topic that does not carry that role is "unknown" (unverified source)."""
    title = str(ev.get("title") or "")[:200]
    body = str(ev.get("message") or "")[:600]
    tags = [str(t) for t in (ev.get("tags") or []) if isinstance(t, (str, bytes))]
    base = dict(ntfy_id=str(ev.get("id") or ""), ntfy_time=ev.get("time"), raw_title=title, raw_body=body)
    m = DISPATCH_TITLE.match(title)
    if m and "incidents" in roles:
        severity = m.group(1).lower()
        b = DISPATCH_BODY.match(body)
        return Alert(level=SEVERITY_LEVEL[severity], severity=severity, source="m4-dispatch",
                     workstation=m.group(2).strip(), incident_suffix=b.group(3) if b else None,
                     clock=b.group(1) if b else None, summary=(b.group(2).strip() if b else None), **base)
    if (PRESENCE_TITLE.match(title) or "locki-presence" in tags) and "presence" in roles:
        t, where, evt = PRESENCE_TITLE.match(title), PRESENCE_WHERE.search(body), PRESENCE_EVENT.search(body)
        return Alert(level="presence", source="jetson-edge", workstation=t.group(1).strip() if t else "unknown",
                     zone=where.group(1) if where else None, clock=where.group(2) if where else None,
                     event_id=evt.group(1) if evt else None,
                     note="simulated rehearsal" if body.startswith("Simulated rehearsal.") else None, **base)
    if title == TEST_TITLE:
        return Alert(level="test", source="m4-dispatch" if "incidents" in roles else "unknown", **base)
    return Alert(level="unknown", source="unknown",
                 note="known format on the wrong topic" if (m or PRESENCE_TITLE.match(title)) else None, **base)


# ---------------------------------------------------------------------------------------------- state
def _atomic_write(path, text):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")   # mkstemp creates it 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_state_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path, 0o700)
    os.chmod(path, 0o700)
    return path


class StateStore(object):
    """state.json: cursors per stream, known keys (bounded), last presence per workstation."""

    def __init__(self, state_dir):
        self.path = os.path.join(ensure_state_dir(state_dir), "state.json")
        self.cursors, self.keys, self.presence_last = {}, OrderedDict(), {}
        try:
            with open(self.path) as f:
                raw = json.load(f)
            self.cursors = dict(raw.get("cursors") or {})
            self.keys = OrderedDict((k, t) for k, t in raw.get("keys") or [])
            self.presence_last = dict(raw.get("presence_last") or {})
        except (OSError, ValueError, TypeError):
            pass

    def known(self, key):
        return key in self.keys

    def remember(self, key, now):
        self.keys[key] = now
        self.keys.move_to_end(key)
        while len(self.keys) > KEY_LIMIT:
            self.keys.popitem(last=False)

    def save(self):
        _atomic_write(self.path, json.dumps({"version": 1, "cursors": self.cursors, "keys": list(self.keys.items()),
                                             "presence_last": self.presence_last}, sort_keys=True))


class Ledger(object):
    """ledger.jsonl: one line per transition. Parsed fields only: never a topic, never a full body."""

    def __init__(self, state_dir):
        self.path = os.path.join(ensure_state_dir(state_dir), "ledger.jsonl")
        self.lock = threading.Lock()

    def write(self, alert, state, detail=None, now=None):
        assert state in STATES, state
        rec = OrderedDict([("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time()))),
                           ("key", alert.key), ("ntfy_id", alert.ntfy_id), ("level", alert.level),
                           ("severity", alert.severity), ("source", alert.source),
                           ("workstation", alert.workstation), ("incident_suffix", alert.incident_suffix),
                           ("event_id", alert.event_id), ("state", state)])
        if detail:
            rec["detail"] = detail
        with self.lock:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LEDGER_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec

    def entries(self):
        out = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out


# ---------------------------------------------------------------------------------------------- display
def run_cmd(cmd, timeout=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", type(e).__name__


def osascript(lines, args):
    """Strings reach AppleScript only as argv data, never spliced into the script."""
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    return cmd + [str(a) for a in args]


NOTIFY_SCRIPT = ("on run argv",
                 "display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)",
                 "end run")


def dialog_script(level):
    style = "critical" if level == "alarm" else "warning"
    return ("on run argv",
            "set r to display alert (item 1 of argv) message (item 2 of argv) as %s buttons %s "
            "default button \"Acknowledge\" cancel button \"Later\" giving up after ((item 3 of argv) as integer)"
            % (style, DIALOG_BUTTONS),
            "return (button returned of r) & \"|\" & ((gave up of r) as text)",
            "end run")


def banner_text(a):
    """-> (title, subtitle, text). Known formats are rebuilt from parsed fields; unknown text is marked."""
    if a.level == "presence":
        sim = " (simulated rehearsal)" if a.note else ""
        return (u"LOCKI presence \u00b7 %s%s" % (a.workstation, sim), "Observation only, not an incident",
                u"Person observed in %s at %s. Event %s" % (a.zone or "the desk zone", a.clock or "an unknown time",
                                                          a.event_id or "id unknown"))
    if a.level in ("incident", "alarm"):
        return (u"LOCKI %s incident \u00b7 %s" % (a.severity.capitalize(), a.workstation),
                u"Incident \u2026%s" % (a.incident_suffix or "id not parsed"),
                u"%s \u00b7 %s" % (a.clock or "--:--:--", a.summary or "Review it in the LOCKI console."))
    if a.level == "elevated":
        return ("LOCKI interaction at protected equipment", "Local observation, not an incident",
                (a.raw_body or "")[:200] or "(no text)")
    if a.level == "test":
        return (TEST_TITLE, "Test only, no incident", (a.raw_body or "")[:200] or "Test notification.")
    return ("LOCKI listener: unverified source", (a.raw_title or "")[:80] or "(no title)",
            (a.raw_body or "")[:200] or "(no text)")


def dialog_text(a):
    title, subtitle, text = banner_text(a)
    lines = [text, "", "%s at %s." % (subtitle, a.workstation)] if a.level != "elevated" else [text]
    return title, "\n".join(lines + ["", LOCAL_ACK_NOTE])


def speech_text(a):
    return "LOCKI %s incident at %s. Check the LOCKI console." % (a.severity or "high", a.workstation)


class Presenter(object):
    """Shows alerts on a worker so the stream never blocks. Dialogs run on their own threads."""

    def __init__(self, settings, ledger=None, runner=run_cmd, spawner=None, dry_run=False, out=None,
                 sync=False):
        self.s, self.ledger, self.runner, self.dry_run, self.sync = settings, ledger, runner, dry_run, sync
        self.spawner = spawner or (lambda cmd: subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                                                stderr=subprocess.DEVNULL))
        self.out = out or (lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))
        self.sound = settings.sound or (resolve_alert_sound() if IS_MAC else FALLBACK_SOUND)
        self.ring_lock = threading.Lock()
        self.queue = queue.Queue()
        self.results = []   # (key, state) for tests and `show`
        if not sync:
            t = threading.Thread(target=self._run, name="presenter")
            t.daemon = True
            t.start()

    def submit(self, alert):
        if self.sync:
            self.present(alert)
        else:
            self.queue.put(alert)

    def _run(self):
        while True:
            self._guarded(self.present, self.queue.get())

    def _guarded(self, fn, alert):
        """Worker/dialog threads must survive any failure, and the ledger must say it failed."""
        try:
            fn(alert)
        except Exception as e:  # noqa: BLE001
            try:
                self._record(alert, "display_failed", type(e).__name__)
            except Exception:  # noqa: BLE001 - e.g. the ledger itself is unwritable
                pass

    def _record(self, alert, state, detail=None):
        self.results.append((alert.key, state))
        if self.ledger is not None:
            self.ledger.write(alert, state, detail)
        self.out("[%s] %s %s %s ws=%s key=%s%s" % (time.strftime("%H:%M:%S"), state, alert.level, alert.source,
                                                  alert.workstation or "-", alert.key,
                                                  " (%s)" % detail if detail else ""))

    def present(self, alert):
        banner, dialog, sound = LEVELS.get(alert.level, LEVELS["unknown"])
        title, subtitle, text = banner_text(alert)
        if self.dry_run or not IS_MAC and self.runner is run_cmd:
            self.out("[dry-run] would show a banner: %s | %s | %s%s" % (
                title, subtitle, text, " + a dialog" + (" + the alert sound" if sound else "") if dialog else ""))
            return
        if banner:
            rc, _out, err = self.runner(osascript(NOTIFY_SCRIPT, (title, subtitle, text)), 15)
            self._record(alert, "displayed" if rc == 0 else "display_failed",
                         None if rc == 0 else "banner rc %d" % rc)
        if dialog:
            if self.sync:
                self.dialog(alert)
            else:
                t = threading.Thread(target=self._guarded, args=(self.dialog, alert), name="dialog")
                t.daemon = True
                t.start()

    def dialog(self, alert):
        stop = threading.Event()
        ring = None
        if LEVELS.get(alert.level, LEVELS["unknown"])[2]:   # only alarm has a sound
            if self.s.speak:
                self._spawn(["say", speech_text(alert)])
            if self.ring_lock.acquire(False):
                ring = threading.Thread(target=self._ring, args=(stop,), name="ring")
                ring.daemon = True
                ring.start()
        title, text = dialog_text(alert)
        try:
            rc, out, err = self.runner(osascript(dialog_script(alert.level), (title, text, int(self.s.dialog_s))),
                                       self.s.dialog_s + 15)
        finally:
            stop.set()
            if ring is not None:
                ring.join(timeout=5)
        state, detail = map_dialog_result(rc, out, err)
        if state == "opened_console":
            orc, _o, _e = self.runner(["open", self.s.console_url], 15)
            detail = None if orc == 0 else "open rc %d" % orc
        self._record(alert, state, detail)
        return state

    def _spawn(self, cmd):
        try:
            return self.spawner(cmd)
        except OSError:
            return None

    def _ring(self, stop):
        """The alert sound at the CURRENT output volume (afplay with no volume flag), until the dialog returns or
        max_ring_s passes."""
        try:
            deadline = time.monotonic() + self.s.max_ring_s
            while not stop.is_set() and time.monotonic() < deadline:
                p = self._spawn(["afplay", self.sound])
                if p is None:
                    return
                while p.poll() is None:
                    if stop.wait(0.1) or time.monotonic() >= deadline:
                        p.terminate()
                        return
                stop.wait(0.3)
        finally:
            self.ring_lock.release()


def map_dialog_result(rc, out, err):
    """osascript result of the dialog -> (ledger state, detail)."""
    if rc == 0:
        button, _, gave_up = (out or "").strip().partition("|")
        if gave_up.strip() == "true":
            return "not_acknowledged", "gave_up"
        if button == "Acknowledge":
            return "acknowledged", "dialog"
        if button == "Open console":
            return "opened_console", None
        if button == "Later":
            return "not_acknowledged", "later"
        return "display_failed", "unexpected dialog result"
    if "-128" in (err or ""):
        return "not_acknowledged", "later"   # Esc / the cancel button
    return "display_failed", "dialog rc %d" % rc


# ---------------------------------------------------------------------------------------------- listener
class Listener(object):
    def __init__(self, settings, sources, presenter, opener=urlopen, clock=time.time, out=None, sleep=None):
        self.s, self.presenter, self.opener, self.clock = settings, presenter, opener, clock
        self.conns = group_sources(sources)
        self.store = StateStore(settings.state_dir)
        self.ledger = presenter.ledger or Ledger(settings.state_dir)
        self.lock = threading.Lock()
        self.out = out or presenter.out
        self.sleep = sleep or time.sleep
        self.stop = threading.Event()
        now = int(self.clock())
        for c in self.conns:   # first-ever start: begin at now, so no backlog is fetched
            self.store.cursors.setdefault(c.label, {"id": None, "time": now})
        self.store.save()

    def stream_url(self, conn):
        cur = self.store.cursors.get(conn.label) or {}
        since = cur.get("id") or str(int(cur.get("time") or self.clock()))
        return "%s/%s/json?since=%s" % (conn.server, ",".join(conn.topics), quote(str(since), safe=""))

    def handle(self, conn, ev):
        """One ntfy event -> the state it ended in (None for open/keepalive)."""
        if not isinstance(ev, dict) or ev.get("event") != "message" or not ev.get("id"):
            return None
        alert = classify(ev, conn.roles_for(ev.get("topic")))
        now = self.clock()
        with self.lock:
            self.ledger.write(alert, "received", alert.note)
            nkey = "ntfy:%s" % alert.ntfy_id
            state, detail = None, None
            try:
                age = now - float(alert.ntfy_time)
            except (TypeError, ValueError):
                age = None
            if self.store.known(nkey):
                state = "duplicate"
            elif age is None or age > self.s.max_age_s:
                state, detail = "stale", "age unknown" if age is None else "age %ds" % int(age)
            elif alert.semantic_key and self.store.known(alert.semantic_key):
                state = "duplicate"
            elif alert.level == "presence" and \
                    now - float(self.store.presence_last.get(alert.workstation, -1e18)) < self.s.presence_cooldown_s:
                state, detail = "suppressed", "presence cooldown"
            self.store.remember(nkey, now)
            if state != "duplicate" and alert.semantic_key:
                self.store.remember(alert.semantic_key, now)
            if state is None and alert.level == "presence":
                self.store.presence_last[alert.workstation] = now
            self.store.cursors[conn.label] = {"id": alert.ntfy_id, "time": alert.ntfy_time}
            self.store.save()
            if state is not None:
                self.ledger.write(alert, state, detail)
                self.out("[%s] %s %s %s ws=%s key=%s" % (time.strftime("%H:%M:%S"), state, alert.level,
                                                         alert.source, alert.workstation or "-", alert.key))
                return state
        self.presenter.submit(alert)
        return "queued"

    def consume(self, conn, lines):
        for raw in lines:
            if self.stop.is_set():
                return
            try:
                ev = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            except ValueError:
                continue
            self.handle(conn, ev)

    def run_connection(self, conn, max_rounds=None):
        backoff, rounds = 1.0, 0
        while not self.stop.is_set() and (max_rounds is None or rounds < max_rounds):
            rounds += 1
            headers = {"Authorization": "Bearer %s" % conn.token} if conn.token else {}
            try:
                resp = self.opener(Request(self.stream_url(conn), headers=headers), timeout=90)
                try:
                    self.out("[%s] listening: %s" % (time.strftime("%H:%M:%S"),
                                                     json.dumps(conn.describe(), ensure_ascii=False)))
                    backoff = 1.0
                    self.consume(conn, resp)
                finally:
                    if hasattr(resp, "close"):
                        resp.close()
                why = "stream ended"
            except HTTPError as e:
                why = "HTTP %d%s" % (e.code, ": check the token in phone.json" if e.code in (401, 403) else "")
            except Exception as e:  # noqa: BLE001 - keep listening through any network failure
                why = type(e).__name__
            if self.stop.is_set() or (max_rounds is not None and rounds >= max_rounds):
                return
            wait = backoff + random.uniform(0, backoff / 2)
            self.out("[%s] %s on %s; reconnecting in %.0fs" % (time.strftime("%H:%M:%S"), why, conn.host, wait))
            self.sleep(wait)
            backoff = min(60.0, backoff * 2)

    def run_forever(self):
        threads = []
        for c in self.conns:
            t = threading.Thread(target=self.run_connection, args=(c,), name="ntfy-%s" % c.host)
            t.daemon = True
            t.start()
            threads.append(t)
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop.set()


# ---------------------------------------------------------------------------------------------- cli
SAMPLES = {
    "presence": dict(level="presence", source="jetson-edge", workstation="preview-ws", zone="desk_01",
                     clock="12:00:00Z", event_id="00000000-0000-4000-8000-000000000000"),
    "elevated": dict(level="elevated", source="unknown", workstation="preview-ws",
                     raw_body="Reserved level: no producer exists yet. This is a local preview."),
    "incident": dict(level="incident", severity="medium", source="m4-dispatch", workstation="preview-ws",
                     incident_suffix="preview00001", clock="12:00:00",
                     summary="New unapproved USB device detected while armed."),
    "alarm": dict(level="alarm", severity="high", source="m4-dispatch", workstation="preview-ws",
                  incident_suffix="preview00002", clock="12:00:00",
                  summary="Unknown USB device detected within 10 seconds of a person in the desk zone."),
    "test": dict(level="test", source="m4-dispatch", workstation="preview-ws",
                 raw_body="A local preview of the test notification. No incident."),
    "unknown": dict(level="unknown", source="unknown", raw_title="Some other message",
                    raw_body="A local preview of an unverified message."),
}


class _Parser(argparse.ArgumentParser):
    def error(self, message):   # never echo the offending values: a pasted topic must not reach the terminal
        self.print_usage(sys.stderr)
        sys.stderr.write("alert_listener: invalid arguments (values are not echoed). Sources come from the "
                         "environment only; see --help.\n")
        sys.exit(2)


def ledger_counts(entries):
    counts = OrderedDict((s, 0) for s in STATES)
    for e in entries:
        counts[e.get("state")] = counts.get(e.get("state"), 0) + 1
    return counts


def main(argv=None, environ=None, runner=run_cmd, spawner=None, opener=urlopen, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    out = out or (lambda line: (sys.stdout.write(line + "\n"), sys.stdout.flush()))
    if any("://" in a for a in argv):
        sys.stderr.write("alert_listener: URLs and topics are never accepted on the command line. Put the URL in a "
                         "chmod-600 file named by AIRLOCK_PUSH_URL_FILE, or use AIRLOCK_ALERT_PHONE_CONFIG.\n")
        return 2
    p = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="run", choices=("run", "status", "ack", "show"))
    p.add_argument("target", nargs="?", help="ack: the ledger key; show: a level")
    p.add_argument("--dry-run", action="store_true", help="no osascript/afplay/say: print what would be shown")
    a = p.parse_args(argv)
    try:
        settings = Settings(environ)
        if a.command == "show":
            if a.target not in LEVELS:
                out("show needs one of: %s" % ", ".join(LEVELS))
                return 2
            presenter = Presenter(settings, None, runner, spawner, a.dry_run, out, sync=True)
            presenter.present(Alert(ntfy_id="preview", **SAMPLES[a.target]))
            out(json.dumps({"preview": a.target, "results": presenter.results}))
            return 0
        if a.command == "ack":
            if not a.target:
                out("ack needs a ledger key (see status)")
                return 2
            ledger = Ledger(settings.state_dir)
            last = [e for e in ledger.entries() if e.get("key") == a.target]
            if not last:
                out("no ledger entry for that key")
                return 1
            e = last[-1]
            alert = Alert(ntfy_id=e.get("ntfy_id"), level=e.get("level"), severity=e.get("severity"),
                          source=e.get("source"), workstation=e.get("workstation"),
                          incident_suffix=e.get("incident_suffix"), event_id=e.get("event_id"))
            ledger.write(alert, "acknowledged", "cli")
            out("acknowledged on this Mac only: %s. %s" % (a.target, LOCAL_ACK_NOTE.split(". ", 1)[1]))
            return 0
        if a.command == "status":
            status = {"settings": settings.public(), "dry_run": a.dry_run}
            try:
                status["streams"] = [c.describe() for c in group_sources(load_sources(environ))]
            except ConfigError as err:
                status["sources_error"] = str(err)
            ledger = Ledger(settings.state_dir)
            entries = ledger.entries()
            status["cursors"] = StateStore(settings.state_dir).cursors
            status["ledger"] = {"path": ledger.path, "counts": ledger_counts(entries),
                                "last": entries[-5:]}
            out(json.dumps(status, indent=1, ensure_ascii=False))
            return 0
        sources = load_sources(environ)
    except ConfigError as err:
        sys.stderr.write("alert_listener: %s\n" % err)
        return 2
    ledger = Ledger(settings.state_dir)
    presenter = Presenter(settings, ledger, runner, spawner, a.dry_run, out)
    listener = Listener(settings, sources, presenter, opener=opener, out=out)
    out("LOCKI alert listener: %s%s" % (json.dumps([c.describe() for c in listener.conns], ensure_ascii=False),
                                        " (dry run)" if a.dry_run else ""))
    listener.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
