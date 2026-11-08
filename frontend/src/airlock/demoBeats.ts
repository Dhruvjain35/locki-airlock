// The guided demo: one beat per product feature, in the order a presenter shows them. A beat is lit only by state the Mac backend
// (GET /api/security, GET /api/security/demo) or SpacetimeDB already reports. Nothing here makes an event happen or claims one.
import type { DemoSnap, Trial } from "../demo/demoApi";
import { type StatusLine, type Tone, lockStatus, warningAudio } from "./containment";
import { type MacLink, reportedStage } from "./macSecurity";
import type { Takeover } from "./takeover";

export type BeatId = "face_enroll" | "face_owner" | "face_stranger" | "arm" | "presence" | "usb" | "owner_return" | "stop";
export type BeatState = "done" | "now" | "waiting" | "off" | "failed";
export interface Beat { id: BeatId; feature: string; title: string; cue: string; state: BeatState; detail: string; tone: Tone }

export interface DemoInputs {
  mac: MacLink;
  /** The freshest demo snapshot: GET /api/security/demo while the panel polls it, else the copy inside GET /api/security. */
  demo: DemoSnap | null;
  /** A live protected session exists in SpacetimeDB. */
  armed: boolean;
  /** The live session's id, so a takeover from another session never counts. */
  sessionId?: string;
  takeover: Takeover | null;
  /** Automatic screen lock for the live session (or, before arming, the next session). Off means no lock is ever requested. */
  autoLock?: boolean;
}

const FACE_OFF = "Face ID is off on this Mac. The backend runs without face identification.";

/** The newest impostor trial: the face test run on someone who is not the owner. */
export function strangerTrial(demo: DemoSnap | null): Trial | undefined {
  return (demo?.trials ?? []).filter(t => t.label === "impostor").slice(-1)[0];
}

function faceEnroll(d: DemoSnap | null, face: boolean): Pick<Beat, "state" | "detail" | "tone"> {
  if (!face) return { state: "off", detail: FACE_OFF, tone: "muted" };
  if (d?.enrolled) return { state: "done", detail: `Owner face enrolled from ${d.enrolled_samples ?? "?"} camera samples, for this demo only.`, tone: "ok" };
  const e = d?.enrollment;
  if (d?.enrolling && e) return { state: "now", detail: `${e.accepted} of ${e.needed} samples · ${e.last_problem || e.hint}`, tone: "warn" };
  if (e?.error) return { state: "failed", detail: e.error, tone: "alert" };
  return { state: "waiting", detail: "Press Enroll owner face, then look at the camera: centre, left, right.", tone: "muted" };
}

function faceOwner(d: DemoSnap | null, face: boolean): Pick<Beat, "state" | "detail" | "tone"> {
  if (!face) return { state: "off", detail: FACE_OFF, tone: "muted" };
  const vs = d?.visual_state;
  // The live confidence belongs to whoever is in frame now, so it is quoted only while that face is the owner.
  const conf = vs?.face_identity_state === "owner" && vs.identity_confidence != null ? ` · agreement ${vs.identity_confidence.toFixed(2)}` : "";
  if (d?.recognized || d?.steps?.recognized === "done") return { state: "done", detail: `Matched the enrolled owner${conf}.`, tone: "ok" };
  if (vs?.face_identity_state === "uncertain") return { state: "waiting", detail: `Face seen, not yet certain${conf}.`, tone: "warn" };
  return { state: "waiting", detail: "The owner sits at the desk facing the Mac camera.", tone: "muted" };
}

function faceStranger(d: DemoSnap | null, face: boolean): Pick<Beat, "state" | "detail" | "tone"> {
  if (!face) return { state: "off", detail: FACE_OFF, tone: "muted" };
  const t = strangerTrial(d);
  if (!t) return { state: "waiting", detail: "Press Test a second person, then someone else sits down.", tone: "muted" };
  const who = t.participant || "Second person";
  if (t.state === "running") return { state: "waiting", detail: `${who}: running the face test now…`, tone: "warn" };
  const ms = t.identity_ms != null ? ` in ${Math.round(t.identity_ms)} ms` : "";
  if (t.outcome === "unrecognized") return { state: "done", detail: `${who}: not the owner. Second face check rejected the match${ms}.`, tone: "ok" };
  if (t.outcome === "owner") return { state: "failed", detail: `${who} was matched as the owner. That is a false match; re-enroll and test again.`, tone: "alert" };
  if (t.outcome === "uncertain") return { state: "failed", detail: `${who}: the face test stayed uncertain. Face the camera and test again.`, tone: "warn" };
  return { state: "failed", detail: `${who}: no face reached the camera. Test again.`, tone: "warn" };
}

/** Short, honest words for the response lines: a dry run never reads as a lock, a queued warning never reads as played. */
export function voiceWords(v: StatusLine): string {
  return v.label === "Played" ? `voice played${v.detail && v.detail !== "Reached the speaker." ? ` on ${v.detail.replace(/\.$/, "")}` : ""}` : v.label === "None yet" ? "no voice warning yet" : `voice ${v.label.toLowerCase()}`;
}
export function lockWords(l: StatusLine, mac: MacLink, autoLock?: boolean): string {
  if (l.label === "Dry run") return "lock requested, dry run (screen not locked)";
  if (l.label === "Not locked") return autoLock === false ? "auto-lock off, no lock requested" : `no lock requested${mac.data?.lock_mode === "dry_run" ? " (dry run mode)" : ""}`;
  return `screen lock ${l.label.toLowerCase()}`;
}

