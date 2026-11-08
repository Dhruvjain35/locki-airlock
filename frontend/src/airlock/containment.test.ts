import { describe, expect, it } from "vitest";
import type { MacLink } from "./macSecurity";
import { MAC_CRITICAL_FRESH_MS, TAKEOVER_FRESH_MS, containmentStatement, containmentTrigger, flowStep, gateStatus, lockStatus, warningAudio } from "./containment";

const NOW = 1_794_150_000_000;
const S = "session-live";
const W = "dhruv-m4-air";
const mac = (data: any, status: MacLink["status"] = "live"): MacLink => ({ status, data, error: null, at: NOW });
const idleMac = mac({ session: { session_id: S, armed_at_ms: NOW - 600_000 }, visual_state: { current_security_mode: "WATCHFUL", mode_since: NOW - 5000 } });
const incident = (over: Record<string, unknown> = {}) => ({ id: "incident:abc", workstationId: W, sessionId: S, state: "open", ruleId: "correlated_usb_physical", simulated: false, createdAtMs: BigInt(NOW - 20_000), summary: "s", ...over });
const none = new Set<string>();
const trig = (inc: any, m: MacLink = idleMac, dismissed: ReadonlySet<string> = none, live: string | undefined = S) => containmentTrigger({ incident: inc, liveSessionId: live, stationId: W, mac: m, now: NOW, dismissed });

describe("containment takeover", () => {
  it("opens for a fresh real incident of the live session", () => {
    expect(trig(incident())).toMatchObject({ key: "incident:abc", reason: "incident" });
  });
  it("never opens for a person alone (WATCHFUL, no incident)", () => {
    expect(trig(undefined)).toBeNull();
  });
  it("never opens for synthetic, old, other-session, other-workstation, resolved or owner-panic incidents", () => {
    expect(trig(incident({ simulated: true }))).toBeNull();
    expect(trig(incident({ createdAtMs: BigInt(NOW - TAKEOVER_FRESH_MS - 1) }))).toBeNull();
    expect(trig(incident({ sessionId: "session-old" }))).toBeNull();
    expect(trig(incident({ workstationId: "desk-b" }))).toBeNull();
    expect(trig(incident({ state: "resolved" }))).toBeNull();
    expect(trig(incident({ ruleId: "owner_panic" }))).toBeNull();
    expect(containmentTrigger({ incident: incident(), liveSessionId: undefined, stationId: W, mac: idleMac, now: NOW, dismissed: none })).toBeNull();
  });
  it("stays dismissed across reloads and reconnects, and the Mac's CRITICAL for the same incident doesn't reopen it", () => {
    const critical = mac({ session: { session_id: S }, visual_state: { current_security_mode: "CRITICAL", mode_since: NOW - 10_000 } });
    expect(trig(incident(), critical, new Set(["incident:abc"]))).toBeNull();
  });
  it("opens on this Mac's own unknown-USB CRITICAL before the database row arrives, only while fresh and in this session", () => {
    const critical = (since: number, session = S) => mac({ session: { session_id: session }, visual_state: { current_security_mode: "CRITICAL", mode_since: since } });
    expect(trig(undefined, critical(NOW - 3000))).toMatchObject({ reason: "mac_critical" });
    expect(trig(undefined, critical((NOW - 3000) / 1000))).toMatchObject({ reason: "mac_critical" });  // seconds or ms
    expect(trig(undefined, critical(NOW - MAC_CRITICAL_FRESH_MS - 1))).toBeNull();
    expect(trig(undefined, critical(NOW - 3000, "session-old"))).toBeNull();
    expect(trig(undefined, mac({ session: { session_id: S }, visual_state: { current_security_mode: "CRITICAL", mode_since: NOW - 3000 } }, "offline"))).toBeNull();
    const k = trig(undefined, critical(NOW - 3000))!.key;
    expect(trig(undefined, critical(NOW - 3000), new Set([k]))).toBeNull();
  });
});

describe("containment wording follows the linked evidence only", () => {
  const ev = (kind: string) => ({ kind });
  it("names a person only when a person event is linked", () => {
    expect(containmentStatement([ev("person_in_zone"), ev("usb_added")], incident(), idleMac)).toBe("Unknown USB device connected while a person was detected at the workstation.");
    expect(containmentStatement([ev("usb_added")], incident(), idleMac)).toBe("Unknown USB device connected to this workstation while protection was on.");
    expect(containmentStatement([ev("usb_added")], incident(), idleMac)).not.toMatch(/person/);
  });
  it("never calls a person alone an intrusion", () => {
    expect(containmentStatement([ev("person_in_zone")], undefined, idleMac)).not.toMatch(/intru|USB/i);
  });
});

