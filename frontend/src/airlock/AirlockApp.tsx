import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, Cable, Camera, Check, ChevronDown, Copy, Fingerprint, FlaskConical, KeyRound, Laptop, LockKeyhole, Plus, Radio, RefreshCw, Server, ServerOff, Settings2, ShieldAlert, ShieldCheck, ShieldOff, SlidersHorizontal, Unplug, Users, Wifi, WifiOff, X } from "lucide-react";
import { ToastProvider } from "../components/Toasts";
import { type ConnectionConfig, readConfig, useAirlock } from "./client";
import { type Row, ago, clock, milliseconds, shortId, textValue, words } from "./format";
import { AccessForm, ArmForm, BaselineList, ConnectionForm, PolicyForm, RegisterForm, RequestActionForm, type Perform } from "./forms";
import { Investigation, ProtectedActions, ReviewPanel } from "./Investigation";
import { Timeline } from "./Timeline";
import { Badge, Button, Panel } from "./ui";
import { VoiceBridge } from "./VoiceBridge";
import { VoiceArmAuthorization } from "./VoiceArm";
import { OwnerRecovery } from "./OwnerRecovery";
import { workstationSelection } from "./ownerRecoveryPolicy";
import { InvestigatorAccess } from "./InvestigatorAccess";
import { DeskView } from "./DeskView";
import { IncidentAlerts } from "./IncidentAlerts";
import { DispatchPanel } from "./DispatchPanel";
import { RecordedVoicePanel } from "./RecordedVoicePanel";
import { detectionCoverage, healthStatus, latestHealth } from "./coverage";
import { FaceIdCard, LiveView, MacActivity, NemotronPanel, ResponsePanel, SensorHealth, StageTrack } from "./CommandCenter";
import { useMacSecurity } from "./macSecurity";
import { bySession, liveSession, macSession, pastSessions, pickIncident } from "./liveScope";
import "./airlock.css";
import "./commandCenter.css";

type Dialog = "connect" | "register" | "arm" | "access" | "policy" | "action" | "recover" | null;

