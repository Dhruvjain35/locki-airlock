import { describe, expect, it } from "vitest";
import type { DemoSnap, Trial } from "../demo/demoApi";
import { demoBeats, demoSnapshot, isFullSnapshot, strangerTrial } from "./demoBeats";
import type { MacLink } from "./macSecurity";
import type { Takeover } from "./takeover";

const NOW = 1_794_150_000_000;
const S = "session-live";
const mac = (data: any, status: MacLink["status"] = "live"): MacLink => ({ status, data, error: null, at: NOW });
const vs = (over: Record<string, unknown> = {}) => ({ current_security_mode: "NORMAL", face_identity_state: null, identity_confidence: null, mode_since: NOW, ...over });
const snap = (over: Partial<DemoSnap> = {}): DemoSnap => ({
  face_identification_enabled: true, active: true, id: "d1", mode: "live", label: null, started_at: NOW / 1000, enrolling: false, enrollment: null,
  enrolled: false, enrolled_samples: null, recognized: false, template: null, thresholds: null, steps: {}, readiness: {}, timings: {}, history: {},
  timeline: [], fixtures: [], trials: [], trial_summary: {} as DemoSnap["trial_summary"], error: null, visual_state: vs() as unknown as DemoSnap["visual_state"],
  armed: false, arming: "database", camera_running: true, preview: "", ...over,
});
const trial = (over: Partial<Trial> = {}): Trial => ({ n: 1, label: "impostor", participant: "Sid", state: "done", outcome: "unrecognized", identity_ms: 412, presence_ms: 300, agreement: 0.21, ...over });
const beats = (p: { demo?: DemoSnap | null; data?: any; armed?: boolean; takeover?: Takeover | null; sessionId?: string; autoLock?: boolean }) =>
  demoBeats({ autoLock: p.autoLock, mac: mac({ session: { session_id: S, armed_at_ms: NOW - 60_000 }, visual_state: vs(), warnings: [], locks: [], lock_mode: "dry_run", ...p.data }), demo: p.demo ?? null, armed: p.armed ?? false, sessionId: p.sessionId ?? (p.armed ? S : undefined), takeover: p.takeover ?? null });
const byId = (b: ReturnType<typeof beats>) => Object.fromEntries(b.map(x => [x.id, x]));

describe("demo beats", () => {
  it("marks the face beats off when face ID is off, and makes arming the next thing to do", () => {
    const b = byId(beats({ demo: snap({ face_identification_enabled: false }) }));
    expect([b.face_enroll.state, b.face_owner.state, b.face_stranger.state, b.owner_return.state]).toEqual(["off", "off", "off", "off"]);
    expect(b.arm.state).toBe("now");
  });
  it("starts with enrollment when face ID is on, and shows live enrollment progress", () => {
    expect(byId(beats({ demo: snap() })).face_enroll.state).toBe("now");
    const enrolling = byId(beats({ demo: snap({ enrolling: true, enrollment: { accepted: 6, needed: 12, poses: {}, hint: "Turn left", last_problem: null, rejected: {}, complete: false } }) }));
    expect(enrolling.face_enroll).toMatchObject({ state: "now", detail: "6 of 12 samples · Turn left" });
  });
  it("lights owner recognition only from the backend's own match", () => {
    expect(byId(beats({ demo: snap({ enrolled: true, enrolled_samples: 12 }) })).face_owner.state).toBe("now");
    expect(byId(beats({ demo: snap({ enrolled: true, recognized: true }) })).face_owner.state).toBe("done");
    // A stranger's low score in frame now is never quoted as the owner's agreement.
    const stranger = snap({ enrolled: true, recognized: true, visual_state: vs({ face_identity_state: "unrecognized", identity_confidence: 0.18 }) as unknown as DemoSnap["visual_state"] });
    expect(byId(beats({ demo: stranger })).face_owner.detail).not.toContain("0.18");
  });
  it("counts a second person only when the impostor face test rejected them", () => {
    const base = { enrolled: true, recognized: true };
    expect(byId(beats({ demo: snap({ ...base, trials: [trial()] }) })).face_stranger).toMatchObject({ state: "done", tone: "ok" });
    expect(byId(beats({ demo: snap({ ...base, trials: [trial({ outcome: "owner" })] }) })).face_stranger).toMatchObject({ state: "failed", tone: "alert" });
    expect(byId(beats({ demo: snap({ ...base, trials: [trial({ state: "running", outcome: null })] }) })).face_stranger.state).toBe("now");
    // A genuine trial is the owner, never the second person.
    expect(byId(beats({ demo: snap({ ...base, trials: [trial({ label: "genuine", outcome: "owner" })] }) })).face_stranger.state).toBe("now");
  });
  it("needs a live session for presence and USB, and reads them from the backend's mode", () => {
    const off = byId(beats({ demo: snap({ face_identification_enabled: false }), data: { visual_state: vs({ current_security_mode: "WATCHFUL" }) } }));
    expect(off.presence.state).not.toBe("done");
    const watch = byId(beats({ demo: snap({ face_identification_enabled: false }), armed: true, data: { visual_state: vs({ current_security_mode: "WATCHFUL" }) } }));
    expect(watch.presence.state).toBe("done");
    expect(watch.usb.state).toBe("now");
    const crit = byId(beats({ demo: snap({ face_identification_enabled: false, steps: { monitor: "done" } }), armed: true, data: { visual_state: vs({ current_security_mode: "CRITICAL" }) } }));
    expect(crit.usb.state).toBe("done");
    expect(crit.usb.detail).toContain("no lock requested (dry run mode)");
    expect(crit.usb.detail).not.toMatch(/Locked|screen locked/);
  });
  it("never counts another session's takeover", () => {
    const record = { id: "usb:e1", live: true, shown_latency_ms: 140 } as any;
    const other: Takeover = { supported: true, armed: true, sessionId: "session-old", record, native: { connected: true, last_poll_ms: NOW }, handoff: null };
    expect(byId(beats({ demo: snap({ face_identification_enabled: false }), armed: true, takeover: other })).usb.state).not.toBe("done");
    const mine: Takeover = { ...other, sessionId: S };
    expect(byId(beats({ demo: snap({ face_identification_enabled: false }), armed: true, takeover: mine })).usb.detail).toContain("140 ms");
  });
  it("finishes only after the owner disarms", () => {
    const b = byId(beats({ demo: snap({ face_identification_enabled: false, steps: { arm: "done", monitor: "done", incident: "done", disarm: "done" } }), armed: false }));
    expect(b.stop.state).toBe("done");
    expect(Object.values(b).filter(x => x.state === "now")).toHaveLength(0);
  });
});