describe("status lines never claim more than the backend reports", () => {
  it("separates not locked, dry run, requested, executed, OS-confirmed and failed", () => {
    const base = (locks: any[], mode = "dry_run") => mac({ lock_mode: mode, session: { session_id: S }, locks });
    expect(lockStatus(base([])).label).toBe("Not locked");
    expect(lockStatus(base([{ session_id: S, status: "LOCK_REQUESTED", dry_run: true }])).label).toBe("Dry run");
    expect(lockStatus(base([{ session_id: S, status: "LOCK_REQUESTED", dry_run: false }], "real")).label).toBe("Requested");
    expect(lockStatus(base([{ session_id: S, status: "LOCK_EXECUTED", dry_run: false }], "real")).label).toBe("Lock executed");
    expect(lockStatus(base([{ session_id: S, status: "LOCK_CONFIRMED", dry_run: false }], "real")).label).toBe("Locked · OS confirmed");
    expect(lockStatus(base([{ session_id: S, status: "LOCK_FAILED", dry_run: false }], "real")).tone).toBe("alert");
    expect(lockStatus(base([{ session_id: "other", status: "LOCK_CONFIRMED", dry_run: false }], "real")).label).toBe("Not locked");
    expect(lockStatus(mac(null, "offline")).label).toBe("Unverified");
  });
  it("reports a protected-tool freeze only when the gate itself says frozen", () => {
    expect(gateStatus(mac({ gate: { tools: ["read_file"], frozen: null }, session: { policy: { freeze_protected_tools: false } } })).label).toBe("Not enabled");
    expect(gateStatus(mac({ gate: { tools: ["read_file"], frozen: null }, session: { policy: { freeze_protected_tools: true } } })).label).toBe("Not enabled");
    expect(gateStatus(mac({ gate: { tools: ["read_file"], frozen: "incident:abc" } })).label).toBe("Frozen");
    expect(gateStatus(mac(null, "offline")).label).toBe("Unverified");
  });
  it("reports warning audio from the playback receipt", () => {
    const m = (w: any) => mac({ session: { armed_at_ms: NOW - 1000 }, warnings: [w] });
    expect(warningAudio(m({ at: NOW / 1000, spoken: true, audio: { outcome: "finished", source: "kokoro_cache", source_label: "Local Kokoro voice" } })).label).toBe("Played");
    expect(warningAudio(m({ at: NOW / 1000, spoken: true, audio: { outcome: "finished", source: "text_only", source_label: "Text only" } }))).toMatchObject({ label: "Not played", tone: "alert", detail: "Text only" });
    expect(warningAudio(m({ at: NOW / 1000, spoken: true, played: false, audio: { outcome: "finished", source: "kokoro_live" } })).label).toBe("Not played");
    expect(warningAudio(m({ at: NOW / 1000, spoken: true })).label).toBe("Queued");
    expect(warningAudio(m({ at: (NOW - 5000) / 1000, spoken: true, audio: { outcome: "finished" } })).label).toBe("None yet");
  });
  it("a skipped repeat never hides the warning that played", () => {
    const played = { at: (NOW - 4000) / 1000, spoken: true, played: true, audio: { outcome: "finished", source: "kokoro_cache", source_label: "Local Kokoro voice" } };
    const skipped = { at: (NOW - 1000) / 1000, reason: "person_at_desk", skipped: true };
    const m2 = mac({ session: { session_id: S, armed_at_ms: NOW - 10_000 }, warnings: [played, skipped] });
    expect(warningAudio(m2).label).toBe("Played");
    expect(warningAudio(mac({ session: { session_id: S, armed_at_ms: NOW - 10_000 }, warnings: [skipped] })).label).toBe("None yet");
  });
});

describe("Protect → Detect → Contain → Explain → Recover", () => {
  it("follows the real run", () => {
    expect(flowStep({ armed: false, stage: null, incident: false, explained: false, stoppedAfterIncident: false, sessionEvents: 0 })).toBeNull();
    expect(flowStep({ armed: true, stage: "NORMAL", incident: false, explained: false, stoppedAfterIncident: false, sessionEvents: 0 })).toBe("protect");
    expect(flowStep({ armed: true, stage: "WATCHFUL", incident: false, explained: false, stoppedAfterIncident: false, sessionEvents: 1 })).toBe("detect");
    expect(flowStep({ armed: true, stage: "CRITICAL", incident: true, explained: false, stoppedAfterIncident: false, sessionEvents: 2 })).toBe("contain");
    expect(flowStep({ armed: true, stage: "CRITICAL", incident: true, explained: true, stoppedAfterIncident: false, sessionEvents: 2 })).toBe("explain");
    expect(flowStep({ armed: false, stage: "NORMAL", incident: false, explained: false, stoppedAfterIncident: true, sessionEvents: 0 })).toBe("recover");
  });
});