/** Every beat of the demo, in order, with the first unfinished one marked "now". */
export function demoBeats(p: DemoInputs): Beat[] {
  const d = p.demo;
  const face = d?.face_identification_enabled === true;
  const stage = reportedStage(p.mac);
  const steps = d?.steps ?? {};
  const rec = p.takeover?.record && p.sessionId && p.takeover.sessionId === p.sessionId ? p.takeover.record : null;
  const voice = warningAudio(p.mac);
  const lock = lockStatus(p.mac);

  const presenceDone = steps.monitor === "done" || (p.armed && (stage === "WATCHFUL" || stage === "DEFENSIVE" || stage === "CRITICAL"));
  const usbDone = steps.incident === "done" || (p.armed && stage === "CRITICAL") || Boolean(rec?.live);
  const rehearsal = d?.mode === "rehearsal";
  const alertLine = !rec ? "Alert window: waiting for a critical event"
    : !rec.live ? "Alert window shown with its REHEARSAL banner"
    : rec.shown_latency_ms != null ? `Alert window on screen ${Math.round(rec.shown_latency_ms)} ms after detection`
    : p.takeover?.native?.connected ? "Alert window: decided, waiting for the window" : "Alert window not running; the dashboard shows the alert";
  const returned = steps.owner_return === "done" || stage === "OWNER_RETURNED";
  const stopped = steps.disarm === "done" && !p.armed;

  const beats: Beat[] = [
    { id: "face_enroll", feature: "Face recognition", title: "Enroll the owner", cue: "Owner looks at the Mac camera", ...faceEnroll(d, face) },
    { id: "face_owner", feature: "Face recognition", title: "Recognize the owner", cue: "Owner sits at the desk", ...faceOwner(d, face) },
    { id: "face_stranger", feature: "Face recognition", title: "Reject a second person", cue: "Someone else sits down", ...faceStranger(d, face) },
    { id: "arm", feature: "Protection", title: "Arm the workstation", cue: "Press Start protection",
      state: p.armed || steps.arm === "done" ? "done" : "waiting", tone: p.armed ? "ok" : "muted",
      detail: p.armed ? "Live session recorded in SpacetimeDB." : "Owner arms from this console." },
    { id: "presence", feature: "Voice + dashboard", title: "Someone else at the desk", cue: face ? "A second person walks up to the desk" : "Walk up to the desk",
      state: presenceDone ? "done" : "waiting", tone: presenceDone ? "ok" : "muted",
      detail: presenceDone ? `Watchful on the dashboard · ${voiceWords(voice)}` : "The Jetson camera sees someone; the mode moves to Watchful and LOCKI speaks." },
    { id: "usb", feature: "Alert + lock", title: "Unknown USB device", cue: "Plug in a USB stick LOCKI hasn't seen",
      state: usbDone ? "done" : "waiting", tone: usbDone ? "ok" : "muted",
      detail: usbDone ? `${rehearsal ? "Rehearsal fixture, not a real device · " : ""}Critical · ${alertLine} · ${lockWords(lock, p.mac, p.autoLock)} · ${voiceWords(voice)}`
        : `The mode goes Critical, the alert window covers the screen and LOCKI speaks.${p.autoLock === false ? " Turn on Automatic screen lock before arming to show the lock (dry run)." : ""}` },
    { id: "owner_return", feature: "Face recognition", title: "Owner returns", cue: "Owner sits back down, facing the camera",
      ...(!face ? { state: "off" as const, detail: FACE_OFF, tone: "muted" as const }
        : returned ? { state: "done" as const, detail: "Owner's face matched: Owner returned.", tone: "ok" as const }
        : { state: "waiting" as const, detail: "LOCKI matches the owner's face and stands down.", tone: "muted" as const }) },
    { id: "stop", feature: "Protection", title: "Stop protection", cue: "Press Stop protection",
      state: stopped ? "done" : "waiting", tone: stopped ? "ok" : "muted",
      detail: stopped ? "Disarmed. Every record is kept." : "The owner disarms; evidence stays in SpacetimeDB." },
  ];
  const next = beats.findIndex(b => b.state === "waiting" || b.state === "failed");
  if (next >= 0 && beats[next].state === "waiting") beats[next] = { ...beats[next], state: "now" };
  return beats;
}

/** The demo snapshot to read: the panel's own fresher poll when it has one, else the copy inside GET /api/security. */
export function demoSnapshot(mac: MacLink, polled: DemoSnap | null): DemoSnap | null {
  if (polled && typeof polled === "object" && "steps" in polled) return polled;
  const d = mac.status === "live" ? mac.data?.demo : null;
  return d && typeof d === "object" && "steps" in d ? d as DemoSnap : null;
}

/** POST /demo/end answers {active:false,...}, not a full snapshot; only a full snapshot may replace the one shown. */
export function isFullSnapshot(x: unknown): x is DemoSnap {
  return Boolean(x && typeof x === "object" && "steps" in x && "visual_state" in x);
}
