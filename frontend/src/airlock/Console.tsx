import { useEffect, useRef, useState, type ReactNode } from "react";
import { Brain, Cable, CameraOff, Check, ChevronDown, CircleAlert, Database, History, KeyRound, LayoutDashboard, LoaderCircle, LockKeyhole, PanelRight, Radar, RefreshCw, ScanSearch, Server, Settings2, ShieldAlert, ShieldCheck, ShieldOff, Square, TriangleAlert, Usb, UserRound, Volume2, X } from "lucide-react";
import "@fontsource-variable/archivo/wdth.css";
import { api } from "../services/api";
import type { CommandResult } from "../types/api";
import { type Row, ago, clock, milliseconds, words } from "./format";
import { maySubmitLiveArm } from "./armConsent";
import { type MacLink, type Stage, STAGES, remoteFreshness, reportedStage } from "./macSecurity";
import { type ProtectionState, EXPLAIN_QUESTION, INVESTIGATOR_IDENTITY, countdown, describeEvent, playbackLabel, startReview } from "./protection";
import "./console.css";

export type ConsoleView = "protection" | "investigation" | "history";
export type ExplainPhase = "idle" | "consent" | "granting" | "waiting_grant" | "investigating" | "done" | "failed";
export interface ExplainRun { phase: ExplainPhase; incidentId?: string; startedAt?: number; finishedAt?: number; result?: CommandResult; error?: string }
export interface StopState { phase: "idle" | "confirm" | "working" | "done"; grants: number; earlier?: number; disarmed?: boolean; revoked?: number; failed?: number; message?: string }

export interface ConsoleModel {
  now: number;
  mac: MacLink;
  db: { connected: boolean; status: string; error?: string; database: string };
  owner: boolean; role: string; profile: string; identity: string;
  station?: Row; stationLabel: string;
  live?: Row; lastSession?: Row;
  state: ProtectionState;
  incident?: Row; incidents: Row[]; events: Row[];
  scope: { eventIds: string[]; events: Row[]; baseline?: Row } | null;
  grant: "granted" | "revoked" | "none"; grantAt: number | null;
  explainBlocked: string | null;
  run: ExplainRun;
  jetsonHealth?: Row;
  voiceOutput: string | null;
  voiceWarning: string | null;
  history: { sessions: number; open: number };
  openGrants: number; revoking: boolean;
  pending: boolean;
}

export interface ConsoleActions {
  setView: (view: ConsoleView) => void;
  start: () => void; stop: () => void; explain: () => void; approve: () => void; cancelConsent: () => void; retry: () => void;
  stopSpeaking: () => void; revokeOpenGrants: () => void; openAdvanced: () => void; openRecover: () => void; openConnection: () => void; copyIdentity: () => void; reconnect: () => void;
}

