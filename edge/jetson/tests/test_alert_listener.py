"""Mac alert listener (tools/alert_listener.py): sources from env/files only, exact classification of the M4
dispatch and Jetson presence strings, persisted dedupe, no replay, presence cooldown, ledger transitions, dialog
result mapping, never a volume change, never a forbidden claim. stdlib unittest; no network, no osascript (fakes)."""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import tokenize
import unittest
from urllib.error import HTTPError

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import alert_listener as al  # noqa: E402
from locki_edge.notify import presence_text  # noqa: E402

PHONE_TOPIC = "locki-PhoneTopicSecret-AbCdEf123456"     # fake topics on a reserved host: nothing resolves
PRESENCE_TOPIC = "locki-presence-test-q7Zr"
SERVER = "https://ntfy.invalid"
NOW = 1794122002.0
FORBIDDEN = re.compile(r"police|911|security (team |desk )?(was |has been )?(notified|alerted)|delivered|\bseen\b",
                       re.I)
SOUND = "/System/Library/Sounds/Sosumi.aiff"


def dispatch_message(severity, workstation, incident_id, summary, created_at_ms):
    """Built exactly the way backend/app/security/dispatch.py Dispatcher.message() + NtfyChannel.send() build it."""
    sev = severity.capitalize()
    when = time.strftime("%H:%M:%S", time.localtime(created_at_ms / 1000))
    summary = (summary or "Security incident while armed.").strip()
    body = u"%s · %s Incident %s. Review and acknowledge it in the LOCKI console." % (when, summary,
                                                                                          incident_id[-12:])
    priority = "urgent" if severity == "critical" else "high" if severity == "high" else "default"
    title = u"LOCKI %s incident · %s" % (sev, workstation)
    return {"title": title, "message": body[:500], "priority": {"default": 3, "high": 4, "urgent": 5}[priority],
            "tags": ["rotating_light"] if priority in ("urgent", "high") else ["warning"]}


def ntfy_event(mid, fields, topic=PHONE_TOPIC, t=NOW):
    ev = {"id": mid, "event": "message", "topic": topic}
    if t is not None:
        ev.update(time=int(t), expires=int(t) + 43200)
    ev.update(fields)
    return ev


def presence_event(mid, event_id, ws="dhruv-m4-air", t=NOW, topic=PRESENCE_TOPIC):
    title, message = presence_text(ws, "desk_01", int(t * 1000), event_id, "jetson-01")
    return ntfy_event(mid, {"title": title, "message": message, "priority": 3, "tags": ["eyes", "locki-presence"]},
                      topic=topic, t=t)


TEST_FIELDS = {"title": "LOCKI test notification", "message": "A test from LOCKI security dispatch. No incident.",
               "priority": 3, "tags": ["warning"]}
INC = "incident:3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d"


class Clock(object):
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeRunner(object):
    def __init__(self, dialog=(0, "Acknowledge|false", "")):
        self.cmds, self.dialog = [], dialog

    def __call__(self, cmd, timeout=None):
        self.cmds.append(list(cmd))
        if cmd[0] == "osascript" and any("display alert" in c for c in cmd):
            return self.dialog
        return 0, "", ""

    def dialogs(self):
        return [c for c in self.cmds if any("display alert" in x for x in c)]

    def banners(self):
        return [c for c in self.cmds if any("display notification" in x for x in c)]


class FakeProc(object):
    def poll(self):
        return 0

    def terminate(self):
        pass


class FakeSpawner(object):
    def __init__(self):
        self.cmds = []

    def __call__(self, cmd):
        self.cmds.append(list(cmd))
        return FakeProc()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.state = os.path.join(self.d, "state")
        self.env = {"AIRLOCK_ALERT_STATE_DIR": self.state, "AIRLOCK_ALERT_SOUND": SOUND}
        self.clock = Clock()
        self.lines = []
        self.runner, self.spawner = FakeRunner(), FakeSpawner()

    def tearDown(self):
        shutil.rmtree(self.d)

    def secret(self, name, text, mode=0o600):
        path = os.path.join(self.d, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def phone(self, mode=0o600, **extra):
        cfg = {"provider": "ntfy", "server": SERVER, "topic": PHONE_TOPIC}
        cfg.update(extra)
        return self.secret("phone.json", json.dumps(cfg), mode)

    def full_env(self, phone=None, **extra):
        env = dict(self.env, AIRLOCK_ALERT_PHONE_CONFIG=self.phone(**(phone or {})),
                   AIRLOCK_PUSH_URL_FILE=self.secret("push_url", "%s/%s\n" % (SERVER, PRESENCE_TOPIC)))
        env.update(extra)
        return env

    def listener(self, env=None, dry_run=False):
        env = env or self.full_env()
        settings = al.Settings(env)
        presenter = al.Presenter(settings, al.Ledger(settings.state_dir), self.runner, self.spawner, dry_run,
                                 self.lines.append, sync=True)
        return al.Listener(settings, al.load_sources(env), presenter, clock=self.clock, out=self.lines.append,
                           sleep=lambda s: None)

    def ledger(self):
        return al.Ledger(self.state).entries()

    def states(self, key=None):
        return [e["state"] for e in self.ledger() if key is None or e["key"] == key]

    def everything_written(self):
        out = "\n".join(self.lines)
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name)) as f:
                out += f.read()
        return out