function AirlockMark({ size = 30 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 36 36" fill="none" aria-hidden="true"><path d="M7 7h22v22H7V7Z" stroke="currentColor" strokeWidth="2" /><path d="M13 2v32M23 2v32" stroke="currentColor" strokeWidth="2" /><path d="m17 14 4 4-4 4" stroke="currentColor" strokeWidth="2" /><path d="M2 13h5m22 0h5M2 23h5m22 0h5" stroke="currentColor" strokeWidth="2" /></svg>;
}

function SessionCard({ title, text, tone = "quiet" }: { title: string; text: string; tone?: "quiet" | "cyan" | "amber" }) {
  return <section className={`cc-session-card cc-tone-${tone}`}><ShieldCheck size={22} /><div><strong>{title}</strong><p>{text}</p></div></section>;
}

function SensorRow({ icon, title, health, now, enrolled, connected }: { icon: React.ReactNode; title: string; health?: Row; now: number; enrolled: boolean; connected: boolean }) {
  const status = healthStatus(health, now, connected, enrolled);
  const online = status === "online";
  return <div className="al-sensor-row"><span className="al-sensor-icon">{icon}</span><div><strong>{title}</strong><small title={health?.detail}>{health ? `${ago(health.lastSeenMs, now)}${health.detail ? ` · ${health.detail}` : ""}` : enrolled ? "Waiting for publisher health" : "Enroll a trusted publisher"}</small></div><Badge tone={online ? "cyan" : "amber"} dot>{words(status)}</Badge></div>;
}

function FixturePanel({ station, session, devices, owner, role, connected, pending, perform }: { station?: Row; session?: Row; devices: Row[]; owner: boolean; role: string; connected: boolean; pending: boolean; perform: Perform }) {
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("correlated");
  const [deviceKey, setDeviceKey] = useState("synthetic-usb-demo");
  const localSeq = useRef<Record<string, bigint>>({});
  const publisher = owner ? devices.find(d => d.id === `${station?.id}:fixture`) : devices.find(d => d.role === role && d.enabled);
  const canPublish = owner || ["mac", "jetson"].includes(role);
  const enabled = connected && session?.state === "armed" && session.simulated && canPublish;
  const emit = async () => {
    if (!station || !session || !enabled) return;
    const deviceId = owner ? "fixture" : publisher?.id;
    if (!deviceId) return;
    const actualKind = kind === "correlated" && !owner ? role === "jetson" ? "person_in_zone" : "usb_added" : kind;
    const kinds = actualKind === "correlated" ? ["person_in_zone", "usb_added"] : [actualKind];
    let seq = BigInt(publisher?.lastSeq || 0);
    if ((localSeq.current[deviceId] || 0n) > seq) seq = localSeq.current[deviceId];
    for (const eventKind of kinds) {
      seq += 1n;
      const metadata = eventKind.startsWith("usb") ? { deviceKey, label: "Synthetic harmless USB fixture" } : eventKind === "app_launched" ? { appId: "synthetic.remote-admin", label: "Synthetic remote-admin app fixture" } : { detail: `Synthetic ${words(eventKind)} fixture; no camera inference` };
      const ok = await perform("ingestDeviceEvent", { workstationId: station.id, sessionId: session.id, deviceId, eventId: `fixture-${crypto.randomUUID()}`, kind: eventKind, observedAtMs: BigInt(Math.max(Date.now(), milliseconds(session.armedAtMs))), seq, confidence: 1, metadataJson: JSON.stringify(metadata), simulated: true, schemaVersion: 1 }, kinds.length > 1 && eventKind === kinds[0] ? "Synthetic physical observation committed." : "Synthetic observation committed to SpacetimeDB.");
      if (!ok) return;
      localSeq.current[deviceId] = seq;
    }
  };
  return <section className="al-fixtures"><button className="al-fixture-heading" aria-expanded={open} onClick={() => setOpen(!open)}><span><FlaskConical size={16} /><strong>Demo fixtures</strong><Badge tone="amber">Synthetic only</Badge></span><ChevronDown size={15} className={open ? "rotated" : ""} /></button>{open && <div className="al-fixture-body"><p>Send labeled fixture observations through real reducers. These do not represent a physical sensor, model inference or real device insertion.</p><div className="al-fixture-controls"><select aria-label="Fixture scenario" value={kind} onChange={e => setKind(e.target.value)}>{owner && <option value="correlated">Person + unknown USB · correlated</option>}{(owner || role === "mac") && <><option value="usb_added">Unknown USB connected</option><option value="app_launched">Remote-admin app launched</option></>}{(owner || role === "jetson") && <><option value="person_in_zone">Person in desk zone</option><option value="camera_offline">Camera unavailable</option></>}</select><Button kind="warning" disabled={!enabled || pending} onClick={emit}><FlaskConical size={14} />Send fixture</Button></div>{kind.includes("usb") || kind === "correlated" ? <label className="al-form-field"><span>Fixture device key</span><input value={deviceKey} onChange={e => setDeviceKey(e.target.value)} /></label> : null}{!enabled && <p className="al-small al-text-amber">{!connected ? "Connect to the database to use fixtures." : !session || session.state !== "armed" ? "Arm a synthetic session first." : !session.simulated ? "Fixtures are blocked in a live session." : "Only an owner or enrolled sensor publisher can send fixtures."}</p>}<div className="al-fixture-guarantee"><LockKeyhole size={12} />Synthetic sessions never request a real screen lock.</div></div>}</section>;
}

function ControlRoom() {
  const [config, setConfig] = useState<ConnectionConfig>(readConfig);
  const live = useAirlock(config);
  const mac = useMacSecurity();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [stationId, setStationId] = useState(new URLSearchParams(location.search).get("workstation") || "");
  const [incidentId, setIncidentId] = useState("");
  const [view, setView] = useState<"live" | "history">("live");
  const [pastId, setPastId] = useState("");
  const [now, setNow] = useState(Date.now());
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(id); }, []);
  useEffect(() => { if (!notice) return; const id = window.setTimeout(() => setNotice(null), notice.error ? 12000 : 5000); return () => window.clearTimeout(id); }, [notice]);
  const connected = live.status === "connected";
  const pending = Boolean(live.pending);
  const stations = live.data.workstations;
  const { station, targetUnavailable, mayCreate } = workstationSelection(stations, stationId);
  const scoped = useMemo(() => Object.fromEntries(Object.entries(live.data).map(([key, rows]) => [key, rows.filter(r => r.workstationId === station?.id)])) as typeof live.data, [live.data, station?.id]);
  const membership = scoped.memberships.find(m => textValue(m.identity) === live.identity && m.enabled);
  const role = membership?.role || "unenrolled";
  const owner = role === "owner";
  const reviewer = owner || role === "reviewer";
  const canApprove = owner || Boolean(membership?.canApprove);
  const sessions = [...scoped.sessions].sort((a, b) => Number(b.armedAtMs) - Number(a.armedAtMs));
  const session = sessions.find(s => s.id === station?.activeSessionId);
  const armed = session?.state === "armed" && milliseconds(session.expiresAtMs) > now;
  const incidents = [...scoped.incidents].sort((a, b) => Number(b.createdAtMs) - Number(a.createdAtMs));
  // Live session vs history: only rows carrying the SpacetimeDB-active, unexpired session's id are current. Everything else is history.
  const liveS = liveSession(sessions, station?.activeSessionId, now);
  const liveId = liveS?.id as string | undefined;
  const evSplit = bySession(scoped.events, liveId);
  const incSplit = bySession(incidents, liveId);
  const past = pastSessions(sessions, scoped.events, incidents, liveId);
  const historyEvents = pastId ? evSplit.history.filter(e => e.sessionId === pastId) : evSplit.history;
  const historyIncidents = pastId ? incSplit.history.filter(i => i.sessionId === pastId) : incSplit.history;
  const viewIncidents = view === "live" ? incSplit.current : historyIncidents;
  const selectedIncident = pickIncident(viewIncidents, incidentId);
  const unresolved = incSplit.current.filter(i => i.state !== "resolved");
  const historyOpen = incSplit.history.filter(i => i.state !== "resolved").length;
  const currentIncident = unresolved[0];
  const liveAudit = liveS ? scoped.audit.filter(a => milliseconds(a.createdAtMs) >= milliseconds(liveS.armedAtMs)) : [];
  const macS = macSession(mac.data);
  const openHistory = (id = "") => { setView("history"); setPastId(id); setIncidentId(""); };
  const ownerPanic = currentIncident?.ruleId === "owner_panic";
  const locks = [...scoped.locks].sort((a, b) => Number(b.issuedAtMs) - Number(a.issuedAtMs));
  const currentLock = locks.find(l => liveId !== undefined && l.sessionId === liveId && !["expired", "cancelled", "failed"].includes(l.status));
  const deviceFor = (r: string) => scoped.devices.some(d => d.role === r && d.enabled);
  const camera = latestHealth(scoped.health, scoped.devices, "camera");
  const usb = latestHealth(scoped.health, scoped.devices, "usb");
  const appHealth = latestHealth(scoped.health, scoped.devices, "apps");
  const model = latestHealth(scoped.health, scoped.devices, "model");
  const capacity = scoped.health.find(h => h.deviceId === "database" && h.component === "capacity" && h.status === "degraded");
  const coverage = detectionCoverage(scoped.health, scoped.devices, now, connected);
  const degraded = coverage !== "available";
  const visibleGate = ["owner", "reviewer", "service"].includes(role);
  const title = !connected ? "Console disconnected" : targetUnavailable ? "Requested workstation unavailable" : !station ? "Your desk. Your boundary." : currentLock?.status === "confirmed" ? currentLock.simulated ? "Synthetic lock confirmation" : "Screen lock confirmed" : currentLock ? currentLock.simulated ? "Synthetic lock request committed" : "Screen lock requested" : ownerPanic ? "Owner security alert" : currentIncident ? "Activity needs your review" : armed ? "Your workstation is armed" : "Your workstation is disarmed";
  const status = !connected ? "Disconnected" : targetUnavailable ? "Access unverified" : currentLock?.status === "confirmed" ? currentLock.simulated ? "Synthetic lock confirmation" : "Screen lock confirmed" : currentLock ? currentLock.simulated ? "Synthetic lock request committed" : "Screen lock requested" : ownerPanic ? "Panic requested" : currentIncident ? "Suspected intrusion" : armed ? "Armed" : "Disarmed";
  const perform: Perform = async (reducer, args, success) => {
    try { await live.call(reducer, args); if (success) setNotice({ text: success, error: false }); return true; }
    catch (e) { setNotice({ text: e instanceof Error ? e.message : String(e), error: true }); return false; }
  };
  const copyIdentity = async () => { if (!live.identity) return; try { await navigator.clipboard.writeText(live.identity); setCopied(true); window.setTimeout(() => setCopied(false), 2000); } catch { setDialog("connect"); } };
  const saveConnection = (next: ConnectionConfig) => {
    localStorage.setItem("airlock.connection.v1", JSON.stringify({ uri: next.uri, database: next.database }));
    const url = new URL(location.href); url.searchParams.set("client", next.profile); url.searchParams.set("airlockUri", next.uri); url.searchParams.set("airlockDatabase", next.database); history.replaceState(null, "", url);
    setConfig(next);
  };
  const quickRegister = () => mayCreate && perform("registerWorkstation", { workstationId: `desk-${live.identity.slice(0, 8)}`, name: "My workstation" }, "Workstation created. You are its owner.");
  if (new URLSearchParams(location.search).get("view") === "desk" && !targetUnavailable) {
    return <div className="airlock-app"><DeskView station={station} role={role} identity={live.identity} connected={connected} pending={pending} perform={perform} now={now}
      incidents={incidents} events={scoped.events} audit={scoped.audit} runs={scoped.runs} steps={scoped.steps} acknowledgements={scoped.acknowledgements} locks={scoped.locks} /></div>;
  }
  const expiresIn = armed ? Math.max(0, Math.round((milliseconds(session?.expiresAtMs) - now) / 1000)) : 0;
  const needsRecovery = connected && !owner && !mayCreate;
  return <div className="airlock-app cc-app"><header className="al-header cc-header"><a className="al-brand" href={`?product=airlock&client=${encodeURIComponent(config.profile)}&airlockUri=${encodeURIComponent(config.uri)}&airlockDatabase=${encodeURIComponent(config.database)}&workstation=${encodeURIComponent(station?.id || stationId)}`}><AirlockMark /><div><span>LOCKI <b>AIRLOCK</b></span><small>Workstation security command center</small></div></a><div className="al-header-actions"><span className={`al-connection-pill ${mac.status === "live" ? "is-connected" : ""}`} title="This Mac's security backend (sensors, voice, stage)">{mac.status === "live" ? <Server size={13} /> : <ServerOff size={13} />}<span>{mac.status === "live" ? "Mac sensors live" : mac.status === "offline" ? "Mac backend offline" : "Connecting to Mac"}</span></span><button className={`al-connection-pill ${connected ? "is-connected" : ""}`} aria-label={`Database connection: ${connected ? "connected" : live.status}. ${config.database}`} title={`${config.uri} / ${config.database}`} onClick={() => setDialog("connect")}>{connected ? <Wifi size={13} /> : <WifiOff size={13} />}<span>{connected ? "SpacetimeDB connected" : live.status === "offline" ? "SpacetimeDB offline" : "Connecting to SpacetimeDB"}</span><ChevronDown size={12} /></button><button className="al-text-button" onClick={() => setDialog("recover")}><KeyRound size={13} />Recover owner access</button><button className="al-profile" aria-label={`Identity settings for ${config.profile}`} title={`Public identity: ${live.identity || "not connected"}`} onClick={() => setDialog("connect")}><span className="al-avatar">{config.profile.slice(0, 1).toUpperCase()}</span><span>{config.profile}<small>{words(role)}</small></span><Settings2 size={14} /></button></div></header>
    <main className="al-main cc-main">
      {needsRecovery && <div className="cc-recovery-banner" role="status"><KeyRound size={20} /><div><strong>{targetUnavailable ? `Workstation ${stationId} isn't available to this browser identity yet` : "This browser isn't signed in as the workstation owner"}</strong><p>Recover your existing owner access. This verifies your existing credential; it never creates a workstation or a new identity.</p></div><Button kind="primary" onClick={() => setDialog("recover")}>Recover existing owner access</Button></div>}
      <section className="cc-band">
        <div className="cc-band-status">
          <div className="al-inline"><Badge tone={currentIncident ? "magenta" : armed ? "amber" : "quiet"} dot>{status}</Badge>{station && degraded && <Badge tone="amber">{connected ? "Degraded coverage" : "Coverage unverified"}</Badge>}{liveS?.simulated && <Badge tone="amber">Synthetic session</Badge>}</div>
          <h1>{title}</h1>
          <dl className="cc-session">
            <div><dt>Workstation</dt><dd>{stations.length > 1 ? <select className="cc-select" aria-label="Select workstation" value={station?.id || ""} onChange={e => { setStationId(e.target.value); setIncidentId(""); const url = new URL(location.href); url.searchParams.set("workstation", e.target.value); history.replaceState(null, "", url); }}>{targetUnavailable && <option value="" disabled>Requested workstation unavailable</option>}{stations.map(s => <option key={s.id} value={s.id}>{s.name} · {s.id}</option>)}</select> : station ? <>{station.name} <code>{station.id}</code></> : targetUnavailable ? <code>{stationId} · unverified</code> : "None selected"}</dd></div>
            <div><dt>Session</dt><dd>{!connected || !station ? "Unverified" : armed ? <><code>{session?.id}</code> · {session?.simulated ? "synthetic" : "live"}</> : "Disarmed"}</dd></div>
            <div><dt>{armed ? "Expires in" : "Role"}</dt><dd>{armed ? `${Math.floor(expiresIn / 60)}:${String(expiresIn % 60).padStart(2, "0")}` : words(role)}</dd></div>
            <div><dt>Auto-lock</dt><dd>{liveS ? liveS.autoLockEnabled ? "Enabled" : "Disabled" : "Disabled"}</dd></div>{currentIncident && <div><dt>Incident severity (SpacetimeDB)</dt><dd className="al-text-magenta">{words(currentIncident.severity)}</dd></div>}
          </dl>
          <div className="al-primary-actions cc-actions">{station ? <><Button kind={armed ? "default" : "primary"} disabled={!connected || !owner || pending} onClick={() => armed ? perform("disarmSession", { workstationId: station.id }, "Workstation disarmed by its owner.") : setDialog("arm")}>{armed ? <ShieldOff size={17} /> : <ShieldCheck size={17} />}{armed ? "Disarm workstation" : "Arm workstation"}</Button><Button kind="warning" disabled={!connected || !owner || !armed || pending} onClick={() => perform("panicSession", { workstationId: station.id, reason: "Owner requested a security alert from the authenticated console." }, "Owner panic intent committed. Lock execution still requires consent and the Mac adapter.")}><ShieldAlert size={15} />Panic</Button><Button disabled={!connected || !owner} onClick={() => setDialog("access")}><Users size={15} />Enroll identity</Button>{owner && <IncidentAlerts incidents={liveId ? incSplit.current : incidents} acknowledgements={scoped.acknowledgements} workstation={String(station.id)} profile={config.profile} now={now} onSelect={setIncidentId} />}{!owner && <span className="al-small al-muted">Only the owner can arm or disarm.</span>}</> : targetUnavailable ? <Button kind="primary" onClick={() => setDialog("recover")}>Recover this workstation’s owner access</Button> : mayCreate ? <><Button kind="primary" disabled={!connected || pending} onClick={quickRegister}><Plus size={17} />Create my workstation</Button><button className="al-text-button" disabled={!connected} onClick={() => setDialog("register")}>Use a custom workstation ID</button></> : null}</div>
        </div>
        <StageTrack link={mac} />{historyOpen > 0 && <button className="cc-history-note" onClick={() => openHistory()}>{historyOpen} open incident{historyOpen === 1 ? "" : "s"} from past sessions · in History, not current</button>}
      </section>

      {!connected && <div className="al-connection-banner"><Unplug size={18} /><div><strong>{live.status === "offline" ? "Database connection unavailable" : "Establishing a private subscription"}</strong><p>{live.error || `Connecting to ${config.uri} / ${config.database}.`}</p></div><Button kind="ghost" onClick={live.reconnect}><RefreshCw size={14} />Retry</Button><Button onClick={() => setDialog("connect")}>Connection settings</Button></div>}
      {connected && mayCreate && <div className="al-onboarding"><KeyRound size={21} /><div><strong>{config.profile === "reviewer" ? "This reviewer identity needs enrollment" : "Connected with a distinct identity"}</strong><p>{config.profile === "reviewer" ? "Copy your public identity and have the workstation owner grant reviewer access. Your cases appear automatically after enrollment." : "Create a workstation to become its owner, or recover your existing owner access if this workstation already exists."}</p></div><Button onClick={copyIdentity}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "Copied" : "Copy public identity"}</Button><Button onClick={() => setDialog("recover")}><KeyRound size={14} />Recover existing owner access</Button></div>}
      {station && owner && <VoiceArmAuthorization key={`${station.id}:${live.connectionId}`} station={station}
        devices={scoped.devices} snapshots={scoped.baselines} health={scoped.health} grants={scoped.armAuthorizations}
        previous={sessions[0]} now={now} connected={connected} available={live.armAuthorizationAvailable}
        identity={live.identity} connectionId={live.connectionId} pending={pending} perform={perform} />}

      <div className="cc-grid">
        <div className="cc-col cc-col-main"><LiveView link={mac} now={now} armedAtMs={macS?.armedAtMs} />
          <div className="cc-viewbar" role="tablist" aria-label="Live session or history"><button role="tab" aria-selected={view === "live"} onClick={() => { setView("live"); setIncidentId(""); }}><span className="cc-live-dot" />Live session{liveS ? ` · ${evSplit.current.length} events · ${incSplit.current.length} incidents` : !station ? " · unverified" : " · not armed"}</button><button role="tab" aria-selected={view === "history"} onClick={() => openHistory(pastId)}>History · {past.length} past session{past.length === 1 ? "" : "s"}{historyOpen ? ` · ${historyOpen} open` : ""}</button></div>
          {view === "live" ? !connected ? <SessionCard tone="amber" title="Session state unverified" text="The console isn't subscribed to SpacetimeDB, so the current session can't be confirmed." />
            : !station ? <SessionCard tone="amber" title="Session state unverified" text="This browser identity can't see the workstation, so its session can't be shown. Recover owner access to see the live session." />
            : !liveS ? <SessionCard title={session && session.state === "armed" ? "The last session has expired" : "Workstation disarmed"} text={`No live session. ${session ? `Last session ${shortId(session.id)} ${session.state === "armed" ? `expired ${clock(session.expiresAtMs)}` : `ended ${clock(session.disarmedAtMs || session.expiresAtMs)}`}. ` : ""}Arm to start a new monitored session. Past sessions stay in History.`} />
            : evSplit.current.length || incSplit.current.length ? <Timeline events={evSplit.current} audit={liveAudit} incidents={incSplit.current} selectedIncidentId={selectedIncident?.id || ""} onSelect={setIncidentId} />
            : <SessionCard tone="cyan" title="Monitoring your workstation. No incidents in this session." text={`Session ${shortId(liveS.id)} · armed ${clock(liveS.armedAtMs)} · ${liveS.simulated ? "synthetic rehearsal" : "live, simulated=false"}. New camera, USB and app observations for this session appear here as SpacetimeDB commits them.`} />
          : <section className="cc-history"><header><strong>Historical records</strong><span>From past sessions. Not current activity. Nothing here is changed by viewing it.</span></header>
              <div className="cc-past-list"><button aria-pressed={!pastId} onClick={() => openHistory("")}>All past sessions</button>{past.map(p => <button key={String(p.session.id)} aria-pressed={pastId === p.session.id} onClick={() => openHistory(String(p.session.id))} title={String(p.session.id)}><code>{shortId(p.session.id)}</code><span>{clock(p.session.armedAtMs)}{p.ended ? ` → ${clock(p.ended)}` : ""}</span><span>{p.events} events · {p.incidents} incidents{p.open ? ` · ${p.open} open` : ""}</span><Badge tone={p.session.simulated ? "amber" : "quiet"}>{p.session.simulated ? "Synthetic" : "Live record"}</Badge></button>)}</div>
              <Timeline events={historyEvents} audit={liveS ? scoped.audit.filter(a => milliseconds(a.createdAtMs) < milliseconds(liveS.armedAtMs)) : scoped.audit} incidents={historyIncidents} selectedIncidentId={selectedIncident?.id || ""} onSelect={setIncidentId} /></section>}
        </div>
        <div className="cc-col"><ResponsePanel link={mac} dbLocks={scoped.locks} sessionId={liveId} macSession={macS}><MacActivity link={mac} armedAtMs={macS?.armedAtMs} /></ResponsePanel><NemotronPanel now={now} incidentIds={new Set(view === "live" ? incSplit.current.map(i => String(i.id)) : selectedIncident ? [String(selectedIncident.id)] : [])} scope={view} />{owner && station && <InvestigatorAccess station={station} incidents={viewIncidents} events={scoped.events} snapshots={scoped.baselines} owner={owner} identity={live.identity} connectionId={live.connectionId} connected={connected} pending={pending} perform={perform} selectedIncidentId={selectedIncident?.id} />}<ReviewPanel key={selectedIncident?.id || "no-incident"} incident={selectedIncident} locks={scoped.locks} owner={owner} reviewer={reviewer} connected={connected} pending={pending} perform={perform} />{visibleGate && scoped.runs.length > 0 && <Investigation visible={visibleGate} runs={scoped.runs} steps={scoped.steps} incidentId={selectedIncident?.id || ""} />}</div>
        <div className="cc-col cc-col-side">{owner ? <RecordedVoicePanel /> : <Panel title="Recorded Voice Input" icon={<Radio size={16} />}><p className="al-panel-note">Available to the workstation owner. Recover owner access to use recorded commands and STOP SPEAKING.</p></Panel>}<SensorHealth link={mac} dbConnected={connected} now={now}><div className="cc-published"><h4>Published to SpacetimeDB</h4><SensorRow icon={<Camera size={17} />} title="Desk camera" health={camera} now={now} enrolled={deviceFor("jetson")} connected={connected} /><SensorRow icon={<Cable size={17} />} title="USB watcher" health={usb} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Laptop size={17} />} title="App watcher" health={appHealth} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Activity size={17} />} title="Model adapter" health={model} now={now} enrolled={deviceFor("service")} connected={connected} />{capacity && <p className="al-small al-text-amber" role="alert">{capacity.detail || "Security event capacity reached. Owner review and rearm required."}</p>}</div></SensorHealth><FaceIdCard link={mac} />{owner && <DispatchPanel incidentId={selectedIncident?.id ? String(selectedIncident.id) : undefined} />}
          <Panel title="Response policy" icon={<SlidersHorizontal size={16} />} aside={<button className="al-icon-button" disabled={!owner || !connected} title="Configure response policy" aria-label="Configure response policy" onClick={() => setDialog("policy")}><Settings2 size={14} /></button>}><div className="al-policy-rows"><div><span>Automatic screen lock</span><Badge tone={liveS?.autoLockEnabled ? "amber" : "quiet"}>{liveS ? liveS.autoLockEnabled ? "Enabled" : "Disabled" : station?.autoLockEnabled ? "Next session: enabled" : "Disabled"}</Badge></div><div><span>Protected tool actions</span><Badge tone="cyan">Approval required</Badge></div><div><span>Physical observation alone</span><strong>No automatic lock</strong></div><div><span>Open incidents</span><strong className={unresolved.length ? "al-text-magenta" : ""}>{unresolved.length} this session · {historyOpen} past</strong></div></div><details className="al-policy-details"><summary>Frozen device and app allowlists<ChevronDown size={13} /></summary><h4>USB baseline</h4><BaselineList value={liveS?.baselineJson} empty="No approved USB keys in this session" /><h4>Approved applications</h4><BaselineList value={liveS?.approvedAppsJson} empty="No approved app IDs in this session" /><p>Policy {shortId(liveS?.ownerPolicyHash || station?.ownerPolicyHash)}</p></details></Panel>
          <ProtectedActions key={station?.id || "no-workstation"} visible={visibleGate} actions={scoped.actions} approvals={scoped.approvals} identity={live.identity} ownerIdentity={textValue(station?.owner)} canApprove={canApprove} connected={connected} pending={pending} perform={perform} now={now} onRequest={station && (owner || role === "service") ? () => setDialog("action") : undefined} /><VoiceBridge />
          <div className="al-identity-card"><Fingerprint size={18} /><div><strong>This console's identity</strong><button className="al-identity-copy" onClick={copyIdentity} title={live.identity}><code>{shortId(live.identity || "Not connected")}</code>{copied ? <Check size={12} /> : <Copy size={12} />}</button><span>{config.profile} profile · permissions from server</span></div></div></div>
      </div>

      <section className="cc-rehearsal"><header><FlaskConical size={16} /><strong>Synthetic rehearsal</strong><Badge tone="amber">Not live data</Badge><span>Labeled fixtures for practice only. They never represent a real sensor, camera or device.</span></header><FixturePanel station={station} session={session} devices={scoped.devices} owner={owner} role={role} connected={connected} pending={pending} perform={perform} /></section>
      <footer className="al-page-footer"><span><AirlockMark size={15} />LOCKI AIRLOCK</span><span>SpacetimeDB owns the security state · this Mac reports its sensors</span><span>{connected ? `Subscribed · ${clock(live.lastSyncedAt)}` : "Reconnecting automatically"}</span></footer>
    </main>{notice && <div className={`al-notice ${notice.error ? "is-error" : ""}`} role={notice.error ? "alert" : "status"}>{notice.error ? <ShieldAlert size={17} /> : <Check size={17} />}<span>{notice.text}</span><button aria-label="Dismiss notification" onClick={() => setNotice(null)}><X size={15} /></button></div>}
    {dialog === "recover" && <OwnerRecovery config={config} workstationId={station?.id || stationId} onClose={() => setDialog(null)} />}{dialog === "connect" && <ConnectionForm config={config} identity={live.identity} onSave={saveConnection} onClose={() => setDialog(null)} />}{dialog === "register" && mayCreate && <RegisterForm perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "arm" && station && <ArmForm station={station} previous={sessions[0]} snapshots={scoped.baselines} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "access" && station && <AccessForm station={station} identity={live.identity} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "policy" && station && <PolicyForm station={station} perform={perform} pending={pending} onClose={() => setDialog(null)} />}{dialog === "action" && station && <RequestActionForm station={station} service={role === "service"} incidentId={selectedIncident?.id || ""} perform={perform} pending={pending} onClose={() => setDialog(null)} />}
  </div>;
}

export default function AirlockApp() { return <ToastProvider><ControlRoom /></ToastProvider>; }