const STAGE_LABEL: Record<Stage, string> = { NORMAL: "Normal", WATCHFUL: "Watchful", DEFENSIVE: "Defensive", CRITICAL: "Critical", OWNER_RETURNED: "Owner returned" };
const STAGE_NOTE: Record<Stage, string> = {
  NORMAL: "Desk clear", WATCHFUL: "Someone is near", DEFENSIVE: "Equipment touched", CRITICAL: "Unknown USB or incident", OWNER_RETURNED: "Owner's face matched",
};
const STATE_COPY: Record<ProtectionState, { word: string; tone: string }> = {
  UNVERIFIED: { word: "Not verified", tone: "muted" }, OFF: { word: "Protection off", tone: "muted" }, PROTECTED: { word: "Protected", tone: "ok" },
  INVESTIGATING: { word: "Investigating", tone: "ok" }, ATTENTION: { word: "Attention required", tone: "alert" },
};
const VIEW_LABEL: Record<ConsoleView, string> = { protection: "Protection", investigation: "Investigation", history: "History" };
const WARNING_TEXT: Record<string, string> = { person_at_desk: "Person at the desk", unknown_usb: "Unknown USB device", correlated: "Intrusion: person and unknown USB", watchlist_app: "Watched app opened", camera_covered: "Camera covered", owner_panic: "Owner security alert" };
const warningText = (w: any) => WARNING_TEXT[String(w?.reason)] ?? words(w?.reason);
const sentence = (s: string) => s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
const sessionShort = (id: unknown) => String(id ?? "").replace(/^session-/, "").slice(0, 8);
const sevTone = (s: unknown) => s === "critical" || s === "high" ? "alert" : s === "medium" ? "warn" : "muted";
const secondsToMs = (v: unknown) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? (n < 1e11 ? n * 1000 : milliseconds(n)) : 0; };
const fmtDuration = (ms: unknown) => { const v = Number(ms); return Number.isFinite(v) && v > 0 ? v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms` : "—"; };
const fmtLength = (ms: number) => { const min = Math.round(ms / 60000); return min >= 60 && min % 60 === 0 ? `${min / 60} h` : `${min} min`; };

function Mark({ size = 22 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 36 36" fill="none" aria-hidden="true"><path d="M7 7h22v22H7V7Z" stroke="currentColor" strokeWidth="2.4" /><path d="M13 2v32M23 2v32" stroke="currentColor" strokeWidth="2.4" /><path d="m17 14 4 4-4 4" stroke="currentColor" strokeWidth="2.4" /></svg>;
}

function Dot({ tone }: { tone: "ok" | "warn" | "alert" | "muted" }) { return <i className={`lk-dot lk-dot-${tone}`} aria-hidden="true" />; }

/** Jetson health: the device's own heartbeat in SpacetimeDB, and whether its presence receipts are reaching this Mac. */
function jetsonStatus(m: ConsoleModel): { tone: "ok" | "warn" | "alert" | "muted"; label: string; detail: string } {
  const h = m.jetsonHealth;
  const seen = h ? m.now - milliseconds(h.lastSeenMs) : Infinity;
  if (!m.db.connected) return { tone: "muted", label: "Jetson", detail: "Unverified: console not subscribed" };
  if (!h) return { tone: "alert", label: "Jetson", detail: "No heartbeat from jetson-01" };
  if (h.status !== "online" || seen > 30_000) return { tone: "warn", label: "Jetson", detail: `Heartbeat ${ago(h.lastSeenMs, m.now)} · ${words(h.status)}` };
  return { tone: "ok", label: "Jetson", detail: `jetson-01 online · heartbeat ${ago(h.lastSeenMs, m.now)}` };
}

/** Sidebar: brand, the three views, and the three links this console depends on. */
function Sidebar({ m, a, view }: { m: ConsoleModel; a: ConsoleActions; view: ConsoleView }) {
  const macTone = m.mac.status === "live" ? "ok" : m.mac.status === "offline" ? "alert" : "muted";
  const st = m.mac.data?.spacetimedb;
  const dbTone = m.db.connected && st?.connected && st?.authorized ? "ok" : m.db.connected || st?.connected ? "warn" : "alert";
  const jet = jetsonStatus(m);
  const tabs: { id: ConsoleView; icon: ReactNode; count?: string; countTone?: string }[] = [
    { id: "protection", icon: <ShieldCheck size={17} /> },
    { id: "investigation", icon: <ScanSearch size={17} />, count: m.incident ? "1" : undefined, countTone: "alert" },
    { id: "history", icon: <History size={17} />, count: m.history.open ? String(m.history.open) : undefined, countTone: "muted" },
  ];
  return <aside className="lk-side">
    <div className="lk-brand"><span className="lk-mark"><Mark /></span><span className="lk-wordmark">LOCKI <b>AIRLOCK</b></span></div>
    <nav className="lk-nav" aria-label="Views">
      <span className="lk-nav-label">Views</span>
      <div className="lk-tabs" role="tablist" aria-label="Views" aria-orientation="vertical">{tabs.map(t => <button key={t.id} role="tab" aria-selected={view === t.id} className={view === t.id ? "is-active" : ""} onClick={() => a.setView(t.id)}>{t.icon}<span>{VIEW_LABEL[t.id]}</span>{t.count && <span className={`lk-count lk-count-${t.countTone}`}>{t.count}</span>}</button>)}</div>
    </nav>
    <div className="lk-nav lk-nav-links">
      <span className="lk-nav-label">Connections</span>
      <ul className="lk-links" aria-label="Sensor connections">
        <li title={m.mac.status === "live" ? "This Mac's security backend is responding" : m.mac.error || "Waiting for this Mac's security backend"}><Server size={16} /><span>Mac</span><Dot tone={macTone} /></li>
        <li title={jet.detail}><Radar size={16} /><span>{jet.label}</span><Dot tone={jet.tone} /></li>
        <li title={`Owner console ${m.db.connected ? "subscribed" : m.db.status} · Mac publisher ${st?.connected ? st?.authorized ? "authorized" : "not authorized" : "offline"} · ${m.db.database}`}><Database size={16} /><span>SpacetimeDB</span><Dot tone={dbTone} /></li>
      </ul>
    </div>
    <div className="lk-side-card">
      <span className="lk-side-card-icon"><Settings2 size={16} /></span>
      <strong>Advanced controls</strong>
      <span>Arming options, enrollment, response policy, sensor health and rehearsal fixtures.</span>
      <button className="lk-btn lk-btn-block" onClick={a.openAdvanced}><PanelRight size={14} />Open controls</button>
    </div>
  </aside>;
}

function TopBar({ m, a, view }: { m: ConsoleModel; a: ConsoleActions; view: ConsoleView }) {
  const [menu, setMenu] = useState(false);
  return <header className="lk-top">
    <div className="lk-title"><span className="lk-crumb">{m.stationLabel} <i>/</i> {VIEW_LABEL[view]}</span><h1>{VIEW_LABEL[view]}</h1></div>
    <div className="lk-top-right">
      <div className="lk-owner">
        <button className="lk-owner-button" aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu(!menu)}><span className="lk-avatar">{(m.owner ? "O" : m.profile.slice(0, 1) || "?").toUpperCase()}</span><span className="lk-owner-text"><strong>{m.owner ? "Owner" : words(m.role)}</strong><small>{m.stationLabel}</small></span><ChevronDown size={14} /></button>
        {menu && <div className="lk-menu" role="menu" onMouseLeave={() => setMenu(false)}>
          <div className="lk-menu-id"><span>Signed-in identity</span><code title={m.identity}>{m.identity ? `${m.identity.slice(0, 8)}…${m.identity.slice(-6)}` : "Not connected"}</code></div>
          <button role="menuitem" onClick={() => { setMenu(false); a.copyIdentity(); }}>Copy public identity</button>
          <button role="menuitem" onClick={() => { setMenu(false); a.openConnection(); }}>Connection settings</button>
          {!m.owner && <button role="menuitem" onClick={() => { setMenu(false); a.openRecover(); }}>Recover owner access</button>}
          <button role="menuitem" onClick={() => { setMenu(false); a.openAdvanced(); }}>Advanced controls</button>
        </div>}
      </div>
      <button className="lk-icon-button" aria-label="Advanced controls" title="Advanced controls" onClick={a.openAdvanced}><PanelRight size={16} /></button>
    </div>
  </header>;
}

/** The four escalation stages drawn as a segmented arc, filled up to the stage the Mac reports. Owner returned and an
 *  unreadable stage fill nothing: the arc never shows a level the backend did not report. */
const ARC_STAGES: Stage[] = ["NORMAL", "WATCHFUL", "DEFENSIVE", "CRITICAL"];
const STAGE_COLOR: Record<Stage, string> = { NORMAL: "var(--green)", WATCHFUL: "var(--amber)", DEFENSIVE: "var(--orange)", CRITICAL: "var(--red)", OWNER_RETURNED: "var(--green)" };
function StageArc({ stage, fresh }: { stage: Stage | null; fresh: boolean }) {
  const per = 7, total = ARC_STAGES.length * per;
  const level = stage ? ARC_STAGES.indexOf(stage) : -1;
  const filled = level >= 0 ? (level + 1) * per : 0;
  const cx = 130, cy = 120, r1 = 80, r2 = 116, from = (196 * Math.PI) / 180, span = (212 * Math.PI) / 180;
  return <svg className={`lk-arc ${fresh ? "is-fresh" : ""}`} viewBox="0 0 260 158" aria-hidden="true">
    {Array.from({ length: total }, (_, i) => {
      const t = from - (i * span) / (total - 1), cos = Math.cos(t), sin = Math.sin(t);
      const on = i < filled;
      return <line key={i} x1={(cx + r1 * cos).toFixed(2)} y1={(cy - r1 * sin).toFixed(2)} x2={(cx + r2 * cos).toFixed(2)} y2={(cy - r2 * sin).toFixed(2)} className={on ? "is-on" : ""} style={{ stroke: on && stage ? STAGE_COLOR[stage] : undefined, ["--i" as any]: i }} />;
    })}
  </svg>;
}

function StageTrack({ m }: { m: ConsoleModel }) {
  const stage = reportedStage(m.mac);
  const vs = m.mac.data?.visual_state;
  const since = secondsToMs(vs?.mode_since);
  const fresh = Boolean(stage && since && m.now - since < 6000);
  return <section className="lk-card lk-stage" aria-live="polite">
    <header className="lk-card-head"><h2>Local security mode · M4</h2></header>
    <div className={`lk-gauge ${stage ? `lk-gauge-${stage.toLowerCase()}` : "is-unknown"}`}>
      <StageArc stage={stage} fresh={fresh} />
      <div className="lk-gauge-center"><span>Mode</span><strong>{stage ? STAGE_LABEL[stage] : "Unavailable"}</strong><small>{stage ? STAGE_NOTE[stage] : m.mac.status === "offline" ? "Mac backend offline" : "Waiting for this Mac"}</small></div>
      <span className="lk-gauge-end lk-gauge-min">Normal</span><span className="lk-gauge-end lk-gauge-max">Critical</span>
    </div>
    <p className="lk-stage-reason">{stage ? <>{words(vs?.mode_reason) !== "Unknown" ? words(vs?.mode_reason) : "Reported by this Mac"}{since ? ` · since ${clock(since)}` : ""}</> : m.mac.status === "offline" ? "Unavailable: this Mac's backend is not responding" : "Waiting for this Mac"}</p>
    <ol className="lk-stage-track">{STAGES.map((s, i) => {
      const active = stage === s;
      return <li key={s} className={`lk-step lk-step-${s.toLowerCase()} ${active ? "is-active" : ""} ${active && fresh ? "is-fresh" : ""}`} aria-current={active ? "step" : undefined}>
        <span className="lk-step-bar" /><span className="lk-step-label"><b>{String(i + 1).padStart(2, "0")}</b>{STAGE_LABEL[s]}</span><small>{STAGE_NOTE[s]}</small>
      </li>;
    })}</ol>
  </section>;
}

/** Time left in the armed session as a ring: the real expiry against the real armed time. */
function SessionRing({ left, total }: { left: number; total: number }) {
  const r = 50, c = 2 * Math.PI * r, v = total > 0 ? Math.max(0, Math.min(1, left / total)) : 0;
  return <div className="lk-ring" role="img" aria-label={`${countdown(left)} remaining of a ${fmtLength(total)} session`}>
    <svg viewBox="0 0 124 124" aria-hidden="true"><circle cx="62" cy="62" r={r + 9} className="lk-ring-guide" /><circle cx="62" cy="62" r={r} className="lk-ring-track" /><circle cx="62" cy="62" r={r} className="lk-ring-value" strokeDasharray={`${(c * v).toFixed(1)} ${c.toFixed(1)}`} transform="rotate(-90 62 62)" /></svg>
    <div className="lk-ring-label"><strong className="lk-mono">{countdown(left)}</strong><small>left of {fmtLength(total)}</small></div>
  </div>;
}

function Hero({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  const copy = STATE_COPY[m.state];
  const left = m.live ? milliseconds(m.live.expiresAtMs) - m.now : 0;
  const total = m.live ? milliseconds(m.live.expiresAtMs) - milliseconds(m.live.armedAtMs) : 0;
  const armed = Boolean(m.live);
  const investigating = ["granting", "waiting_grant", "investigating"].includes(m.run.phase);
  const meta = !m.db.connected ? (m.db.error || "Connecting to SpacetimeDB…")
    : !m.owner ? "This browser is not signed in as the workstation owner."
    : armed ? <>Live session · <span className="lk-mono">{countdown(left)}</span> remaining · auto-lock {m.live?.autoLockEnabled ? "on" : "off"}</>
    : m.lastSession ? `Last session ${m.lastSession.state === "armed" ? `expired ${clock(m.lastSession.expiresAtMs)}` : `ended ${clock(m.lastSession.disarmedAtMs || m.lastSession.expiresAtMs)}`}`
    : "No protected session yet.";
  return <section className={`lk-card lk-hero lk-tone-${copy.tone}`}>
    <div className="lk-hero-main">
      <h2 className="lk-state" aria-live="polite"><span className={`lk-state-dot lk-dot-${copy.tone}`} />{copy.word}</h2>
      <p className="lk-hero-meta">{meta}</p>
      <div className="lk-hero-actions">
        {!m.db.connected ? <button className="lk-btn" onClick={a.reconnect}><RefreshCw size={16} />Retry connection</button>
          : !m.owner ? <button className="lk-btn lk-btn-primary" onClick={a.openRecover}><KeyRound size={16} />Recover owner access</button>
          : !armed ? <button className="lk-btn lk-btn-primary lk-btn-xl" disabled={m.pending || !m.station} onClick={a.start}><ShieldCheck size={18} />Start protection</button>
          : <>{m.incident && <button className="lk-btn lk-btn-primary lk-btn-xl" disabled={investigating || m.pending} onClick={a.explain}>{investigating ? <LoaderCircle size={18} className="lk-spin" /> : <Brain size={18} />}{investigating ? "Investigating…" : "Explain incident"}</button>}
            <button className={`lk-btn ${m.incident ? "" : "lk-btn-xl"}`} disabled={m.pending} onClick={a.stop}><ShieldOff size={17} />Stop protection</button></>}
      </div>
      {!armed && m.owner && m.openGrants > 0 && <GrantsOpen m={m} a={a} />}
    </div>
    {(armed && total > 0) || m.incident ? <div className="lk-hero-side">
      {armed && total > 0 && <SessionRing left={left} total={total} />}
      {m.incident && <div className={`lk-severity lk-sev-${sevTone(m.incident.severity)}`}><span className="lk-eyebrow">Incident severity · SpacetimeDB</span><strong>{words(m.incident.severity)}</strong><span>{words(m.incident.state)} · {clock(m.incident.createdAtMs)}</span></div>}
    </div> : null}
  </section>;
}

/** Observations of one kind in this session, counted into equal time slices from arming until now. */
function sessionBuckets(m: ConsoleModel, tone: string, n = 18): { counts: number[]; total: number; from: number } | null {
  if (!m.live) return null;
  const from = milliseconds(m.live.armedAtMs), to = Math.max(from + 60_000, m.now);
  const width = (to - from) / n;
  const counts = new Array<number>(n).fill(0);
  let total = 0;
  for (const e of m.events) {
    if (describeEvent(e).tone !== tone) continue;
    const t = milliseconds(e.observedAtMs);
    if (t < from || t > to) continue;
    counts[Math.min(n - 1, Math.floor((t - from) / width))]++; total++;
  }
  return { counts, total, from };
}

function MiniBars({ data, noun }: { data: { counts: number[]; total: number; from: number } | null; noun: string }) {
  if (!data) return null;
  const max = Math.max(1, ...data.counts);
  const last = data.counts.reduce((acc, c, i) => (c > 0 ? i : acc), -1);
  return <div className="lk-bars-wrap">
    <div className="lk-bars" role="img" aria-label={`${data.total} ${noun}${data.total === 1 ? "" : "s"} since arming at ${clock(data.from)}`}>
      {data.counts.map((c, i) => <i key={i} className={i === last ? "is-last" : c ? "is-on" : ""} style={{ height: c ? `${Math.max(18, (c / max) * 100)}%` : undefined }} />)}
    </div>
    <span className="lk-bars-axis" aria-hidden="true"><span>Armed {clock(data.from)}</span><span>Now</span></span>
  </div>;
}

function EventRow({ e, now }: { e: Row; now: number }) {
  const d = describeEvent(e);
  const icon = d.tone === "person" ? <UserRound size={15} /> : d.tone === "usb" ? <Usb size={15} /> : d.tone === "camera" ? <CameraOff size={15} /> : <Cable size={15} />;
  return <li className={`lk-event lk-event-${d.tone}`}><span className="lk-event-icon">{icon}</span><span className="lk-event-text"><strong>{d.title}</strong><small>{d.source}{d.detail ? ` · ${d.detail}` : ""}</small></span><time title={clock(e.observedAtMs)}>{clock(e.observedAtMs)}<small>{ago(e.observedAtMs, now)}</small></time></li>;
}

function LiveActivity({ m }: { m: ConsoleModel }) {
  const feed = [...m.events].sort((x, y) => milliseconds(y.observedAtMs) - milliseconds(x.observedAtMs)).slice(0, 7);
  const linked = m.scope?.events ?? [];
  return <section className="lk-card lk-activity">
    <header className="lk-card-head"><h2>Live activity</h2><span className="lk-card-note">{m.live ? `This session · since ${clock(m.live.armedAtMs)}` : "No protected session"}</span></header>
    {m.incident ? <div className="lk-incident" role="alert">
      <div className="lk-incident-top"><span className="lk-incident-icon"><ShieldAlert size={18} /></span><div><span className="lk-eyebrow lk-text-alert">Incident · {words(m.incident.severity)} severity</span><h3>{m.incident.summary || words(m.incident.ruleId)}</h3></div><time>{clock(m.incident.createdAtMs)}</time></div>
      {linked.length > 0 && <ul className="lk-events lk-events-tight">{linked.map(e => <EventRow key={String(e.id)} e={e} now={m.now} />)}</ul>}
      {m.explainBlocked && <p className="lk-hint">{m.explainBlocked}</p>}
    </div>
      : m.live ? <div className="lk-calm"><span className="lk-calm-icon"><ShieldCheck size={22} strokeWidth={1.8} /></span><div><strong>Your workstation is protected.</strong><span>No incidents in this session.</span></div></div>
      : <div className="lk-calm lk-calm-off"><span className="lk-calm-icon"><ShieldOff size={22} strokeWidth={1.8} /></span><div><strong>{m.owner ? "Protection is off." : "Owner sign-in required."}</strong><span>{m.owner ? "Start protection to watch this desk with the Jetson camera and this Mac's USB and app sensors." : "Recover owner access to see this workstation's live activity."}</span></div></div>}
    {m.live && <><h4 className="lk-sub">Observations this session</h4>{feed.length ? <ul className="lk-events lk-timeline">{feed.map(e => <EventRow key={String(e.id)} e={e} now={m.now} />)}</ul> : <p className="lk-empty">No sensor observations yet in this session.</p>}</>}
  </section>;
}

function GrantsOpen({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  return <div className="lk-grants-open" role="status"><LockKeyhole size={16} /><span>The investigator still has read access to <strong>{m.openGrants} incident{m.openGrants === 1 ? "" : "s"}</strong> from earlier sessions.</span><button className="lk-btn lk-btn-ghost" disabled={m.revoking || m.pending} onClick={a.revokeOpenGrants}>{m.revoking ? <LoaderCircle size={14} className="lk-spin" /> : <ShieldOff size={14} />}Revoke access</button></div>;
}

function ResponseCard({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  const live = m.mac.status === "live";
  const armedAt = Number(m.mac.data?.session?.armed_at_ms) || (m.live ? milliseconds(m.live.armedAtMs) : 0);
  const warnings = live && armedAt ? (m.mac.data?.warnings ?? []).filter((w: any) => Number(w.at) * 1000 >= armedAt).slice(-4).reverse() : [];
  const lockMode = live ? m.mac.data?.lock_mode : null;
  const macLocks = live && m.mac.data?.session ? (m.mac.data?.locks ?? []).filter((l: any) => l.session_id === m.mac.data?.session?.session_id).slice(-2).reverse() : [];
  return <section className="lk-card lk-response">
    <header className="lk-card-head"><h2>LOCKI response</h2><span className="lk-card-note">What this Mac actually did</span></header>
    <div className="lk-response-block"><h4 className="lk-sub"><Volume2 size={14} />Spoken warnings</h4>
      {warnings.length ? <ul className="lk-warnings">{warnings.map((w: any, i: number) => { const p = playbackLabel(w); return <li key={`${w.event_id}-${i}`}><span className={`lk-warning-mark ${p.ok ? "is-ok" : ""}`}>{p.ok ? <Check size={12} /> : <Volume2 size={12} />}</span><span className="lk-warning-text">{warningText(w)}<small className={p.ok ? "lk-text-ok" : "lk-text-muted"}>{p.text}</small></span><time>{clock(Number(w.at) * 1000)}</time></li>; })}</ul>
        : <p className="lk-empty">{!live ? "Unavailable while this Mac's backend is offline." : m.live ? "No warning needed so far." : "Warnings play while protection is on."}</p>}
      {m.voiceOutput && <p className="lk-output"><Volume2 size={13} />Speaking on <strong>{m.voiceOutput}</strong></p>}
      {m.voiceWarning && <p className="lk-blocker" role="alert"><TriangleAlert size={14} />Voice output {m.voiceWarning}</p>}
    </div>
    <div className="lk-response-block"><h4 className="lk-sub"><LockKeyhole size={14} />Screen lock</h4>
      <div className="lk-lockline"><span className={`lk-pill ${lockMode === "dry_run" ? "lk-pill-warn" : lockMode ? "lk-pill-alert" : ""}`}>{lockMode === "dry_run" ? "Dry run" : lockMode ? words(lockMode) : "Unverified"}</span><span>{lockMode === "dry_run" ? "A lock request would be recorded, never executed. Auto-lock is off." : lockMode ? "Real lock mode" : "Lock mode can't be read right now."}</span></div>
      {macLocks.map((l: any, i: number) => <p key={i} className="lk-lock-request">Lock request {words(l.status)}{l.dry_run === true ? " · dry run, screen not locked" : ""}</p>)}
    </div>
    {m.incident && <div className="lk-response-cta"><div><strong>Want to know what happened?</strong><span>Nemotron reviews only this incident's evidence, with your approval.</span></div><button className="lk-btn lk-btn-primary" disabled={Boolean(m.explainBlocked) || ["granting", "waiting_grant", "investigating"].includes(m.run.phase)} onClick={a.explain}><Brain size={16} />Explain incident</button></div>}
  </section>;
}

function SensorTiles({ m }: { m: ConsoleModel }) {
  const d = m.mac.data;
  const live = m.mac.status === "live";
  const rc = d?.remote_camera;
  const armedAt = Number(d?.session?.armed_at_ms) || 0;
  const inSession = Boolean(live && rc && armedAt && Number(rc.server_received_at_ms) >= armedAt);
  const fresh = inSession ? remoteFreshness(rc, m.now) : "none";
  const camEvent = inSession ? (d?.camera_events ?? []).find((e: any) => e.event_id === rc.event_id) : null;
  const jet = jetsonStatus(m);
  const usb = d?.monitor?.usb; const apps = d?.monitor?.apps;
  const camOn = live && d?.demo?.camera_running === true;
  const usbTone = !live ? "muted" : usb && !usb.error && d?.monitor?.running ? "ok" : "warn";
  const appTone = !live ? "muted" : apps && !apps.error && d?.monitor?.running ? "ok" : "warn";
  const dbTone = m.db.connected && d?.spacetimedb?.authorized ? "ok" : m.db.connected ? "warn" : "alert";
  return <section className="lk-tiles" aria-label="Sensors">
    <div className={`lk-tile lk-tile-wide lk-tile-${jet.tone} ${fresh === "fresh" ? "is-pulse" : ""}`}><div className="lk-tile-head"><span className="lk-tile-icon"><Radar size={16} /></span><span>Jetson presence</span><Dot tone={jet.tone} /></div>
      {inSession ? <><strong>{sentence(words(rc.kind))}{camEvent?.zone ? ` · ${words(camEvent.zone)} zone` : ""}</strong><small>{typeof camEvent?.confidence === "number" ? `${Math.round(camEvent.confidence * 100)}% confidence · ` : ""}{ago(rc.server_received_at_ms, m.now)}{rc.acted_on ? " · acted on" : ""}</small></>
        : <><strong>{jet.tone === "ok" ? "Watching the desk" : jet.tone === "muted" ? "Unverified" : "Not reporting"}</strong><small>{m.live ? "No presence observed this session" : jet.detail}</small></>}
      <MiniBars data={sessionBuckets(m, "person")} noun="presence observation" />
    </div>
    <div className={`lk-tile lk-tile-${camOn ? "ok" : "muted"} ${camOn ? "lk-tile-media" : ""}`}><div className="lk-tile-head"><span className="lk-tile-icon"><CameraOff size={16} /></span><span>Local camera</span><Dot tone={camOn ? "ok" : "muted"} /></div>{camOn ? <img className="lk-tile-cam" src={`/api/security/demo/stream.mjpg?t=${d?.demo?.started_at ?? 0}`} alt="Live M4 camera" /> : <><strong>Off</strong><small>Not used for protection</small></>}</div>
    <div className={`lk-tile lk-tile-${usbTone}`}><div className="lk-tile-head"><span className="lk-tile-icon"><Usb size={16} /></span><span>USB watcher</span><Dot tone={usbTone} /></div>{!live ? <strong>Unverified</strong> : usb?.error ? <strong>Error</strong> : <strong className="lk-tile-num"><span className="lk-num">{usb?.connected ?? 0}</span> devices</strong>}<small>{!live ? "Mac backend offline" : usb?.error || "Watching connections"}</small><MiniBars data={sessionBuckets(m, "usb")} noun="USB observation" /></div>
    <div className={`lk-tile lk-tile-${appTone}`}><div className="lk-tile-head"><span className="lk-tile-icon"><LayoutDashboard size={16} /></span><span>App watcher</span><Dot tone={appTone} /></div><strong>{!live ? "Unverified" : apps?.error ? "Error" : "Watching"}</strong><small>{!live ? "Mac backend offline" : apps?.error || "Visible apps only"}</small><MiniBars data={sessionBuckets(m, "app")} noun="app observation" /></div>
    <div className={`lk-tile lk-tile-${dbTone}`}><div className="lk-tile-head"><span className="lk-tile-icon"><Database size={16} /></span><span>SpacetimeDB</span><Dot tone={dbTone} /></div><strong>{m.db.connected ? "Synced" : "Offline"}</strong><small>{d?.spacetimedb?.authorized ? "Mac publisher authorized" : live ? "Mac publisher not authorized" : "Mac status unknown"}</small></div>
  </section>;
}

function useInvestigationRecord(id?: string) {
  const [rec, setRec] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!id) return;
    let stop = false; setRec(null); setErr(null);
    api.investigation(id).then(r => { if (stop) return; if (!r || r.investigation_id !== id) setErr("The returned record does not match this investigation."); else setRec(r); })
      .catch(e => { if (!stop) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { stop = true; };
  }, [id]);
  return { rec, err };
}

function NemotronPanel({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  const run = m.run.incidentId === m.incident?.id ? m.run : { phase: "idle" as ExplainPhase };
  const n = run.result?.data?.nemotron;
  const id = typeof n?.investigation_id === "string" && /^inv_[A-Za-z0-9_-]{1,80}$/.test(n.investigation_id) ? n.investigation_id : undefined;
  const { rec, err } = useInvestigationRecord(run.phase === "done" ? id : undefined);
  const live = n?.evidence_badge === "LIVE";
  const elapsed = run.startedAt ? (run.finishedAt ?? m.now) - run.startedAt : 0;
  const gaps = (rec?.tools ?? []).filter((t: any) => !["ok", "finished"].includes(t.status));
  return <section className="lk-card lk-nemotron">
    <header className="lk-nemo-head"><div className="lk-nemo-title"><span className="lk-nemo-icon"><Brain size={18} /></span><div><h2>Nemotron investigation</h2><span className="lk-powered">Powered by NVIDIA Nemotron · runs on the M5 investigator</span></div></div>
      {run.phase === "done" && <span className={`lk-pill ${live ? "lk-pill-ok" : "lk-pill-warn"}`}>{live ? "LIVE evidence" : words(n?.status)}</span>}</header>
    <div className="lk-access"><LockKeyhole size={14} />{m.grant === "granted" ? <>Read-only access approved {m.grantAt ? clock(m.grantAt) : ""} for this incident only</> : m.grant === "revoked" ? "Access was revoked. Explaining asks you again." : "No access yet. Explaining asks for your one-time approval."}</div>
    {run.phase === "idle" || run.phase === "consent" ? <div className="lk-nemo-idle"><p>Ask the investigator to review exactly this incident's evidence and explain it. LOCKI speaks the answer.</p><button className="lk-btn lk-btn-primary lk-btn-xl" disabled={Boolean(m.explainBlocked)} onClick={a.explain}><Brain size={18} />Explain incident</button>{m.explainBlocked && <p className="lk-hint">{m.explainBlocked}</p>}</div>
      : run.phase === "granting" || run.phase === "waiting_grant" ? <div className="lk-nemo-run"><LoaderCircle size={22} className="lk-spin" /><div><strong>Approving scoped access</strong><span>{run.phase === "granting" ? "Committing your consent to SpacetimeDB…" : "Waiting for SpacetimeDB to confirm the grant…"}</span></div></div>
      : run.phase === "investigating" ? <div className="lk-nemo-run is-running"><span className="lk-scan" aria-hidden="true" /><div><strong>Nemotron is reviewing the evidence</strong><span>Live run · <span className="lk-mono">{(elapsed / 1000).toFixed(0)} s</span> · typically 10–15 s</span></div></div>
      : run.phase === "failed" ? <div className="lk-nemo-fail"><TriangleAlert size={20} /><div><strong>{n?.status === "unavailable" ? "Nemotron couldn't review this incident" : "The investigation didn't complete"}</strong><span>{n?.error || run.error || run.result?.message || "No reason was given."}</span>{run.result?.reply && <blockquote>{run.result.reply}</blockquote>}<button className="lk-btn" onClick={a.retry}><RefreshCw size={15} />Retry</button></div></div>
      : <div className="lk-nemo-result">
        <div className="lk-nemo-facts">
          <div><span className="lk-eyebrow">Run</span><code>{id ?? "—"}</code></div>
          <div><span className="lk-eyebrow">Inference time</span><strong className="lk-mono">{fmtDuration(n?.total_ms ?? rec?.total_latency_ms)}</strong></div>
          <div><span className="lk-eyebrow">Official severity</span><strong>{words(rec?.official_severity ?? m.incident?.severity)}</strong></div>
          <div><span className="lk-eyebrow">Grounding</span><strong>{live ? "Live evidence" : "Not live"}</strong></div>
        </div>
        {run.result?.reply && <figure className="lk-answer"><figcaption><Volume2 size={14} />LOCKI said · {clock(run.finishedAt)}</figcaption><blockquote>{run.result.reply}</blockquote><button className="lk-btn lk-btn-ghost" onClick={a.stopSpeaking}><Square size={13} />Stop speaking</button></figure>}
        {err ? <p className="lk-hint lk-text-warn">Investigation record unavailable: {err}</p> : !rec ? <p className="lk-empty">Loading the investigation record…</p> : <>
          {rec.headline && <p className="lk-headline">{rec.headline}</p>}
          {(rec.needs_review || gaps.length > 0) && <ul className="lk-caveats">{rec.needs_review && <li><CircleAlert size={14} />Needs human review: the investigator flagged its own result.</li>}{gaps.map((t: any) => <li key={t.tool}><CircleAlert size={14} />Not available to the investigation: {words(t.tool).replace(/^get /, "")} ({words(t.status)})</li>)}</ul>}
          <h4 className="lk-sub">Grounded findings</h4>
          <ol className="lk-findings">{(rec.observed_facts ?? []).map((f: any, i: number) => <li key={i}><span>{f.fact}</span>{(f.evidence_ids ?? []).length > 0 && <details><summary>{f.evidence_ids.length} evidence id{f.evidence_ids.length === 1 ? "" : "s"}</summary>{f.evidence_ids.map((e: string) => <code key={e}>{e}</code>)}</details>}</li>)}</ol>
          <h4 className="lk-sub">Tool execution</h4>
          <ul className="lk-tools">{(rec.tools ?? []).map((t: any, i: number) => <li key={i} className={["ok", "finished"].includes(t.status) ? "is-ok" : "is-gap"}><span className="lk-tool-mark">{["ok", "finished"].includes(t.status) ? <Check size={13} /> : <X size={13} />}</span><code>{t.tool}</code><span>{words(t.status)}{t.live_data ? " · live data" : t.live_data === false ? " · not live" : ""}</span></li>)}</ul>
        </>}
      </div>}
  </section>;
}

function InvestigationView({ m, a }: { m: ConsoleModel; a: ConsoleActions }) {
  if (!m.incident) return <section className="lk-card lk-empty-view"><span className="lk-empty-icon"><ScanSearch size={26} strokeWidth={1.6} /></span><h2>No incident in this session</h2><p>{m.live ? "When the Jetson and this Mac's sensors correlate into an incident, it appears here with its evidence." : "Start protection to monitor this workstation. Past incidents are in History."}</p><button className="lk-btn" onClick={() => a.setView("history")}><History size={15} />Open history</button></section>;
  const ev = m.scope?.events ?? [];
  return <div className="lk-investigation">
    <section className="lk-card lk-case">
      <header className="lk-case-head"><span className="lk-eyebrow lk-text-alert">Current incident · {words(m.incident.state)}</span><h2>{m.incident.summary || words(m.incident.ruleId)}</h2>
        <dl className="lk-case-meta">
          <div><dt>Severity</dt><dd><span className={`lk-pill lk-pill-${sevTone(m.incident.severity)}`}>{words(m.incident.severity)} severity</span></dd></div>
          <div><dt>Raised</dt><dd>{clock(m.incident.createdAtMs)}</dd></div>
          <div><dt>Rule</dt><dd>{words(m.incident.ruleId)}</dd></div>
          <div><dt>Session</dt><dd title={String(m.incident.sessionId)} className="lk-mono">{sessionShort(m.incident.sessionId)}</dd></div>
        </dl></header>
      <h4 className="lk-sub">Evidence sequence</h4>
      {ev.length ? <ol className="lk-sequence">{ev.map(e => { const d = describeEvent(e); return <li key={String(e.id)} className={`lk-seq-${d.tone}`}><time>{clock(e.observedAtMs)}</time><div><strong>{d.title}</strong><small>{d.source}{d.detail ? ` · ${d.detail}` : ""}</small><details><summary>Event id</summary><code>{String(e.id)}</code></details></div></li>; })}
        <li className="lk-sequence-end"><time>{clock(m.incident.createdAtMs)}</time><div><strong>Correlated into an incident</strong><small>SpacetimeDB rule {words(m.incident.ruleId)} · {words(m.incident.severity)} severity</small></div></li></ol>
        : <p className="lk-empty">This incident's linked events aren't visible to the console.</p>}
      <h4 className="lk-sub">What the investigator can read</h4>
      <ul className="lk-scope"><li><Check size={13} />This incident and its {ev.length} linked event{ev.length === 1 ? "" : "s"}</li>{m.scope?.baseline && <li><Check size={13} />This session's frozen USB baseline</li>}<li><X size={13} />Nothing else: no other incidents, sessions, files or screen</li></ul>
    </section>
    <NemotronPanel m={m} a={a} />
  </div>;
}

function Modal({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const d = ref.current; if (d && !d.open) d.showModal(); }, []);
  return <dialog ref={ref} className={`lk-modal ${wide ? "lk-modal-wide" : ""}`} onCancel={e => { e.preventDefault(); onClose(); }}>
    <header><h2>{title}</h2><button className="lk-icon-button" aria-label="Close" onClick={onClose}><X size={16} /></button></header>{children}
  </dialog>;
}

export function ConsentDialog({ m, busy, onApprove, onCancel }: { m: ConsoleModel; busy: boolean; onApprove: () => void; onCancel: () => void }) {
  const ev = m.scope?.events ?? [];
  return <Modal title="Allow Nemotron to review this incident?" onClose={onCancel}>
    <div className="lk-modal-body">
      <p className="lk-lede">LOCKI's investigator gets <strong>read-only</strong> access to exactly this evidence, then explains it aloud.</p>
      <div className="lk-consent-incident"><ShieldAlert size={16} /><div><strong>{m.incident?.summary || words(m.incident?.ruleId)}</strong><span>{words(m.incident?.severity)} severity · {clock(m.incident?.createdAtMs)}</span></div></div>
      <ul className="lk-consent-list">{ev.map(e => { const d = describeEvent(e); return <li key={String(e.id)}><Check size={13} /><span>{d.title}<small>{d.source} · {clock(e.observedAtMs)}</small></span></li>; })}
        {m.scope?.baseline && <li><Check size={13} /><span>This session's frozen USB baseline<small>Used to tell known devices from new ones</small></span></li>}</ul>
      <p className="lk-fine">Investigator <code title={INVESTIGATOR_IDENTITY}>{INVESTIGATOR_IDENTITY.slice(0, 8)}…{INVESTIGATOR_IDENTITY.slice(-6)}</code> · no other incidents, sessions or devices · revoked when you stop protection.</p>
    </div>
    <footer className="lk-modal-actions"><button className="lk-btn" onClick={onCancel}>Cancel</button><button className="lk-btn lk-btn-primary" disabled={busy || !ev.length} onClick={onApprove}>{busy ? <LoaderCircle size={15} className="lk-spin" /> : <ShieldCheck size={15} />}Approve and explain</button></footer>
  </Modal>;
}

const BLOCKER_TEXT: Record<string, string> = {
  autolock: "Automatic screen lock is on in this workstation's response policy. Turn it off under Advanced controls → Response policy; Start Protection never changes policy for you.",
  no_capture: "Waiting for this Mac to report its USB baseline.",
  stale_capture: "This Mac's baseline is out of date. Capture a fresh one.",
  invalid_capture: "This Mac's baseline report couldn't be read.",
};

export function StartDialog({ station, snapshots, sessions, now, call, onDone, onClose }: { station: Row; snapshots: Row[]; sessions: Row[]; now: number; call: (reducer: string, args: Row) => Promise<void>; onDone: () => void; onClose: () => void }) {
  const [duration, setDuration] = useState("60");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const review = startReview(station, snapshots, sessions, duration, now);
  const shown = review.reviewed?.fingerprint ?? null;
  const blocked = review.blockers.length > 0 || !review.reviewed;
  const capture = async () => { setCapturing(true); setError(null); try { await api.securityMonitorStart(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setCapturing(false); } };
  const start = async () => {
    const fresh = startReview(station, snapshots, sessions, duration, Date.now());
    if (fresh.blockers.length || !fresh.reviewed || !maySubmitLiveArm(shown, fresh.reviewed, fresh.capture?.observedAtMs, Date.now())) { setError("The Mac baseline changed or expired while you were reviewing it. Check it again."); return; }
    setBusy(true); setError(null);
    try {
      await call("armSession", { workstationId: station.id, sessionId: `session-${crypto.randomUUID()}`, baselineJson: fresh.reviewed.baselineJson, approvedAppsJson: JSON.stringify(fresh.apps), durationMs: BigInt(Number(duration) * 60000), simulated: false });
      onDone();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  return <Modal title="Start protection" onClose={onClose} wide>
    <div className="lk-modal-body">
      <p className="lk-lede">LOCKI freezes this Mac's current USB devices as the baseline. Anything new plugged in while you're away is flagged.</p>
      <div className="lk-start-grid">
        <div className="lk-start-box"><span className="lk-eyebrow">Mac baseline</span>
          {review.capture ? <><strong>{review.keys.length} USB device{review.keys.length === 1 ? "" : "s"} connected now</strong><small>Captured by {review.capture.deviceId} · {Math.max(0, Math.round(review.ageMs / 1000))} s ago{review.fresh ? "" : " · out of date"}</small>
            <details className="lk-keys"><summary>Device fingerprints</summary>{review.keys.length ? review.keys.map(k => <code key={k}>{k.slice(0, 16)}…</code>) : <span>No USB devices</span>}</details></>
            : <><strong>Waiting for this Mac</strong><small>No baseline report yet</small></>}
          {(review.blockers.includes("no_capture") || review.blockers.includes("stale_capture")) && <button className="lk-btn lk-btn-ghost" disabled={capturing} onClick={capture}>{capturing ? <LoaderCircle size={14} className="lk-spin" /> : <RefreshCw size={14} />}Capture now</button>}
        </div>
        <div className="lk-start-box"><span className="lk-eyebrow">Session</span>
          <dl className="lk-start-facts"><div><dt>Mode</dt><dd>Live · real sensors</dd></div><div><dt>Workstation</dt><dd>{station.name || station.id}</dd></div>
            <div><dt>Duration</dt><dd><select aria-label="Session length" value={duration} onChange={e => setDuration(e.target.value)}><option value="15">15 minutes</option><option value="60">1 hour</option><option value="240">4 hours</option><option value="480">8 hours</option></select></dd></div>
            <div><dt>Auto-lock</dt><dd className={station.autoLockEnabled ? "lk-text-alert" : ""}>{station.autoLockEnabled ? "On (blocks start)" : "Off · screen lock stays dry run"}</dd></div>
            <div><dt>Approved apps</dt><dd>{review.apps.length ? review.apps.join(", ") : "None"}</dd></div></dl>
        </div>
      </div>
      {review.blockers.map(b => <p key={b} className="lk-blocker"><TriangleAlert size={14} />{BLOCKER_TEXT[b]}</p>)}
      {error && <p className="lk-blocker" role="alert"><TriangleAlert size={14} />{error}</p>}
    </div>
    <footer className="lk-modal-actions"><span className="lk-fine">Starting confirms you reviewed this baseline and policy for <strong>{station.name || station.id}</strong>.</span><button className="lk-btn" onClick={onClose}>Cancel</button><button className="lk-btn lk-btn-primary" disabled={blocked || busy} onClick={start}>{busy ? <LoaderCircle size={15} className="lk-spin" /> : <ShieldCheck size={15} />}Start protection</button></footer>
  </Modal>;
}

export function StopDialog({ state, onConfirm, onClose }: { state: StopState; onConfirm: () => void; onClose: () => void }) {
  return <Modal title={state.phase === "done" ? "Protection stopped" : "Stop protection?"} onClose={onClose}>
    <div className="lk-modal-body">
      {state.phase === "done" ? <ul className="lk-stop-result">
        <li className={state.disarmed ? "is-ok" : "is-fail"}>{state.disarmed ? <Check size={14} /> : <X size={14} />}{state.disarmed ? "Workstation disarmed" : "Disarm was not confirmed"}</li>
        {state.grants > 0 && <li className={state.failed ? "is-fail" : "is-ok"}>{state.failed ? <X size={14} /> : <Check size={14} />}{`Nemotron access revoked for ${state.revoked ?? 0} of ${state.grants} incident${state.grants === 1 ? "" : "s"}`}</li>}
        {state.message && <li className="is-fail"><X size={14} />{state.message}</li>}
        <li className="is-ok"><Check size={14} />Evidence and history stay in SpacetimeDB</li></ul>
        : <><p className="lk-lede">LOCKI will disarm this workstation{state.grants ? ` and revoke the investigator's access to ${state.grants} incident${state.grants === 1 ? "" : "s"}${state.earlier ? ` (${state.earlier} from earlier sessions)` : ""}` : ""}. Incidents and evidence stay in History.</p></>}
    </div>
    <footer className="lk-modal-actions">{state.phase === "done" ? <button className="lk-btn lk-btn-primary" onClick={onClose}>Done</button> : <><button className="lk-btn" disabled={state.phase === "working"} onClick={onClose}>Cancel</button><button className="lk-btn lk-btn-danger" disabled={state.phase === "working"} onClick={onConfirm}>{state.phase === "working" ? <LoaderCircle size={15} className="lk-spin" /> : <ShieldOff size={15} />}Stop protection</button></>}</footer>
  </Modal>;
}

