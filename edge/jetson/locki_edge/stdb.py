"""SpacetimeDB HTTP client + typed, honest publish receipts.

SpacetimeDB has no Python SDK, so reducers are called over the documented HTTP API:
    POST {server}/v1/database/{db}/call/{reducer}
    Authorization: Bearer <this device's own token>     (identity = ctx.sender on the server)
    body: JSON array of POSITIONAL reducer args

Receipt statuses (a timeout is NEVER success):
    accepted   2xx: the reducer ran and committed.
    duplicate  the server says it already has this event/seq AND an earlier attempt of ours was
               ambiguous, so that earlier attempt is what committed. Counts as delivered.
    rejected   the reducer returned an error, auth failed, or the request is malformed.
               Never retried (retrying an invalid event can't make it valid).
    retryable  the request never reached the reducer (DNS, refused, 408/429/503). Not delivered.
    ambiguous  the request may have been processed (read timeout, connection reset after send,
               500/502/504). Retried with the SAME event_id + seq; the server dedupes.

Python 3.6 compatible on purpose.
"""
import base64
import datetime
import errno
import json
import re
import socket
import ssl
import time
from typing import NamedTuple, Optional

try:
    from http.client import HTTPException, RemoteDisconnected
except ImportError:  # pragma: no cover
    from http.client import HTTPException
    RemoteDisconnected = HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contract import (HEALTH_REDUCER, INGEST_REDUCER, ContractError, health_args, ingest_args)

SERVER_ALIASES = {
    "maincloud": "https://maincloud.spacetimedb.com",
    "local": "http://127.0.0.1:3000",
}
DELIVERED = ("accepted", "duplicate")
RETRY = ("retryable", "ambiguous")
# Reducer error text that means "you already sent this". Matched case-insensitively.
DUPLICATE_PATTERN = re.compile(
    r"(duplicate|already (exists|ingested|recorded|seen|processed)|replay|unique constraint|"
    r"stale seq|seq\b.*(<=|not greater|must (be )?increas|out of order|already))", re.I)
AUTH_PATTERN = re.compile(r"(not (an )?enrolled|enrolled member|unknown device|unauthori[sz]ed|forbidden|not allowed|"
                          r"permission|disabled device|identity)", re.I)


def resolve_server(server):
    server = (server or "").strip()
    return SERVER_ALIASES.get(server, server).rstrip("/")


def utc_now_iso():
    now = time.time()
    dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (int(now * 1000) % 1000)


class RawResult(NamedTuple):
    outcome: str                 # http | unreachable | ambiguous_network
    http_status: Optional[int]
    body: str                    # server text, truncated to 500 chars
    error: str
    latency_ms: float


class PublishReceipt(NamedTuple):
    event_id: str
    seq: int
    kind: str
    status: str                  # accepted | duplicate | rejected | retryable | ambiguous
    delivered: bool
    retry_eligible: bool
    http_status: Optional[int]
    server_result: str           # normalized server response text
    error: str
    reason: str                  # short machine reason: ok, auth, contract, reducer_error, duplicate, ...
    attempt: int
    attempted_at_utc: str
    latency_ms: float