class ConfigTest(Base):
    def test_secret_files_must_be_owner_only_and_errors_never_echo_a_topic(self):
        with self.assertRaises(al.ConfigError) as cm:
            al.load_sources(dict(self.env, AIRLOCK_ALERT_PHONE_CONFIG=self.phone(mode=0o644)))
        self.assertIn("chmod 600", str(cm.exception))
        with self.assertRaises(al.ConfigError) as cm:
            al.load_sources(dict(self.env, AIRLOCK_PUSH_URL_FILE=self.secret("u", SERVER + "/" + PRESENCE_TOPIC,
                                                                             0o640)))
        self.assertNotIn(PRESENCE_TOPIC, str(cm.exception))
        for bad in ({"AIRLOCK_PUSH_URL": "http://ntfy.invalid/" + PRESENCE_TOPIC},
                    {"AIRLOCK_PUSH_URL": SERVER + "/" + PRESENCE_TOPIC + "?x=1"},
                    {"AIRLOCK_PUSH_URL": SERVER + "/x", "AIRLOCK_PUSH_URL_FILE": self.secret("v", SERVER + "/y")},
                    {"AIRLOCK_ALERT_PHONE_CONFIG": self.phone(server="http://ntfy.invalid")},
                    {"AIRLOCK_ALERT_PHONE_CONFIG": self.phone(provider="sms")}, {}):
            with self.assertRaises(al.ConfigError) as cm:
                al.load_sources(dict(self.env, **bad))
            self.assertNotIn(PRESENCE_TOPIC, str(cm.exception))
            self.assertNotIn(PHONE_TOPIC, str(cm.exception))

    def test_console_url_is_loopback_http_or_https(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "http://example.com/", "ftp://127.0.0.1/"):
            with self.assertRaises(al.ConfigError):
                al.Settings(dict(self.env, AIRLOCK_ALERT_CONSOLE_URL=bad))
        self.assertEqual(al.Settings(self.env).console_url, "http://127.0.0.1:5174/?product=airlock")
        self.assertEqual(al.Settings(dict(self.env, AIRLOCK_ALERT_CONSOLE_URL="https://console.invalid/x"))
                         .console_url, "https://console.invalid/x")

    def test_cli_refuses_urls_and_topics_without_echoing_them(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(al.main([SERVER + "/" + PRESENCE_TOPIC], environ=self.full_env()), 2)
            with self.assertRaises(SystemExit):
                al.main([PRESENCE_TOPIC], environ=self.full_env())
        self.assertNotIn(PRESENCE_TOPIC, err.getvalue())

    def test_status_shows_config_without_secrets(self):
        out = []
        self.assertEqual(al.main(["status"], environ=self.full_env(), out=out.append), 0)
        text = "\n".join(out)
        self.assertNotIn(PHONE_TOPIC, text)
        self.assertNotIn(PRESENCE_TOPIC, text)
        status = json.loads(text)
        self.assertEqual([s["destination"] for s in status["streams"][0]["sources"]],
                         [u"ntfy …" + PHONE_TOPIC[-4:], u"ntfy …" + PRESENCE_TOPIC[-4:]])
        self.assertIn("counts", status["ledger"])

    def test_one_stream_per_server_and_token(self):
        conns = al.group_sources(al.load_sources(self.full_env()))
        self.assertEqual(len(conns), 1)
        self.assertEqual(conns[0].topics, [PHONE_TOPIC, PRESENCE_TOPIC])
        env = self.full_env(phone={"token": "tk_fake"})
        self.assertEqual(len(al.group_sources(al.load_sources(env))), 2)


class ClassifyTest(unittest.TestCase):
    INC_ROLES, PRES_ROLES = {"incidents"}, {"presence"}

    def classify(self, fields, roles):
        return al.classify(ntfy_event("m1", fields), roles)

    def test_dispatch_strings(self):
        created = int(NOW * 1000)
        for severity, level in (("medium", "incident"), ("high", "alarm"), ("critical", "alarm"),
                                ("low", "incident"), ("info", "incident")):
            a = self.classify(dispatch_message(severity, "dhruv-m4-air", INC, "New unapproved USB device detected "
                                               "while armed.", created), self.INC_ROLES)
            self.assertEqual((a.level, a.severity, a.source, a.workstation, a.incident_suffix),
                             (level, severity, "m4-dispatch", "dhruv-m4-air", INC[-12:]))
            self.assertEqual(a.summary, "New unapproved USB device detected while armed.")
            self.assertEqual(a.key, "incident:%s#%s" % (INC[-12:], severity))
        panic = self.classify(dispatch_message("high", "this workstation", "incident:owner-panic:sess-42", None,
                                               created), self.INC_ROLES)
        self.assertEqual((panic.level, panic.incident_suffix, panic.summary),
                         ("alarm", "-panic:sess-42"[-12:], "Security incident while armed."))

    def test_test_presence_and_unknown(self):
        self.assertEqual(self.classify(TEST_FIELDS, self.INC_ROLES).level, "test")
        p = al.classify(presence_event("m2", "3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d"), self.PRES_ROLES)
        self.assertEqual((p.level, p.source, p.workstation, p.event_id, p.zone, p.clock),
                         ("presence", "jetson-edge", "dhruv-m4-air", "3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d",
                          "desk_01", "07:13:22Z"))
        self.assertEqual(p.key, "event:3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d")
        u = self.classify({"title": "LOCKI: someone is at ws", "message": "anything"}, self.INC_ROLES)
        self.assertEqual((u.level, u.source, u.key), ("unknown", "unknown", "ntfy:m1"))

    def test_a_known_format_on_the_wrong_topic_is_unverified(self):
        a = self.classify(dispatch_message("high", "ws", INC, "x", int(NOW * 1000)), self.PRES_ROLES)
        self.assertEqual((a.level, a.note), ("unknown", "known format on the wrong topic"))
        p = al.classify(presence_event("m3", "3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d"), self.INC_ROLES)
        self.assertEqual(p.level, "unknown")

    def test_elevated_is_reserved_and_never_produced(self):
        self.assertIn("elevated", al.LEVELS)
        self.assertIn("No producer exists yet", al.__doc__)
        self.assertNotIn("elevated", al.SEVERITY_LEVEL.values())


class ListenerTest(Base):
    def test_first_start_since_now_then_the_cursor_across_reconnect_and_restart(self):
        lst = self.listener()
        conn = lst.conns[0]
        self.assertEqual(lst.stream_url(conn), "%s/%s,%s/json?since=%d" % (SERVER, PHONE_TOPIC, PRESENCE_TOPIC,
                                                                          int(NOW)))
        self.clock.t += 50
        self.assertTrue(lst.stream_url(conn).endswith("since=%d" % int(NOW)))   # no gap before the first message
        lst.handle(conn, ntfy_event("AbC1", TEST_FIELDS, t=self.clock.t))
        self.assertTrue(lst.stream_url(conn).endswith("since=AbC1"))
        self.assertTrue(self.listener().stream_url(conn).endswith("since=AbC1"))  # restart: same state dir

    def test_stream_reconnects_with_the_cursor_and_ignores_open_and_keepalive(self):
        lst = self.listener(self.full_env(phone={"token": "tk_fake"}))
        conn = lst.conns[0]
        reqs = []
        lines = [json.dumps({"id": "o1", "time": int(NOW), "event": "open", "topic": PHONE_TOPIC}).encode(),
                 json.dumps({"id": "k1", "time": int(NOW), "event": "keepalive", "topic": PHONE_TOPIC}).encode(),
                 b"not json",
                 json.dumps(ntfy_event("Msg1", TEST_FIELDS)).encode()]
        script = [lines, HTTPError("x", 502, "bad gateway", {}, None), []]

        def opener(req, timeout=None):
            reqs.append(req)
            r = script.pop(0)
            if isinstance(r, Exception):
                raise r
            return iter(r)
        lst.opener = opener
        lst.run_connection(conn, max_rounds=3)
        self.assertEqual(reqs[0].get_header("Authorization"), "Bearer tk_fake")
        self.assertTrue(reqs[0].full_url.endswith("/json?since=%d" % int(NOW)))
        self.assertTrue(reqs[1].full_url.endswith("since=Msg1"))
        self.assertTrue(reqs[2].full_url.endswith("since=Msg1"))
        self.assertEqual(self.states(), ["received", "displayed"])
        self.assertNotIn(PHONE_TOPIC, "\n".join(self.lines))
        self.assertNotIn("tk_fake", "\n".join(self.lines))

    def test_dedupe_survives_a_restart_and_covers_a_new_ntfy_id_for_the_same_incident(self):
        m = dispatch_message("medium", "ws-1", INC, "New unapproved USB device detected while armed.",
                             int(NOW * 1000))
        lst = self.listener()
        self.assertEqual(lst.handle(lst.conns[0], ntfy_event("n1", m)), "queued")
        lst2 = self.listener()
        conn = lst2.conns[0]
        self.assertEqual(lst2.handle(conn, ntfy_event("n1", m)), "duplicate")
        self.assertEqual(lst2.handle(conn, ntfy_event("n2", m)), "duplicate")   # e.g. a retried publish
        high = dispatch_message("high", "ws-1", INC, "Unknown USB device detected within 10 seconds of a person "
                                "in the desk zone.", int(NOW * 1000))
        self.assertEqual(lst2.handle(conn, ntfy_event("n3", high)), "queued")    # severity rose: a new alert
        key = "incident:%s#medium" % INC[-12:]
        self.assertEqual(self.states(key), ["received", "displayed", "acknowledged", "received", "duplicate",
                                            "received", "duplicate"])
        self.assertEqual(len(self.runner.dialogs()), 2)

    def test_stale_messages_are_never_shown(self):
        lst = self.listener()
        m = dispatch_message("high", "ws-1", INC, "x", int(NOW * 1000))
        self.assertEqual(lst.handle(lst.conns[0], ntfy_event("old1", m, t=NOW - 301)), "stale")
        self.assertEqual(lst.handle(lst.conns[0], ntfy_event("old2", dict(TEST_FIELDS), t=None)), "stale")
        self.assertEqual(self.runner.cmds, [])
        self.assertEqual(self.spawner.cmds, [])

    def test_presence_cooldown_per_workstation_survives_restart(self):
        lst = self.listener()
        conn = lst.conns[0]
        uid = "00000000-0000-4000-8000-%012d"
        self.assertEqual(lst.handle(conn, presence_event("p1", uid % 1, "ws-1")), "queued")
        self.clock.t += 10
        self.assertEqual(lst.handle(conn, presence_event("p2", uid % 2, "ws-1", t=self.clock.t)), "suppressed")
        self.assertEqual(lst.handle(conn, presence_event("p3", uid % 3, "ws-2", t=self.clock.t)), "queued")
        self.clock.t += 10
        self.assertEqual(self.listener().handle(conn, presence_event("p4", uid % 4, "ws-1", t=self.clock.t)),
                         "suppressed")
        self.clock.t += 300
        self.assertEqual(lst.handle(conn, presence_event("p5", uid % 5, "ws-1", t=self.clock.t)), "queued")
        self.assertEqual(lst.handle(conn, presence_event("p6", uid % 5, "ws-1", t=self.clock.t)), "duplicate")
        self.assertEqual(self.runner.dialogs(), [])               # presence never opens a dialog
        self.assertEqual(len(self.runner.banners()), 3)

    def test_ledger_and_state_hold_no_topic_no_body_and_are_owner_only(self):
        lst = self.listener()
        summary = "Unknown USB device detected within 10 seconds of a person in the desk zone."
        lst.handle(lst.conns[0], ntfy_event("n1", dispatch_message("high", "ws-1", INC, summary, int(NOW * 1000))))
        lst.handle(lst.conns[0], presence_event("p1", "00000000-0000-4000-8000-000000000001"))
        written = self.everything_written()
        for secret in (PHONE_TOPIC, PRESENCE_TOPIC, summary, "Review and acknowledge"):
            self.assertNotIn(secret, written)
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o700")
        for name in os.listdir(self.state):
            self.assertEqual(oct(os.stat(os.path.join(self.state, name)).st_mode & 0o777), "0o600", name)
        rec = self.ledger()[0]
        self.assertEqual(sorted(rec), sorted(["ts", "key", "ntfy_id", "level", "severity", "source", "workstation",
                                              "incident_suffix", "event_id", "state"]))


class PresentationTest(Base):
    def present(self, fields, dialog=(0, "Acknowledge|false", ""), env=None, topic=PHONE_TOPIC):
        self.runner.dialog = dialog
        self.n = getattr(self, "n", 0) + 1
        lst = self.listener(env)
        return lst.handle(lst.conns[0], ntfy_event("x%d" % self.n, fields, topic=topic))

    def alarm(self):
        return dispatch_message("high", "ws-1", INC, "Unknown USB device detected within 10 seconds of a person in "
                                "the desk zone.", int(NOW * 1000))

    def test_dialog_results_map_to_ledger_states(self):
        cases = [((0, "Acknowledge|false", ""), "acknowledged", "dialog"),
                 ((0, "Open console|false", ""), "opened_console", None),
                 ((0, "|true", ""), "not_acknowledged", "gave_up"),
                 ((0, "Later|false", ""), "not_acknowledged", "later"),
                 ((1, "", "execution error: User canceled. (-128)"), "not_acknowledged", "later"),
                 ((1, "", "execution error: something else (-1700)"), "display_failed", "dialog rc 1")]
        for result, state, detail in cases:
            self.assertEqual(al.map_dialog_result(*result), (state, detail))
        self.present(self.alarm(), dialog=(0, "Open console|false", ""))
        self.assertIn(["open", "http://127.0.0.1:5174/?product=airlock"], self.runner.cmds)
        self.assertEqual(self.states()[-3:], ["received", "displayed", "opened_console"])

    def test_dialog_names_the_incident_and_says_the_ack_is_local(self):
        self.present(self.alarm())
        cmd = self.runner.dialogs()[0]
        script, args = " ".join(cmd[:cmd.index("end run") + 1]), cmd[cmd.index("end run") + 1:]
        self.assertIn('buttons {"Later", "Open console", "Acknowledge"}', script)
        self.assertIn('default button "Acknowledge"', script)
        self.assertIn('cancel button "Later"', script)
        self.assertIn("as critical", script)
        self.assertIn("giving up after", script)
        self.assertEqual(args[0], u"LOCKI High incident · ws-1")
        self.assertIn(INC[-12:], args[1])
        self.assertIn("Acknowledging here is local to this Mac. The SpacetimeDB acknowledgement is done in the "
                      "LOCKI console.", args[1])
        self.assertEqual(args[2], "120")
        self.assertNotIn(args[1], script)                            # text travels as argv data only

    def test_alarm_rings_at_current_volume_and_never_changes_it(self):
        env = self.full_env(AIRLOCK_ALERT_SPEAK="1")
        self.present(self.alarm(), env=env)
        self.present(dispatch_message("medium", "ws-1", "incident:abc-medium-000001", "m", int(NOW * 1000)), env=env)
        self.present(TEST_FIELDS, env=env)
        lst = self.listener(env)
        lst.handle(lst.conns[0], presence_event("pp", "00000000-0000-4000-8000-0000000000aa"))
        afplay = [c for c in self.spawner.cmds if c[0] == "afplay"]
        self.assertTrue(afplay)
        self.assertTrue(all(c == ["afplay", SOUND] for c in afplay))   # no -v: the current volume
        self.assertEqual([c[0] for c in self.spawner.cmds if c[0] == "say"], ["say"])   # once, alarm only
        everything = json.dumps(self.runner.cmds + self.spawner.cmds)
        for banned in ("set volume", "output volume", "output muted", "sound name"):
            self.assertNotIn(banned, everything)

    def test_no_speech_by_default_and_no_dialog_for_test_presence_unknown(self):
        self.present(self.alarm())
        self.assertNotIn("say", [c[0] for c in self.spawner.cmds])
        self.runner.cmds = []
        self.present(TEST_FIELDS)
        self.present({"title": "hello", "message": "spoof attempt"})
        self.present(dispatch_message("high", "ws", INC, "x", int(NOW * 1000)), topic=PRESENCE_TOPIC)
        self.assertEqual(self.runner.dialogs(), [])
        unverified = [c for c in self.runner.banners() if "LOCKI listener: unverified source" in c]
        self.assertEqual(len(unverified), 2)

    def async_listener(self, runner):
        env = self.full_env()
        settings = al.Settings(env)
        presenter = al.Presenter(settings, al.Ledger(settings.state_dir), runner, FakeSpawner(), False,
                                 self.lines.append)
        return al.Listener(settings, al.load_sources(env), presenter, clock=self.clock, out=self.lines.append)

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.02)
        return predicate()

    def test_open_dialogs_never_block_the_stream_or_each_other(self):
        gate = threading.Event()

        class SlowDialogs(FakeRunner):
            def __call__(self, cmd, timeout=None):
                if any("display alert" in c for c in cmd):
                    gate.wait(5)
                return FakeRunner.__call__(self, cmd, timeout)
        lst = self.async_listener(SlowDialogs())
        t0 = time.monotonic()
        self.assertEqual(lst.handle(lst.conns[0], ntfy_event("a1", self.alarm())), "queued")
        medium = dispatch_message("medium", "ws-2", "incident:other-incident-01", "m", int(NOW * 1000))
        self.assertEqual(lst.handle(lst.conns[0], ntfy_event("a2", medium)), "queued")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertTrue(self.wait_for(lambda: self.states().count("displayed") == 2))
        gate.set()
        self.assertTrue(self.wait_for(lambda: self.states().count("acknowledged") == 2))

    def test_a_display_failure_is_ledgered_and_the_worker_survives(self):
        class Flaky(FakeRunner):
            def __call__(self, cmd, timeout=None):
                if not self.cmds:
                    self.cmds.append(cmd)
                    raise RuntimeError("osascript exploded")
                return FakeRunner.__call__(self, cmd, timeout)
        lst = self.async_listener(Flaky())
        lst.handle(lst.conns[0], ntfy_event("f1", TEST_FIELDS))
        lst.handle(lst.conns[0], ntfy_event("f2", dict(TEST_FIELDS, message="second")))
        self.assertTrue(self.wait_for(lambda: "displayed" in self.states()))
        self.assertIn("display_failed", self.states())

    def test_dry_run_runs_nothing(self):
        lst = self.listener(dry_run=True)
        lst.handle(lst.conns[0], ntfy_event("d1", self.alarm()))
        self.assertEqual((self.runner.cmds, self.spawner.cmds), ([], []))
        self.assertTrue(any(line.startswith("[dry-run]") for line in self.lines))
        self.assertEqual(self.states(), ["received"])

    def test_ack_and_show_cli(self):
        lst = self.listener()
        lst.handle(lst.conns[0], ntfy_event("a1", self.alarm()))
        key = "incident:%s#high" % INC[-12:]
        out = []
        self.assertEqual(al.main(["ack", key], environ=self.env, out=out.append), 0)
        self.assertEqual((self.ledger()[-1]["state"], self.ledger()[-1]["detail"]), ("acknowledged", "cli"))
        self.assertEqual(al.main(["ack", "incident:nope#high"], environ=self.env, out=out.append), 1)
        before = len(self.ledger())
        runner = FakeRunner(dialog=(0, "|true", ""))
        self.assertEqual(al.main(["show", "alarm"], environ=self.env, runner=runner, spawner=FakeSpawner(),
                                 out=out.append), 0)
        self.assertEqual(json.loads(out[-1])["results"], [["incident:preview00002#high", "displayed"],
                                                          ["incident:preview00002#high", "not_acknowledged"]])
        self.assertEqual(len(self.ledger()), before)                 # a preview writes no ledger line
        self.assertEqual(al.main(["show", "nonsense"], environ=self.env, out=out.append), 2)


class WordingTest(unittest.TestCase):
    def string_literals(self, path):
        with open(path, "rb") as f:
            for tok in tokenize.tokenize(f.readline):
                if tok.type == tokenize.STRING:
                    yield tok.string

    def test_no_forbidden_words_in_any_template_or_docstring(self):
        for path in (os.path.join(ROOT, "tools", "alert_listener.py"),
                     os.path.join(ROOT, "locki_edge", "notify.py")):
            for s in self.string_literals(path):
                self.assertIsNone(FORBIDDEN.search(s), "%s: %s" % (os.path.basename(path), s[:80]))

    def test_rendered_texts_are_clean(self):
        for level, sample in al.SAMPLES.items():
            a = al.Alert(ntfy_id="p", **sample)
            self.assertIsNone(FORBIDDEN.search(" ".join(al.banner_text(a) + al.dialog_text(a))), level)
        self.assertIsNone(FORBIDDEN.search(al.speech_text(al.Alert(severity="high", workstation="ws"))))


if __name__ == "__main__":
    unittest.main()
