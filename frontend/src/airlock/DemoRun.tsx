// The guided demo panel: starts the backend's own demo engine (backend/app/security/demo.py) and walks the presenter through every
// feature, one beat at a time. Each beat is lit only by real state (demoBeats.ts). Arming and stopping use the console's own owner
// flows; the panel never arms, locks or raises an incident by itself.
import { useEffect, useState } from "react";
import { CameraOff, Check, CircleDashed, FlaskConical, LoaderCircle, Play, ScanFace, ShieldCheck, ShieldOff, Square, Usb, UserX, X } from "lucide-react";
import { demoApi, type DemoSnap } from "../demo/demoApi";
import type { ConsoleActions, ConsoleModel } from "./Console";
import { type Beat, demoBeats, demoSnapshot, isFullSnapshot, strangerTrial } from "./demoBeats";
import { readTakeover } from "./takeover";

const POLL_MS = 500;

export function DemoRun({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  const [polled, setPolled] = useState<DemoSnap | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [guest, setGuest] = useState("Guest");
  const [camBroken, setCamBroken] = useState(false);

  // Enrollment progress and face trials move faster than the console's 1 Hz security poll, so the open panel reads the demo route twice
  // a second. If it fails, the copy inside GET /api/security still carries the demo.
  useEffect(() => {
    let stop = false;
    let timer = 0;
    const load = async () => {
      try { const s = await demoApi.get(); if (!stop && isFullSnapshot(s)) setPolled(s); } catch { /* fall back to the 1 Hz copy */ }
      finally { if (!stop) timer = window.setTimeout(load, POLL_MS); }
    };
    void load();
    return () => { stop = true; window.clearTimeout(timer); };
  }, []);

  const demo = demoSnapshot(m.mac, polled);
  const active = Boolean(demo?.active);
  const face = demo?.face_identification_enabled === true;
  const takeover = readTakeover(m.mac);
  const autoLock = Boolean(m.live ? m.live.autoLockEnabled : m.station?.autoLockEnabled);
  const beats = demoBeats({ mac: m.mac, demo, armed: Boolean(m.live), sessionId: m.live ? String(m.live.id) : undefined, takeover, autoLock });
  const shown = beats.filter(b => b.state === "done").length;
  const counted = beats.filter(b => b.state !== "off").length;
  const trial = strangerTrial(demo);
  const rehearsal = demo?.mode === "rehearsal";
  useEffect(() => { setCamBroken(false); }, [demo?.started_at, demo?.camera_running]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    try {
      const r = await fn();
      // POST /demo/end answers {active:false} rather than a snapshot; the next poll brings the real one.
      setPolled(isFullSnapshot(r) ? r : null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  // A start that fails (a missing model, a busy camera) still leaves the backend's run set (demo.py:104-115), and every later start is then
  // refused as "already running". Clear that half-started run at once, unless the refusal was about a demo that really is running.
  const startDemo = async (mode: "live" | "rehearsal") => {
    try { return await demoApi.start(mode); }
    catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (!/already running/i.test(msg)) await demoApi.end().catch(() => undefined);
      throw new Error(`The demo didn't start: ${msg}`);
    }
  };
  const spin = (label: string, icon: JSX.Element) => busy === label ? <LoaderCircle size={16} className="lk-spin" /> : icon;

  return <section className={`lk-card lk-demo${active ? " is-active" : ""}${rehearsal ? " is-rehearsal" : ""}`} aria-label="Guided demo">
    <header className="lk-demo-head">
      <div className="lk-demo-title">
        <span className="lk-demo-badge">{!active ? "Demo" : rehearsal ? "Rehearsal · fixtures are labelled" : "Live demo · real sensors"}</span>
        <h2>{active ? <>{shown}<span> of {counted} features shown</span></> : "Show every LOCKI feature, live"}</h2>
      </div>
      <div className="lk-demo-actions">
        {!active ? <>
          <button className="lk-btn lk-btn-primary" disabled={Boolean(busy)} onClick={() => void act("live", () => startDemo("live"))}>{spin("live", <Play size={16} />)}Start live demo</button>
          <button className="lk-btn" disabled={Boolean(busy)} onClick={() => void act("rehearsal", () => startDemo("rehearsal"))}>{spin("rehearsal", <FlaskConical size={16} />)}Rehearsal</button>
        </> : <>
          {face && <button className="lk-btn" disabled={Boolean(busy) || demo?.enrolling} onClick={() => void act("enroll", demoApi.enroll)}>{spin("enroll", <ScanFace size={16} />)}{demo?.enrolled ? "Re-enroll owner" : "Enroll owner face"}</button>}
          {face && <span className="lk-demo-guest">
            <input value={guest} maxLength={16} onChange={e => setGuest(e.target.value)} aria-label="Second person's name" />
            <button className="lk-btn" disabled={Boolean(busy) || !demo?.enrolled || trial?.state === "running"} title={demo?.enrolled ? "Runs the face test on the next person who sits down" : "Enroll the owner first"}
              onClick={() => void act("trial", () => demoApi.trial("impostor", guest.trim() || "Guest"))}>{spin("trial", <UserX size={16} />)}Test a second person</button>
          </span>}
          {rehearsal && <button className="lk-btn" disabled={Boolean(busy)} onClick={() => void act("usb", demoApi.fixtureUsb)}>{spin("usb", <Usb size={16} />)}Fixture USB</button>}
          {!m.live ? <button className="lk-btn lk-btn-primary" disabled={m.pending || !m.station || !m.owner} title={m.owner ? undefined : "Only the workstation owner can arm"} onClick={a.start}><ShieldCheck size={16} />Start protection</button>
            : <button className="lk-btn" disabled={m.pending} onClick={a.stop}><ShieldOff size={16} />Stop protection</button>}
          <button className="lk-btn lk-btn-ghost" disabled={Boolean(busy)} onClick={() => void act("end", demoApi.end)}>{spin("end", <Square size={14} />)}End demo</button>
        </>}
        <button className="lk-icon-button" aria-label="Close the demo panel" title="Close (the demo keeps running until you end it)" onClick={a.closeDemo}><X size={16} /></button>
      </div>
    </header>
    {(error || demo?.error) && <p className="lk-demo-error" role="alert">{error || demo?.error}</p>}
    {!face && demo && <p className="lk-demo-note">Face recognition is off on this Mac, so its beats are marked off. Everything else runs on the real sensors.</p>}
    <div className="lk-demo-body">
      <div className="lk-demo-cam">
        {demo?.camera_running && !camBroken
          ? <img src={`/api/security/demo/stream.mjpg?t=${demo.started_at ?? 0}`} alt="This Mac's camera with LOCKI's detections drawn on it" onError={() => setCamBroken(true)} />
          : <div className="lk-demo-cam-off"><CameraOff size={22} /><strong>Mac camera off</strong><small>{!active ? "It starts with the demo." : face ? "The demo couldn't open the camera." : "Face ID is off, so the demo doesn't use the Mac camera."}</small></div>}
        {active && <IdentityChip demo={demo} />}
      </div>
      <ol className="lk-demo-beats">{beats.map(b => <BeatRow key={b.id} b={b} />)}</ol>
    </div>
  </section>;
}

function IdentityChip({ demo }: { demo: DemoSnap | null }) {
  const s = demo?.visual_state?.face_identity_state;
  if (!s) return null;
  const conf = demo?.visual_state?.identity_confidence;
  const text = s === "owner" ? "Owner" : s === "unrecognized" ? "Not the owner" : "Checking face…";
  return <span className={`lk-demo-id is-${s}`}>{text}{conf != null && s !== "uncertain" ? <em>{conf.toFixed(2)}</em> : null}</span>;
}

function BeatRow({ b }: { b: Beat }) {
  const mark = b.state === "done" ? <Check size={14} /> : b.state === "failed" ? <X size={14} /> : b.state === "now" ? <i className="lk-beat-pulse" /> : <CircleDashed size={14} />;
  return <li className={`lk-beat is-${b.state} lk-beat-${b.tone}`}>
    <span className="lk-beat-mark" aria-hidden="true">{mark}</span>
    <div className="lk-beat-text">
      <span className="lk-beat-feature">{b.feature}</span>
      <strong>{b.title}</strong>
      {b.state === "now" && <span className="lk-beat-cue">{b.cue}</span>}
      <small>{b.detail}</small>
    </div>
  </li>;
}