def token_identity(token):
    """Best-effort read of the identity claim of a SpacetimeDB JWT (NOT verified; the server verifies).
    Used only to log which identity is publishing and to refuse forbidden (owner/admin) identities."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    ident = claims.get("hex_identity") or claims.get("identity")
    return ident.lower() if isinstance(ident, str) else None


class StdbClient(object):
    def __init__(self, server, database, token=None, timeout=5.0, urlopen_fn=urlopen):
        self.base = resolve_server(server)
        if not self.base.startswith(("http://", "https://")):
            raise ValueError("server must be maincloud, local, or an http(s) URL")
        self.database = database
        self.token = token
        self.timeout = float(timeout)
        self.urlopen = urlopen_fn

    def _request(self, path, body, content_type, max_body=500):
        req = Request(self.base + path, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        t0 = time.monotonic()
        try:
            resp = self.urlopen(req, timeout=self.timeout)
            try:
                text = resp.read().decode("utf-8", "replace")
                code = resp.status if hasattr(resp, "status") else resp.getcode()
            finally:
                resp.close()
            return RawResult("http", code, text[:max_body], "", (time.monotonic() - t0) * 1000.0)
        except HTTPError as err:
            try:
                text = err.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            finally:
                err.close()
            return RawResult("http", err.code, text[:max_body], "http_%d" % err.code,
                             (time.monotonic() - t0) * 1000.0)
        except URLError as err:
            # urllib wraps failures that happen while connecting/sending. The reducer can't have run.
            return RawResult("unreachable", None, "", "unreachable: %s" % _short(err.reason),
                             (time.monotonic() - t0) * 1000.0)
        except (socket.timeout, RemoteDisconnected, ConnectionResetError, HTTPException) as err:
            # Request was sent, response never arrived: the reducer may or may not have committed.
            return RawResult("ambiguous_network", None, "", "ambiguous: %s" % _short(err),
                             (time.monotonic() - t0) * 1000.0)
        except (ssl.SSLError, OSError) as err:
            if getattr(err, "errno", None) in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH):
                return RawResult("unreachable", None, "", "unreachable: %s" % _short(err),
                                 (time.monotonic() - t0) * 1000.0)
            return RawResult("ambiguous_network", None, "", "ambiguous: %s" % _short(err),
                             (time.monotonic() - t0) * 1000.0)

    def call(self, reducer, args):
        body = json.dumps(args, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return self._request("/v1/database/%s/call/%s" % (self.database, reducer), body, "application/json")

    def sql(self, query, max_body=2000000):
        """Read-only SQL over HTTP (as THIS identity, so my_* views show only what it may see).
        Returns RawResult; body holds the full JSON text (up to max_body chars)."""
        return self._request("/v1/database/%s/sql" % self.database, query.encode("utf-8"), "text/plain",
                             max_body=max_body)

    def sql_rows(self, query):
        """SQL -> (http_status, [ {column: value} ... ], error_text). Identity columns come back as hex."""
        raw = self.sql(query)
        if raw.http_status != 200:
            return raw.http_status, [], raw.body or raw.error
        try:
            doc = json.loads(raw.body)
        except ValueError as err:
            return raw.http_status, [], "unparsable sql response: %s" % err
        out = []
        for stmt in doc if isinstance(doc, list) else []:
            cols = [((e.get("name") or {}).get("some") if isinstance(e.get("name"), dict) else e.get("name"))
                    for e in stmt.get("schema", {}).get("elements", [])]
            for row in stmt.get("rows", []):
                out.append(dict(zip(cols, [_sats_plain(v) for v in row])))
        return raw.http_status, out, ""

    @staticmethod
    def mint_identity(server, timeout=10.0, urlopen_fn=urlopen):
        """POST /v1/identity -> (identity_hex, token). Creates a brand-new identity with no rights."""
        req = Request(resolve_server(server) + "/v1/identity", data=b"", method="POST")
        data = json.loads(urlopen_fn(req, timeout=timeout).read().decode("utf-8"))
        return data["identity"], data["token"]


def _sats_plain(value):
    """SATS-JSON -> plain JSON: Identity ["0x.."] / {"__identity__": ..} and 1-element wrappers flattened."""
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], (str, int)):
        return value[0]
    if isinstance(value, dict) and len(value) == 1:
        k, v = list(value.items())[0]
        if k.startswith("__") and k.endswith("__"):
            return v
    return value


def _short(err):
    text = "%s: %s" % (type(err).__name__, err)
    return text[:160]


def classify(raw, prior_ambiguous=False):
    """RawResult -> (status, reason). Pure function: the whole delivery policy is here."""
    if raw.outcome == "unreachable":
        return "retryable", "unreachable"
    if raw.outcome == "ambiguous_network":
        return "ambiguous", "timeout_or_reset_after_send"
    code = raw.http_status or 0
    if 200 <= code < 300:
        return "accepted", "ok"
    if code in (401, 403):
        return "rejected", "auth"
    if code in (408, 429, 503):
        return "retryable", "server_busy_%d" % code
    if code in (500, 502, 504):
        return "ambiguous", "server_error_%d" % code
    if DUPLICATE_PATTERN.search(raw.body or ""):
        return ("duplicate", "duplicate_after_ambiguous") if prior_ambiguous else ("rejected", "duplicate")
    if code == 530:  # SpacetimeDB: the reducer returned Err(...)
        return "rejected", "auth" if AUTH_PATTERN.search(raw.body or "") else "reducer_error"
    if code in (400, 404, 413, 415, 422):
        return "rejected", "contract_%d" % code
    if 400 <= code < 500:
        return "rejected", "client_error_%d" % code
    return "ambiguous", "unexpected_%d" % code


class SpacetimePublisher(object):
    """EdgePublisher for the deployed module: publish(event) -> PublishReceipt.

    Synchronous by design (stdlib urllib, Python 3.6, runs inside the outbox worker thread).
    """

    def __init__(self, client, contract):
        self.client = client
        self.contract = contract

    def publish(self, event, attempt=1, prior_ambiguous=False):
        try:
            args = ingest_args(event, self.contract)
        except ContractError as err:
            return PublishReceipt(event.event_id, event.seq, event.kind, "rejected", False, False, None, "",
                                  "contract: %s" % err, "local_contract", attempt, utc_now_iso(), 0.0)
        return self.publish_args(event.event_id, event.seq, event.kind, args, attempt, prior_ambiguous)

    def publish_args(self, event_id, seq, kind, args, attempt=1, prior_ambiguous=False):
        raw = self.client.call(INGEST_REDUCER, args)
        status, reason = classify(raw, prior_ambiguous)
        return PublishReceipt(event_id, seq, kind, status, status in DELIVERED, status in RETRY, raw.http_status,
                              (raw.body or "").strip()[:300], raw.error, reason, attempt, utc_now_iso(),
                              round(raw.latency_ms, 1))

    def report_health(self, workstation_id, device_id, component, status, detail):
        """Health is latest-state-wins: not queued, not retried. Returns (status, reason, RawResult)."""
        args = health_args(workstation_id, device_id, component, status, detail)
        raw = self.client.call(HEALTH_REDUCER, args)
        result, reason = classify(raw, False)
        return result, reason, raw


__all__ = ["StdbClient", "SpacetimePublisher", "PublishReceipt", "RawResult", "classify", "token_identity",
           "resolve_server", "DELIVERED", "RETRY"]