export function Console({ m, a, view, history, drawer, drawerOpen, closeDrawer, notice, dialogs }: { m: ConsoleModel; a: ConsoleActions; view: ConsoleView; history: ReactNode; drawer: ReactNode; drawerOpen: boolean; closeDrawer: () => void; notice?: ReactNode; dialogs?: ReactNode }) {
  return <div className={`lk-app lk-state-${m.state.toLowerCase()}`}>
    <Sidebar m={m} a={a} view={view} />
    <div className="lk-body">
      <TopBar m={m} a={a} view={view} />
      <main className="lk-main">
        {view === "protection" && <div className="lk-overview"><Hero m={m} a={a} /><StageTrack m={m} /><SensorTiles m={m} /><LiveActivity m={m} /><ResponseCard m={m} a={a} /></div>}
        {view === "investigation" && <InvestigationView m={m} a={a} />}
        {view === "history" && <div className="lk-history airlock-app lk-legacy">{m.owner && m.openGrants > 0 && <GrantsOpen m={m} a={a} />}{history}</div>}
        <footer className="lk-foot"><span><Mark size={13} />LOCKI AIRLOCK</span><span>SpacetimeDB holds the security record · this Mac reports its sensors · screen lock {m.mac.data?.lock_mode === "dry_run" ? "dry run" : "per policy"}</span><span><Server size={12} />{m.mac.status === "live" ? "Mac backend live" : "Mac backend unreachable"}</span></footer>
      </main>
    </div>
    {drawerOpen && <div className="lk-drawer-scrim" onClick={closeDrawer} />}
    <aside className={`lk-drawer ${drawerOpen ? "is-open" : ""}`} aria-hidden={!drawerOpen} aria-label="Advanced controls">
      <header><div><Settings2 size={16} /><strong>Advanced controls</strong></div><button className="lk-icon-button" aria-label="Close advanced controls" onClick={closeDrawer}><X size={16} /></button></header>
      <div className="lk-drawer-body airlock-app lk-legacy">{drawerOpen && drawer}</div>
    </aside>
    {notice}{dialogs}
  </div>;
}

export { EXPLAIN_QUESTION };
