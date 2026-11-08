"""Presence push: only a SpacetimeDB-accepted person_in_zone pushes, at priority <= 3, rate limited and once per
visit (reset by the LOCAL zone clear); the topic never reaches a log; the payload carries no image or biometric
field; failures never raise and never block the outbox. stdlib unittest, no network (fake openers only)."""
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
import unittest
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from locki_edge.capture import CaptureSource  # noqa: E402
from locki_edge.config import ConfigError, EdgeConfig  # noqa: E402
from locki_edge.contract import is_forbidden_key  # noqa: E402
from locki_edge.notify import (PAYLOAD_KEYS, PresencePusher, PushConfigError, parse_push_url,  # noqa: E402
                               presence_text)
from locki_edge.observers import FixtureProvider  # noqa: E402
from locki_edge.runtime import EdgeRuntime  # noqa: E402
from locki_edge.stdb import PublishReceipt  # noqa: E402
from test_runtime_failures import FakeCamera  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOPIC = "locki-presence-test-q7Zr"          # fake; the host below is reserved (.invalid), nothing can resolve it
URL = "https://ntfy.invalid/" + TOPIC
FORBIDDEN_WORDS = re.compile(r"police|911|security (team |desk )?(was |has been )?(notified|alerted)|delivered|"
                             r"\bseen\b", re.I)
IMAGE_OR_BIOMETRIC = ("image", "attach", "attachment", "icon", "click", "confidence", "face", "frame", "camera",
                      "biometric", "embedding", "bbox", "landmark", "photo", "jpeg", "png", "url")
T0_MS = 1794122002000                          # 2026-11-08T07:13:22Z


class Resp(object):
    def __init__(self, status, body=b'{"id":"Rcpt0001","event":"message"}'):
        self.status, self.body, self.closed = status, body, False

    def getcode(self):
        return self.status

    def read(self):
        return self.body

    def close(self):
        self.closed = True


class Opener(object):
    def __init__(self, results):
        self.results, self.reqs = list(results), []

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, Resp) else Resp(r)


class Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def receipt(status, kind="person_in_zone", event_id="e-1", seq=7):
    return PublishReceipt(event_id, seq, kind, status, status in ("accepted", "duplicate"),
                          status in ("retryable", "ambiguous"), 200, "", "", "ok", 1, "x", 140.0)


class ParseUrlTest(unittest.TestCase):
    def test_https_or_loopback_only_and_errors_never_echo_the_topic(self):
        self.assertEqual(parse_push_url(URL), ("https://ntfy.invalid", TOPIC))
        self.assertEqual(parse_push_url("http://127.0.0.1:8080/" + TOPIC), ("http://127.0.0.1:8080", TOPIC))
        self.assertEqual(parse_push_url("https://h.invalid/ntfy/" + TOPIC), ("https://h.invalid/ntfy", TOPIC))
        for bad in ("http://ntfy.invalid/" + TOPIC, "https://ntfy.invalid/" + TOPIC + "?auth=x",
                    "https://u:p@ntfy.invalid/" + TOPIC, "https://ntfy.invalid/", "ftp://ntfy.invalid/" + TOPIC,
                    "https://ntfy.invalid/bad topic!"):
            with self.assertRaises(PushConfigError) as cm:
                parse_push_url(bad)
            self.assertNotIn(TOPIC, str(cm.exception))


class PusherTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.logs = []
        self.clock = Clock()

    def tearDown(self):
        shutil.rmtree(self.d)

    def make(self, opener=None, **kw):
        kw.setdefault("retry_delays", (0, 0))
        return PresencePusher(URL, "ws-1", "jetson-01", opener=opener or Opener([200]), clock=self.clock,
                              wall=lambda: T0_MS / 1000.0, start_worker=False,
                              log=lambda level, msg, **f: self.logs.append(dict(f, level=level, msg=msg)),
                              audit_path=os.path.join(self.d, "push_audit.jsonl"), **kw)

    def accept(self, p, event_id, zone="desk_01", observed_ms=T0_MS):
        p.note_event(event_id, zone, observed_ms)
        return p.on_receipt(receipt("accepted", event_id=event_id))

    def test_payload_is_the_exact_presence_text_and_nothing_else(self):
        op = Opener([200])
        p = self.make(op)
        self.assertEqual(self.accept(p, "3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d"), "queued")
        event_id, zone, payload = p.queue.get_nowait()
        self.assertEqual(sorted(payload), sorted(PAYLOAD_KEYS))
        self.assertEqual(payload["topic"], TOPIC)
        self.assertEqual(payload["title"], u"LOCKI presence · ws-1")
        self.assertEqual(payload["message"], u"Person observed in desk_01 at 07:13:22Z. Observation only, not an "
                                             u"incident. Event 3f2a9c1b-7d4e-4a5b-9c6d-0e1f2a3b4c5d · device "
                                             u"jetson-01.")
        self.assertEqual(payload["priority"], 3)
        self.assertEqual(payload["tags"], ["eyes", "locki-presence"])
        for key in payload:
            self.assertFalse(is_forbidden_key(key), key)
            self.assertFalse(any(word in key.lower() for word in IMAGE_OR_BIOMETRIC), key)
        self.assertEqual(p.deliver(event_id, zone, payload), "Rcpt0001")
        req = op.reqs[0]
        self.assertEqual((req.full_url, req.get_method()), ("https://ntfy.invalid/", "POST"))  # topic in the body only
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode("utf-8")), payload)

    def test_only_person_in_zone_pushes(self):
        p = self.make()
        for kind in ("unrecognized_face", "owner_face_match", "face_uncertain", "camera_online", "person_left_zone",
                     "keyboard_interaction", "usb_zone_interaction", "heartbeat"):
            p.note_event("e-%s" % kind, "desk_01", T0_MS)
            self.assertIsNone(p.on_receipt(receipt("accepted", kind=kind, event_id="e-%s" % kind)))
        self.assertEqual(p.queue.qsize(), 0)

    def test_only_accepted_pushes_and_a_retry_keeps_where_and_when(self):
        p = self.make()
        for i, status in enumerate(("rejected", "duplicate", "retryable", "ambiguous")):
            p.note_event("e-%d" % i, "desk_01", T0_MS)
            self.assertIsNone(p.on_receipt(receipt(status, event_id="e-%d" % i)))
        self.assertEqual(p.queue.qsize(), 0)
        p.note_event("e-r", "zone_b", T0_MS)
        p.on_receipt(receipt("retryable", event_id="e-r"))
        self.assertEqual(p.on_receipt(receipt("accepted", event_id="e-r")), "queued")
        self.assertIn("in zone_b at 07:13:22Z", p.queue.get_nowait()[2]["message"])

    def test_priority_is_clamped_to_1_through_3(self):
        self.assertEqual(self.make(priority=5).priority, 3)
        self.assertEqual(self.make(priority=0).priority, 1)
        self.assertEqual(self.make(priority=2).priority, 2)
        p = self.make(priority=5)
        self.accept(p, "e-1")
        self.assertEqual(p.queue.get_nowait()[2]["priority"], 3)

    def test_rate_limit_per_workstation_and_zone(self):
        p = self.make(min_interval_s=600)
        self.assertEqual(self.accept(p, "e-1"), "queued")
        p.visit_ended("desk_01")
        self.clock.t += 100
        self.assertEqual(self.accept(p, "e-2"), "suppressed")
        self.assertEqual(self.logs[-1]["reason"], "rate_limited")
        self.assertEqual(self.accept(p, "e-z", zone="door_01"), "queued")   # another zone has its own budget
        p.visit_ended("desk_01")
        self.clock.t += 600
        self.assertEqual(self.accept(p, "e-3"), "queued")
        self.assertEqual(p.describe()["suppressed"], 1)

    def test_once_per_visit_until_the_local_clear(self):
        p = self.make(min_interval_s=600)
        self.assertEqual(self.accept(p, "e-1"), "queued")
        self.clock.t += 5000                                              # well past the interval, same visit
        self.assertEqual(self.accept(p, "e-2"), "suppressed")
        self.assertEqual(self.logs[-1]["reason"], "once_per_visit")
        p.visit_ended("desk_01")
        self.assertEqual(self.accept(p, "e-3"), "queued")

    def test_a_clear_before_the_receipt_still_starts_a_new_visit(self):
        p = self.make(min_interval_s=600)
        p.note_event("e-1", "desk_01", T0_MS)
        p.visit_ended("desk_01")                                          # short visit: clear beats the receipt
        self.assertEqual(p.on_receipt(receipt("accepted", event_id="e-1")), "queued")
        self.clock.t += 700
        self.assertEqual(self.accept(p, "e-2"), "queued")                 # a new visit, not "once_per_visit"

    def test_ntfy_receipt_is_recorded_as_accepted(self):
        p = self.make(Opener([Resp(200, b'{"id":"AbC123xyz","time":1}')]))
        self.accept(p, "e-1")
        self.assertEqual(p.deliver(*p.queue.get_nowait()), "AbC123xyz")
        with open(os.path.join(self.d, "push_audit.jsonl")) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual((lines[-1]["status"], lines[-1]["provider_receipt"], lines[-1]["event_id"]),
                         ("accepted", "AbC123xyz", "e-1"))
        self.assertEqual(p.describe()["last"]["provider_receipt"], "AbC123xyz")
        self.assertEqual(p.describe()["accepted"], 1)
        self.assertEqual(oct(os.stat(os.path.join(self.d, "push_audit.jsonl")).st_mode & 0o777), "0o600")

    def test_bounded_retries_never_raise(self):
        p = self.make(Opener([URLError("down"), 500, Resp(200, b"{}"), 200]), retry_delays=(0, 0, 0))
        self.accept(p, "e-1")
        self.assertEqual(p.deliver(*p.queue.get_nowait()), "Rcpt0001")   # no receipt id counts as transient
        self.assertEqual(p.describe()["last"]["attempt"], 4)
        op = Opener([OSError("x")] * 4)
        p = self.make(op, retry_delays=(0, 0, 0))
        self.accept(p, "e-2")
        self.assertIsNone(p.deliver(*p.queue.get_nowait()))
        self.assertEqual((len(op.reqs), p.describe()["failed"]), (4, 1))
        op = Opener([400, 200])
        p = self.make(op)
        self.accept(p, "e-3")
        self.assertIsNone(p.deliver(*p.queue.get_nowait()))
        self.assertEqual(len(op.reqs), 1)                                 # a refusal is not retried

    def test_topic_never_reaches_logs_audit_or_describe(self):
        p = self.make(Opener([OSError("connect to %s failed" % URL)] * 3))
        self.accept(p, "e-1")
        p.deliver(*p.queue.get_nowait())
        with open(os.path.join(self.d, "push_audit.jsonl")) as f:
            audit = f.read()
        everything = json.dumps(self.logs) + audit + json.dumps(p.describe())
        self.assertNotIn(TOPIC, everything)
        self.assertNotIn("ntfy.invalid/", everything)
        self.assertIn(u"ntfy …q7Zr", p.describe()["destination"])

    def test_on_receipt_only_enqueues_even_when_ntfy_hangs(self):
        gate, started = threading.Event(), threading.Event()

        def slow(req, timeout=None):
            started.set()
            gate.wait(5)
            return Resp(200)
        p = PresencePusher(URL, "ws-1", "jetson-01", opener=slow, retry_delays=(0,))
        try:
            self.accept(p, "e-1")
            self.assertTrue(started.wait(2))
            t0 = time.monotonic()
            p.visit_ended("desk_01")
            p.limiter.clock = lambda: 10 ** 9
            self.assertEqual(self.accept(p, "e-2"), "queued")
            self.assertLess(time.monotonic() - t0, 0.5)
        finally:
            gate.set()
            p.close()

    def test_wording_has_no_forbidden_claims(self):
        for sim in (False, True):
            title, message = presence_text("ws-1", "desk_01", T0_MS, "e-1", "jetson-01", simulated=sim)
            self.assertIsNone(FORBIDDEN_WORDS.search(title + " " + message))
            self.assertIn("Observation only, not an incident.", message)
        self.assertTrue(presence_text("ws", "z", T0_MS, "e", "d", True)[1].startswith("Simulated rehearsal. "))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.values = {"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_PUBLISH": "off"}

    def tearDown(self):
        shutil.rmtree(self.d)

    def cfg(self, **extra):
        return EdgeConfig(dict(self.values, **extra), platform_name="jetson")

    def url_file(self, mode, text=URL):
        path = os.path.join(self.d, "push_url")
        with open(path, "w") as f:
            f.write(text + "\n")
        os.chmod(path, mode)
        return path

    def test_defaults_push_off_priority_3_interval_600(self):
        cfg = self.cfg()
        self.assertEqual((cfg.push_url, cfg.push_priority, cfg.push_min_interval_s), ("", 3, 600.0))

    def test_url_file_must_be_owner_only_and_errors_never_echo_the_topic(self):
        with self.assertRaises(ConfigError) as cm:
            self.cfg(AIRLOCK_PUSH_URL_FILE=self.url_file(0o644))
        self.assertIn("chmod 600", str(cm.exception))
        self.assertNotIn(TOPIC, str(cm.exception))
        self.assertEqual(self.cfg(AIRLOCK_PUSH_URL_FILE=self.url_file(0o600)).push_url, URL)
        with self.assertRaises(ConfigError) as cm:
            self.cfg(AIRLOCK_PUSH_URL_FILE=self.url_file(0o600, "http://ntfy.invalid/" + TOPIC))
        self.assertNotIn(TOPIC, str(cm.exception))

    def test_not_both_and_https_only(self):
        with self.assertRaises(ConfigError):
            self.cfg(AIRLOCK_PUSH_URL=URL, AIRLOCK_PUSH_URL_FILE=self.url_file(0o600))
        with self.assertRaises(ConfigError) as cm:
            self.cfg(AIRLOCK_PUSH_URL="http://ntfy.invalid/" + TOPIC)
        self.assertNotIn(TOPIC, str(cm.exception))
        self.assertEqual(self.cfg(AIRLOCK_PUSH_URL="http://127.0.0.1:9/" + TOPIC).push_url,
                         "http://127.0.0.1:9/" + TOPIC)

    def test_priority_clamp_and_bad_numbers(self):
        self.assertEqual(self.cfg(AIRLOCK_PUSH_PRIORITY="5").push_priority, 3)
        self.assertEqual(self.cfg(AIRLOCK_PUSH_PRIORITY="0").push_priority, 1)
        for bad in ({"AIRLOCK_PUSH_PRIORITY": "high"}, {"AIRLOCK_PUSH_MIN_INTERVAL_S": "-1"},
                    {"AIRLOCK_PUSH_MIN_INTERVAL_S": "soon"}):
            with self.assertRaises(ConfigError):
                self.cfg(**bad)

    def test_inline_url_is_masked_in_the_raw_copy(self):
        cfg = self.cfg(AIRLOCK_PUSH_URL=URL)
        self.assertEqual(cfg.push_url, URL)
        self.assertNotIn(TOPIC, json.dumps(cfg.raw) + json.dumps(cfg.public(), default=str))


class RuntimeHookTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        tok = os.path.join(self.d, "tok")
        with open(tok, "w") as f:
            f.write("t")
        os.chmod(tok, 0o600)
        self.values = {"AIRLOCK_EDGE_RUNTIME": "jetson", "AIRLOCK_DEVICE_ID": "jetson-01",
                       "AIRLOCK_WORKSTATION_ID": "ws-1", "AIRLOCK_EDGE_AUTH_FILE": tok,
                       "AIRLOCK_STATE_DIR": os.path.join(self.d, "state"), "AIRLOCK_SESSION_ID": "sess-1",
                       "AIRLOCK_CONTRACT_FILE": os.path.join(ROOT, "contract", "locki-w91bw.live.json"),
                       "AIRLOCK_PUSH_URL": URL}
        self.clock = Clock()
        self.stdout = io.StringIO()

    def tearDown(self):
        shutil.rmtree(self.d)

    def rt(self):
        class Sql(object):
            def sql_rows(self, q):
                return 200, [], ""
        with contextlib.redirect_stdout(self.stdout):
            rt = EdgeRuntime(EdgeConfig(self.values, platform_name="jetson"),
                             capture=CaptureSource(0, "jetson", opener=FakeCamera().opener),
                             provider=FixtureProvider(lambda i: []), client=Sql())
        self.assertIsInstance(rt.push, PresencePusher)
        self.assertEqual((rt.push.priority, rt.push.limiter.min_interval_s), (3, 600.0))
        rt.push.close()                      # tests inspect the queue instead of sending
        rt.push.limiter.clock = self.clock
        return rt

    def emit(self, rt, kind, zone="desk_01"):
        with contextlib.redirect_stdout(self.stdout):
            return rt.emit(kind, 0.9 if kind == "person_in_zone" else None, int(time.time() * 1000), None,
                           {"zone_id": zone, "observation": "presence" if kind == "person_in_zone" else "absence"})

    def answer(self, rt, event, status="accepted"):
        with contextlib.redirect_stdout(self.stdout):
            rt._on_receipt(receipt(status, kind=event.kind, event_id=event.event_id, seq=event.seq))

    def test_accepted_person_in_zone_pushes_and_the_local_clear_resets_the_visit(self):
        rt = self.rt()
        e1 = self.emit(rt, "person_in_zone")
        self.answer(rt, e1)
        self.assertEqual(rt.push.queue.qsize(), 1)
        self.assertIn(e1.event_id, rt.push.queue.get_nowait()[2]["message"])
        self.clock.t += 5000
        e2 = self.emit(rt, "person_in_zone")       # camera flap: a second enter with no clear in between
        self.answer(rt, e2)
        self.assertEqual(rt.push.queue.qsize(), 0)
        self.assertEqual(rt.push.describe()["suppressed"], 1)
        # The local clear path: the normalizer's person_left_zone, refused by the live module for role jetson, so it
        # never reaches the DB. It must still end the visit.
        self.assertIsNone(self.emit(rt, "person_left_zone"))
        self.assertEqual(rt.local_only.get("kind_not_in_contract"), 1)
        e3 = self.emit(rt, "person_in_zone")
        self.answer(rt, e3)
        self.assertEqual(rt.push.queue.qsize(), 1)

    def test_local_clear_counts_while_disarmed_too(self):
        rt = self.rt()
        e1 = self.emit(rt, "person_in_zone")
        self.answer(rt, e1)
        rt.push.queue.get_nowait()
        rt.session.armed = False
        self.emit(rt, "person_left_zone")
        rt.session.armed = True
        self.clock.t += 700
        e2 = self.emit(rt, "person_in_zone")
        self.answer(rt, e2)
        self.assertEqual(rt.push.queue.qsize(), 1)

    def test_nothing_but_an_accepted_person_in_zone_pushes(self):
        rt = self.rt()
        for status in ("rejected", "duplicate", "retryable", "ambiguous"):
            self.answer(rt, self.emit(rt, "person_in_zone"), status)
        with contextlib.redirect_stdout(self.stdout):
            for kind in ("unrecognized_face", "camera_online"):
                rt._on_receipt(receipt("accepted", kind=kind, event_id="x-" + kind))
        self.assertEqual(rt.push.queue.qsize(), 0)
        self.assertEqual(rt.local_test_alert(), {"played": False, "reason": "AIRLOCK_LOCAL_ALARM=off"})
        self.assertEqual(rt.push.queue.qsize(), 0)        # no test push from the edge

    def test_topic_never_in_stdout_health_or_audit(self):
        rt = self.rt()
        e1 = self.emit(rt, "person_in_zone")
        self.answer(rt, e1)
        rt.push.opener = Opener([Resp(200, b'{"id":"Zz9"}')])
        with contextlib.redirect_stdout(self.stdout):
            rt.push.deliver(*rt.push.queue.get_nowait())
            health = json.dumps(rt.health(), default=str)
        with open(os.path.join(self.d, "state", "push_audit.jsonl")) as f:
            audit = f.read()
        for text in (self.stdout.getvalue(), health, audit):
            self.assertNotIn(TOPIC, text)
        self.assertIn('"provider_receipt": "Zz9"', audit)
        self.assertIn('"status": "accepted"', audit)

    def test_no_push_url_means_no_pusher_and_http_is_refused(self):
        self.values["AIRLOCK_PUSH_URL"] = ""

        class Sql(object):
            def sql_rows(self, q):
                return 200, [], ""
        with contextlib.redirect_stdout(self.stdout):
            self.assertIsNone(EdgeRuntime(EdgeConfig(self.values, platform_name="jetson"),
                                          capture=CaptureSource(0, "jetson", opener=FakeCamera().opener),
                                          provider=FixtureProvider(lambda i: []), client=Sql()).push)
        self.values["AIRLOCK_PUSH_URL"] = "http://ntfy.invalid/x"
        with self.assertRaises(ConfigError):
            EdgeConfig(self.values, platform_name="jetson")


if __name__ == "__main__":
    unittest.main()
