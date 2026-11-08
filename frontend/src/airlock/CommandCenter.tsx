import { useEffect, useState, type ReactNode } from "react";
import { Activity, AudioLines, BellRing, Brain, Cable, Camera, CameraOff, Cpu, Database, Eye, LockKeyhole, Mic, Radar, ScanFace, Server, Speaker, UserRound, Volume2 } from "lucide-react";
import { api } from "../services/api";
import { type Row, ago, clock, shortId, words } from "./format";
import { Badge, Panel } from "./ui";
import { InvestigationRecord } from "./RecordedVoicePanel";
import { type MacLink, type Stage, STAGES, latestInvestigationId, remoteFreshness, reportedStage } from "./macSecurity";

const STAGE_INFO: Record<Stage, { label: string; note: string }> = {
  NORMAL: { label: "Normal", note: "No one unmatched at the desk." },
  WATCHFUL: { label: "Watchful", note: "Someone is near the desk." },
  DEFENSIVE: { label: "Defensive", note: "Protected equipment was touched." },
  CRITICAL: { label: "Critical", note: "Unknown USB or a database incident." },
  OWNER_RETURNED: { label: "Owner returned", note: "The enrolled owner's face matched." },
};

const fmtMs = (v: unknown) => typeof v === "number" && Number.isFinite(v) ? v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms` : "—";

/** The five stages, with only the one the Mac backend actually reports lit. Offline means no stage is lit. */
export function StageTrack({ link }: { link: MacLink }) {
  const stage = reportedStage(link);
  const vs = link.data?.visual_state;
  return <div className="cc-stage" aria-label={stage ? `Security stage: ${STAGE_INFO[stage].label}` : "Security stage unavailable"}>
    <ol className="cc-stage-track">{STAGES.map((s, i) => <li key={s} className={`cc-stage-step cc-stage-${s.toLowerCase()} ${stage === s ? "is-current" : ""}`} aria-current={stage === s ? "step" : undefined}>
      <span className="cc-stage-index">{String(i + 1).padStart(2, "0")}</span><strong>{STAGE_INFO[s].label}</strong><small>{STAGE_INFO[s].note}</small>
    </li>)}</ol>
    <p className="cc-stage-reason">{stage ? <>Reported by the Mac backend · {words(vs?.mode_reason) || "no reason given"} · since {clock(vs?.mode_since)}</> : link.status === "offline" ? "Stage unavailable: the Mac security backend is not responding." : "Waiting for the Mac security backend…"}</p>
  </div>;
}

/** The real camera picture when the Mac's camera pipeline is running. Otherwise it says the camera is off; it never shows a placeholder frame. */
export function LiveView({ link, now }: { link: MacLink; now: number }) {
  const demo = link.data?.demo;
  const running = link.status === "live" && demo?.camera_running === true;
  const vs = link.data?.visual_state;
  const [broken, setBroken] = useState(false);
  useEffect(() => { if (running) setBroken(false); }, [running]);
  return <section className="cc-live">
    <header className="cc-live-head"><span><Camera size={15} />Live camera</span>{running && !broken ? <Badge tone="magenta" dot>LIVE · M4 camera</Badge> : <Badge tone="quiet">No video stream</Badge>}</header>
    <div className="cc-live-frame">
      {running && !broken ? <img src={`/api/security/demo/stream.mjpg?t=${demo?.started_at ?? 0}`} alt="Live M4 camera with the pipeline's own annotations" onError={() => setBroken(true)} />
        : <div className="cc-live-empty"><CameraOff size={34} strokeWidth={1.3} /><strong>{link.status !== "live" ? "Mac backend unreachable" : broken ? "The camera stream stopped" : "M4 camera is off"}</strong>
          <p>{link.status !== "live" ? "No camera state can be shown until the backend responds." : "No frames are displayed. Jetson presence below comes from its own detector; the Jetson does not stream video here."}</p></div>}
      {running && !broken && vs && <div className="cc-live-overlay"><span>{vs.person_present ? vs.at_desk ? "Person at desk" : "Person near desk" : "No person detected"}</span>{vs.face_identity_state && <span>Face: {words(vs.face_identity_state)}</span>}<span>Camera {words(vs.camera_health)}</span></div>}
    </div>
    <JetsonPresence remote={link.data?.remote_camera} events={link.data?.camera_events} now={now} live={link.status === "live"} />
  </section>;
}

function JetsonPresence({ remote, events, now, live }: { remote: any; events: any[] | undefined; now: number; live: boolean }) {
  const fresh = live ? remoteFreshness(remote, now) : "none";
  const remoteEvents = (events ?? []).filter(e => e?.device_id && e.device_id !== "local" && !String(e.device_id).startsWith("mac")).slice(-4).reverse();
  return <div className="cc-jetson">
    <div className="cc-jetson-head"><span><Radar size={14} />Jetson presence detector</span><Badge tone={fresh === "fresh" ? "cyan" : "amber"} dot>{!live ? "Unverified" : fresh === "fresh" ? "Fresh" : fresh === "stale" ? "Stale" : "No receipt"}</Badge></div>
    {remote ? <dl className="cc-jetson-facts">
      <div><dt>Last observation</dt><dd>{words(remote.kind)}</dd></div>
      <div><dt>Device</dt><dd><code>{remote.device_id}</code></dd></div>
      <div><dt>Server received</dt><dd>{ago(remote.server_received_at_ms, now)}</dd></div>
      <div><dt>Camera → server</dt><dd>{fmtMs(remote.camera_to_server_ms)}</dd></div>
      <div><dt>Acted on</dt><dd>{remote.acted_on ? "Yes" : `No${remote.why_not ? ` · ${words(remote.why_not)}` : ""}`}</dd></div>
    </dl> : <p className="cc-muted-line">No Jetson observation has reached this Mac yet.</p>}
    {remoteEvents.length > 0 && <ul className="cc-mini-list">{remoteEvents.map(e => <li key={e.event_id}><UserRound size={12} />{words(e.kind)}<time>{clock(e.observed_at)}</time></li>)}</ul>}
    <p className="cc-footnote">Presence only. The Jetson does not identify anyone.</p>
  </div>;
}

/** Owner-face recognition on the M4's own camera, only if the backend reports it enabled. Never arms, disarms or unlocks. */
export function FaceIdCard({ link }: { link: MacLink }) {
  const demo = link.data?.demo;
  const vs = link.data?.visual_state;
  const enabled = demo?.face_identification_enabled === true;
  const state = vs?.face_identity_state as string | null | undefined;
  return <Panel title="Owner face (M4, optional)" icon={<ScanFace size={16} />} aside={<Badge tone={!enabled ? "quiet" : state === "owner" ? "cyan" : state === "unrecognized" ? "magenta" : "amber"}>{link.status !== "live" ? "Unverified" : !enabled ? "Disabled" : state ? words(state) : demo?.camera_running ? "Looking" : "Camera off"}</Badge>}>
    <dl className="cc-facts">
      <div><dt>Feature</dt><dd>{link.status !== "live" ? "—" : enabled ? "Enabled for consented tests" : "Off"}</dd></div>
      <div><dt>Enrollment</dt><dd>{demo?.enrolling ? `Enrolling ${demo.enrollment?.accepted ?? 0}/${demo.enrollment?.needed ?? "?"}` : demo?.enrolled ? `Enrolled (${demo.enrolled_samples ?? "?"} samples)` : "Not enrolled"}</dd></div>
      <div><dt>Recognition</dt><dd>{state ? `${words(state)}${typeof vs?.identity_confidence === "number" ? ` · ${(vs.identity_confidence * 100).toFixed(0)}%` : ""}` : "No face checked"}</dd></div>
    </dl>
    <p className="cc-footnote">Face recognition changes what LOCKI says. It cannot arm, disarm or unlock. {enabled && <a href="#face-setup">Open face setup</a>}</p>
  </Panel>;
}

/** What the system actually did: spoken warnings, owner notifications and lock requests, with dry_run stated plainly. */
export function ResponsePanel({ link, dbLocks, sessionId, children }: { link: MacLink; dbLocks: Row[]; sessionId?: string; children?: ReactNode }) {
  const live = link.status === "live";
  const warnings = live ? [...(link.data?.warnings ?? [])].reverse().slice(0, 4) : [];
  const macLocks = live ? [...(link.data?.locks ?? [])].reverse().slice(0, 3) : [];
  const locks = [...dbLocks].filter(l => !sessionId || l.sessionId === sessionId).sort((a, b) => Number(b.issuedAtMs) - Number(a.issuedAtMs)).slice(0, 3);
  const lockMode = live ? link.data?.lock_mode : null;
  return <Panel title="Security response" icon={<BellRing size={16} />} aside={<Badge tone={lockMode === "dry_run" ? "amber" : lockMode ? "magenta" : "quiet"}>{lockMode ? lockMode === "dry_run" ? "Screen lock: dry run" : `Screen lock: ${words(lockMode)}` : "Lock mode unverified"}</Badge>}>
    <div className="cc-response">
      <h4><Volume2 size={13} />Audio warnings</h4>
      {warnings.length ? <ul className="cc-mini-list">{warnings.map((w: any, i: number) => <li key={`${w.event_id}-${i}`}><Speaker size={12} />{w.label || words(w.reason)}<span className={w.spoken ? "al-text-cyan" : "al-muted"}>{w.spoken ? "spoken" : w.skipped ? "not repeated" : "not spoken"}</span><time>{clock(Number(w.at) * 1000)}</time></li>)}</ul> : <p className="cc-muted-line">{live ? "No warnings spoken this session." : "Unavailable while the Mac backend is offline."}</p>}
      <h4><LockKeyhole size={13} />Lock requests</h4>
      {locks.length || macLocks.length ? <ul className="cc-mini-list">
        {locks.map(l => <li key={String(l.id)}><Database size={12} />{words(l.status)}{l.simulated && <Badge tone="amber">Synthetic</Badge>}<time>{clock(l.issuedAtMs)}</time></li>)}
        {macLocks.map((l: any, i: number) => <li key={`${l.action_id}-${i}`}><LockKeyhole size={12} />Mac: {words(l.status)}{l.dry_run === true && <Badge tone="amber">dry run · not locked</Badge>}{typeof l.confirm_ms === "number" && <span className="al-muted">{fmtMs(l.confirm_ms)}</span>}<time>{l.at ? clock(Number(l.at) * 1000) : ""}</time></li>)}
      </ul> : <p className="cc-muted-line">No lock requested.{lockMode === "dry_run" ? " Locks are dry run: a request would be recorded, never executed." : ""}</p>}
    </div>
    {children}
  </Panel>;
}

/** The latest Nemotron investigation LOCKI ran for a voice question, read from the Mac backend (the investigator key never reaches the browser). */
export function NemotronPanel({ now }: { now: number }) {
  const [turns, setTurns] = useState<any[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => { try { const s = await api.recordedStatus(); if (!stop) { setTurns(s?.turns ?? []); setError(null); } } catch (e) { if (!stop) setError(e instanceof Error ? e.message : String(e)); } };
    void load();
    const id = window.setInterval(load, 3000);
    return () => { stop = true; window.clearInterval(id); };
  }, []);
  const latest = latestInvestigationId(turns ?? undefined);
  const lastAsk = (turns ?? []).find(t => t?.nemotron);
  const n = lastAsk?.nemotron;
  const live = n?.evidence_badge === "LIVE";
  return <Panel title="Nemotron investigation" icon={<Brain size={16} />} aside={n ? <Badge tone={live ? "cyan" : "amber"} dot>{live ? "LIVE evidence" : words(n.status) || "Unavailable"}</Badge> : <Badge tone="quiet">No investigation yet</Badge>}>
    <div className="cc-nemotron">
      {error ? <p className="cc-muted-line">Unavailable: {error}</p>
        : !n ? <p className="cc-muted-line">Ask "LOCKI, what happened while I was gone?" about a live incident. The answer and its evidence appear here.</p>
        : <>
          <div className="cc-nemotron-meta"><span>{lastAsk.transcript ? `“${lastAsk.transcript}”` : lastAsk.file}</span><time>{ago(Number(lastAsk.at) * 1000, now)}</time></div>
          {n.status === "unavailable" ? <p className="al-text-amber al-small">Nemotron couldn't review this one: {n.error}. LOCKI answered from the local record only.</p>
            : <p className="al-small">Status {words(n.status)} · {fmtMs(n.total_ms)}{n.needs_review ? " · needs human review" : ""}</p>}
          {latest && latest.turn === lastAsk && <InvestigationRecord id={latest.id} />}
          {latest && <p className="cc-footnote">Investigation <code>{latest.id}</code></p>}
        </>}
    </div>
  </Panel>;
}

type Health = "ok" | "degraded" | "offline";
function HealthRow({ icon, name, state, detail }: { icon: ReactNode; name: string; state: Health; detail?: string }) {
  return <li className={`cc-health cc-health-${state}`}><span className="cc-health-icon">{icon}</span><span className="cc-health-name"><strong>{name}</strong>{detail && <small title={detail}>{detail}</small>}</span><Badge tone={state === "ok" ? "cyan" : state === "degraded" ? "amber" : "magenta"} dot>{state === "ok" ? "Connected" : state === "degraded" ? "Degraded" : "Offline"}</Badge></li>;
}

/** Every sensor and link, as the Mac backend reports it right now. Offline when the backend itself can't be reached. */
export function SensorHealth({ link, dbConnected, now, children }: { link: MacLink; dbConnected: boolean; now: number; children?: ReactNode }) {
  const live = link.status === "live";
  const r = link.data?.demo?.readiness ?? {};
  const mon = link.data?.monitor;
  const remote = link.data?.remote_camera;
  const fresh = remoteFreshness(remote, now);
  const st = link.data?.spacetimedb;
  const off = (d = "Mac backend not responding") => ({ state: "offline" as Health, detail: d });
  const ready = (key: string, offWhen = false) => !live ? off() : { state: (r[key]?.ok ? "ok" : offWhen ? "offline" : "degraded") as Health, detail: r[key]?.detail };
  const rows: { icon: ReactNode; name: string; v: { state: Health; detail?: string } }[] = [
    { icon: <Server size={15} />, name: "Mac security backend", v: live ? { state: "ok", detail: "responding" } : off(link.error || "connecting") },
    { icon: <Database size={15} />, name: "SpacetimeDB (Mac)", v: !live ? off() : { state: st?.connected && st?.authorized ? "ok" : st?.connected ? "degraded" : "offline", detail: st ? `${st.database ?? ""}${st.authorized ? " · authorized" : " · not authorized"}` : undefined } },
    { icon: <Database size={15} />, name: "SpacetimeDB (console)", v: dbConnected ? { state: "ok", detail: "subscribed" } : { state: "offline", detail: "not subscribed" } },
    { icon: <Cable size={15} />, name: "USB watcher", v: !live ? off() : { state: mon?.running && !mon?.usb?.error ? "ok" : "degraded", detail: mon?.usb ? `${mon.usb.connected ?? 0} devices · ${mon.usb.error || "running"}` : r.usb_monitor?.detail } },
    { icon: <Eye size={15} />, name: "App-launch watcher", v: !live ? off() : { state: mon?.running && !mon?.apps?.error ? "ok" : "degraded", detail: mon?.apps ? `visible apps only · ${mon.apps.launched ?? 0} launches seen` : undefined } },
    { icon: <Radar size={15} />, name: "Jetson presence", v: !live ? off() : { state: fresh === "fresh" ? "ok" : fresh === "stale" ? "degraded" : "offline", detail: remote ? `last receipt ${ago(remote.server_received_at_ms, now)}` : "no receipt" } },
    { icon: <Camera size={15} />, name: "M4 camera", v: ready("camera") },
    { icon: <Speaker size={15} />, name: "Speaker", v: ready("speaker") },
    { icon: <Mic size={15} />, name: "Microphone", v: !live ? off() : { state: r.microphone?.ok ? "ok" : "degraded", detail: r.microphone?.detail } },
    { icon: <Cpu size={15} />, name: "Investigator model", v: ready("investigator") },
    { icon: <LockKeyhole size={15} />, name: "Screen lock", v: ready("screen_lock") },
  ];
  return <Panel title="System health" icon={<Activity size={16} />} aside={<Badge tone={live ? "cyan" : "magenta"} dot>{live ? "Live" : "Mac offline"}</Badge>}>
    <ul className="cc-health-list">{rows.map(x => <HealthRow key={x.name} icon={x.icon} name={x.name} state={x.v.state} detail={x.v.detail} />)}</ul>
    {children}
  </Panel>;
}

/** A plain, newest-first feed of what the Mac received and did: camera and Jetson observations, stage changes and spoken warnings. */
export function MacActivity({ link }: { link: MacLink }) {
  if (link.status !== "live") return null;
  const d = link.data ?? {};
  const items: { at: number; icon: ReactNode; text: string; tag?: string }[] = [
    ...(d.camera_events ?? []).map((e: any) => ({ at: Number(e.observed_at), icon: <UserRound size={13} />, text: `${words(e.kind)}${e.zone ? ` · ${e.zone}` : ""}`, tag: e.device_id })),
    ...(d.mode_transitions ?? []).map((t: any) => ({ at: Number(t.at), icon: <Activity size={13} />, text: `Stage ${words(t.from)} → ${words(t.to)}${t.reason ? ` · ${words(t.reason)}` : ""}`, tag: "stage" })),
    ...(d.warnings ?? []).map((w: any) => ({ at: Number(w.at) * 1000, icon: <AudioLines size={13} />, text: `Warning: ${w.label || words(w.reason)}`, tag: w.spoken ? "spoken" : "silent" })),
  ].filter(x => Number.isFinite(x.at)).sort((a, b) => b.at - a.at).slice(0, 10);
  if (!items.length) return null;
  return <div className="cc-activity"><h4>Mac receipts</h4><ul className="cc-mini-list">{items.map((x, i) => <li key={i}>{x.icon}{x.text}{x.tag && <code>{shortId(x.tag)}</code>}<time>{clock(x.at)}</time></li>)}</ul></div>;
}