describe("demo beats: lock and rehearsal wording", () => {
  const crit = (over: Record<string, unknown>, demo: Partial<DemoSnap> = {}, autoLock?: boolean) => byId(beats({ autoLock, armed: true,
    demo: snap({ face_identification_enabled: false, ...demo }), data: { visual_state: vs({ current_security_mode: "CRITICAL" }), ...over } }));
  it("says dry run only when a lock was requested and recorded as a dry run", () => {
    expect(crit({ locks: [{ session_id: S, status: "LOCK_DRY_RUN", dry_run: true }] }).usb.detail).toContain("lock requested, dry run (screen not locked)");
    expect(crit({}, {}, false).usb.detail).toContain("auto-lock off, no lock requested");
  });
  it("tells the presenter to turn on auto-lock before arming when it is off", () => {
    expect(byId(beats({ demo: snap({ face_identification_enabled: false }), armed: true, autoLock: false })).usb.detail).toContain("Turn on Automatic screen lock");
  });
  it("labels a rehearsal fixture as a fixture, never as a real device", () => {
    const rec = { id: "usb:fixture", live: false, rehearsal: true, shown_latency_ms: 90 } as any;
    const takeover: Takeover = { supported: true, armed: true, sessionId: S, record: rec, native: { connected: true, last_poll_ms: NOW }, handoff: null };
    const b = byId(beats({ armed: true, takeover, demo: snap({ face_identification_enabled: false, mode: "rehearsal" }), data: { visual_state: vs({ current_security_mode: "CRITICAL" }) } }));
    expect(b.usb.detail).toMatch(/^Rehearsal fixture, not a real device/);
    expect(b.usb.detail).toContain("REHEARSAL banner");
  });
});

describe("demo snapshot", () => {
  it("prefers the panel's own poll, falls back to the copy in GET /api/security, and ignores the end reply", () => {
    const polled = snap({ id: "polled" });
    const copy = snap({ id: "copy" });
    expect(demoSnapshot(mac({ demo: copy }), polled)?.id).toBe("polled");
    expect(demoSnapshot(mac({ demo: copy }), null)?.id).toBe("copy");
    expect(demoSnapshot(mac({ demo: copy }, "offline"), null)).toBeNull();
    expect(isFullSnapshot({ active: false, ended: "d1" })).toBe(false);
    expect(isFullSnapshot(polled)).toBe(true);
  });
  it("picks the newest impostor trial", () => {
    expect(strangerTrial(snap({ trials: [trial({ n: 1 }), trial({ n: 2, label: "genuine" }), trial({ n: 3 })] }))?.n).toBe(3);
  });
});
