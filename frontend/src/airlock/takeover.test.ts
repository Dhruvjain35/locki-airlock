import { describe, expect, it } from "vitest";
import type { MacLink } from "./macSecurity";
import { RESPONSE_ROWS, nativeStatus, readTakeover, responseTone, responseWord } from "./takeover";

const NOW = 1_762_600_000_000;
const link = (data: Record<string, unknown> | null, status: MacLink["status"] = "live"): MacLink => ({ status, data, error: null, at: NOW });
const record = { id: "usb:e1", live: true, rehearsal: false, trigger: "unknown_usb", detected_at_ms: NOW - 2000, fresh: true, local_mode: "CRITICAL", local_mode_verified: true,
  headline: "Unauthorized device activity detected", sentence: "An unknown USB device was connected while protection was armed.", sentence_source: "usb_only", evidence: [],
  response: { audio: { state: "played", detail: "" }, tools: { state: "not_configured", detail: "" }, lock: { state: "dry_run", detail: "" }, monitoring: { state: "running", detail: "" }, nemotron: { state: "available", detail: "" }, evidence: { state: "local_only", detail: "" } },
  incident_id: "local-1", acknowledged_at_ms: null, shown_at_ms: NOW - 1700, shown_latency_ms: 300 };

describe("readTakeover", () => {
  it("shows nothing as current while this Mac's backend can't be read", () => {
    expect(readTakeover(link({ takeover: { armed: true, takeover: record } }, "offline"))).toBeNull();
  });
  it("tells an old backend apart from 'nothing happening'", () => {
    const t = readTakeover(link({ armed: true }));
    expect(t?.supported).toBe(false);
    expect(t?.record).toBeNull();
    expect(nativeStatus(t, NOW).label).toBe("Not supported");
  });
  it("passes the backend's record through untouched", () => {
    const t = readTakeover(link({ takeover: { armed: true, session_id: "s1", takeover: record, native: { connected: true, last_poll_ms: NOW - 200 }, handoff: null } }));
    expect(t?.record?.id).toBe("usb:e1");
    expect(t?.sessionId).toBe("s1");
  });
  it("has no record after a disarm", () => {
    expect(readTakeover(link({ takeover: { armed: false, session_id: null, takeover: null, native: { connected: true, last_poll_ms: NOW } } }))?.record).toBeNull();
  });
});

describe("response wording", () => {
  it("only says locked when the lock adapter confirmed it", () => {
    const states = ["not_locked", "dry_run", "requested", "executed", "refused", "failed"];
    for (const s of states) expect(responseWord(s).toLowerCase()).not.toMatch(/^locked/);
    expect(responseWord("confirmed")).toBe("Locked (confirmed)");
  });
  it("never claims containment, isolation or blocking", () => {
    const words = ["played", "failed", "muted", "skipped", "pending", "unknown", "frozen", "not_frozen", "not_configured", "not_locked", "dry_run", "requested", "executed", "confirmed",
      "refused", "running", "degraded", "stopped", "investigating", "answered", "available", "unavailable", "recorded", "local_only"].map(responseWord).join(" ").toLowerCase();
    for (const banned of ["contained", "isolated", "blocked", "secured"]) expect(words).not.toContain(banned);
  });
  it("a dry run and a failure are never shown as success", () => {
    expect(responseTone("dry_run")).toBe("warn");
    expect(responseTone("failed")).toBe("alert");
    expect(responseTone("muted")).toBe("warn");
    expect(responseTone("not_frozen")).not.toBe("ok");
  });
  it("covers all six response lines", () => {
    expect(RESPONSE_ROWS.map(r => r.key)).toEqual(["audio", "lock", "tools", "monitoring", "nemotron", "evidence"]);
  });
});

describe("nativeStatus", () => {
  it("says degraded when the alert window isn't polling", () => {
    const s = nativeStatus(readTakeover(link({ takeover: { armed: true, native: { connected: false, last_poll_ms: null } } })), NOW);
    expect(s.tone).toBe("warn");
    expect(s.detail).toContain("degraded");
  });
  it("is unknown, not fine, while the backend is unreachable", () => {
    expect(nativeStatus(null, NOW).tone).toBe("muted");
  });
});
