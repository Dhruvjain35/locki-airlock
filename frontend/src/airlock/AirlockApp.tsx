import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, Cable, Camera, Check, ChevronDown, Copy, Fingerprint, FlaskConical, KeyRound, Laptop, LockKeyhole, Radio, Settings2, ShieldAlert, SlidersHorizontal, Users, X } from "lucide-react";
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
import { DispatchPanel } from "./DispatchPanel";
import { RecordedVoicePanel } from "./RecordedVoicePanel";
import { healthStatus, latestHealth } from "./coverage";
import { FaceIdCard, SensorHealth } from "./CommandCenter";
import { useMacSecurity } from "./macSecurity";
import { HANDOFF_MAX_AGE_MS, markHandoffHandled, readHandledHandoff, readTakeover } from "./takeover";
import { FIXTURE_ACTIVE, installFixtureFetch, useFixtureAirlock } from "./fixture";
import { bySession, liveSession, pastSessions, pickIncident } from "./liveScope";
import { type InvestigatorAccessContext, changeInvestigatorAccess, investigatorAccessReview } from "./investigatorAccessPolicy";
import { EXPLAIN_QUESTION, INVESTIGATOR_IDENTITY, evidenceGrantState, explainBlocker, explainScope, grantApprovedAt, openGrantIncidentIds, protectionState } from "./protection";
import { type ConsoleActions, type ConsoleModel, type ConsoleView, type ExplainRun, type StopState, Console, ConsentDialog, StartDialog, StopDialog } from "./Console";
import { api } from "../services/api";
import "./airlock.css";
import "./commandCenter.css";

// Dev-only design review data (?fixture=… under `vite dev`); production builds always use the live subscription.
if (import.meta.env.DEV && FIXTURE_ACTIVE) installFixtureFetch();
const useLive = import.meta.env.DEV && FIXTURE_ACTIVE ? useFixtureAirlock : useAirlock;

type Dialog = "connect" | "register" | "arm" | "start" | "access" | "policy" | "action" | "recover" | null;

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
  const live = useLive(config);
  const mac = useMacSecurity();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [stationId] = useState(new URLSearchParams(location.search).get("workstation") || "");
  const [incidentId, setIncidentId] = useState("");
  const [pastId, setPastId] = useState("");
  const [cview, setCview] = useState<ConsoleView>("protection");
  const [drawer, setDrawer] = useState(false);
  const [run, setRun] = useState<ExplainRun>({ phase: "idle" });
  const [stop, setStop] = useState<StopState>({ phase: "idle", grants: 0 });
  const [revoking, setRevoking] = useState(false);
  const [voiceOutput, setVoiceOutput] = useState<string | null>(null);
  const [voiceWarning, setVoiceWarning] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => { const id = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(id); }, []);
  useEffect(() => { if (!notice) return; const id = window.setTimeout(() => setNotice(null), notice.error ? 12000 : 5000); return () => window.clearTimeout(id); }, [notice]);
  useEffect(() => {
    let stopped = false;
    const load = () => api.voiceDevices().then(d => { if (!stopped) setVoiceOutput(d?.available ? (d.outputs ?? []).find((o: any) => o?.default)?.name ?? null : null); }).catch(() => { if (!stopped) setVoiceOutput(null); });
    const loadWarning = () => api.runtimeState().then(st => { if (!stopped) { const w = st?.voice?.output?.warning; setVoiceWarning(typeof w === "string" && w ? (w.startsWith("muted") ? "is muted in macOS. Unmute with the volume keys so warnings are heard." : w) : null); } }).catch(() => undefined);
    load(); loadWarning(); const id = window.setInterval(() => { load(); loadWarning(); }, 15000);
    return () => { stopped = true; window.clearInterval(id); };
  }, []);
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
  const incidents = [...scoped.incidents].sort((a, b) => Number(b.createdAtMs) - Number(a.createdAtMs));
  // Live session vs history: only rows carrying the SpacetimeDB-active, unexpired session's id are current. Everything else is history.
  const liveS = liveSession(sessions, station?.activeSessionId, now);
  const liveId = liveS?.id as string | undefined;
  const evSplit = bySession(scoped.events, liveId);
  const incSplit = bySession(incidents, liveId);
  const past = pastSessions(sessions, scoped.events, incidents, liveId);
  const historyEvents = pastId ? evSplit.history.filter(e => e.sessionId === pastId) : evSplit.history;
  const historyIncidents = pastId ? incSplit.history.filter(i => i.sessionId === pastId) : incSplit.history;
  const historySelected = pickIncident(historyIncidents, incidentId);
  const historyOpen = incSplit.history.filter(i => i.state !== "resolved").length;
  const incident = incSplit.current.filter(i => i.state !== "resolved")[0];
  const incidentKey = incident ? String(incident.id) : undefined;
  const deviceFor = (r: string) => scoped.devices.some(d => d.role === r && d.enabled);
  const camera = latestHealth(scoped.health, scoped.devices, "camera");
  const usb = latestHealth(scoped.health, scoped.devices, "usb");
  const appHealth = latestHealth(scoped.health, scoped.devices, "apps");
  const model = latestHealth(scoped.health, scoped.devices, "model");
  const jetsonHealth = [...scoped.health].filter(h => String(h.deviceId).startsWith("jetson")).sort((a, b) => milliseconds(b.lastSeenMs) - milliseconds(a.lastSeenMs))[0];
  const visibleGate = ["owner", "reviewer", "service"].includes(role);
  const perform: Perform = async (reducer, args, success) => {
    try { await live.call(reducer, args); if (success) setNotice({ text: success, error: false }); return true; }
    catch (e) { setNotice({ text: e instanceof Error ? e.message : String(e), error: true }); return false; }
  };
  const copyIdentity = async () => { if (!live.identity) return; try { await navigator.clipboard.writeText(live.identity); setCopied(true); setNotice({ text: "Public identity copied.", error: false }); window.setTimeout(() => setCopied(false), 2000); } catch { setDialog("connect"); } };
  const saveConnection = (next: ConnectionConfig) => {
    localStorage.setItem("airlock.connection.v1", JSON.stringify({ uri: next.uri, database: next.database }));
    const url = new URL(location.href); url.searchParams.set("client", next.profile); url.searchParams.set("airlockUri", next.uri); url.searchParams.set("airlockDatabase", next.database); history.replaceState(null, "", url);
    setConfig(next);
  };

  // Explain incident: owner consent for exactly this incident's evidence, then the Mac's own typed-command path to Mustafa's investigator.
  const scope = incident && station ? explainScope(incident, scoped.events, scoped.baselines) : null;
  const grant = incidentKey ? evidenceGrantState(scoped.audit, incidentKey) : "none";
  const grantAt = incidentKey ? grantApprovedAt(scoped.audit, incidentKey) : null;
  const explainBlocked = !owner ? "Only the workstation owner can explain incidents." : explainBlocker(incident, liveId, mac);
  const investigating = ["granting", "waiting_grant", "investigating"].includes(run.phase) && run.incidentId === incidentKey;
  const accessContext = (action: "grant" | "revoke", id: string, eventIds: string[] = [], includeBaseline = false): InvestigatorAccessContext => ({
    station: station!, incidents: scoped.incidents, events: scoped.events, snapshots: scoped.baselines, owner, identity: live.identity, connectionId: live.connectionId,
    connected, incidentId: id, investigatorIdentity: INVESTIGATOR_IDENTITY, selectedEventIds: eventIds, includeBaseline, action,
  });
  const investigate = async (id: string) => {
    const startedAt = Date.now();
    setRun({ phase: "investigating", incidentId: id, startedAt });
    try {
      const result = await Promise.race([api.command(EXPLAIN_QUESTION), new Promise<never>((_, reject) => window.setTimeout(() => reject(new Error("Nemotron didn't answer within 60 s. Nothing was presented as its result.")), 60_000))]);
      const n = result?.data?.nemotron;
      const answered = result.ok === true && n?.incident_id === id && n?.evidence_badge === "LIVE";
      setRun({ phase: answered ? "done" : "failed", incidentId: id, startedAt, finishedAt: Date.now(), result, error: answered ? undefined : n?.error || result.message });
    } catch (e) { setRun({ phase: "failed", incidentId: id, startedAt, finishedAt: Date.now(), error: e instanceof Error ? e.message : String(e) }); }
  };
  const explain = () => {
    setCview("investigation");
    if (!incidentKey || explainBlocked || investigating) return;
    if (grant === "granted") void investigate(incidentKey); else setRun({ phase: "consent", incidentId: incidentKey });
  };
  const approve = async () => {
    if (!incidentKey || !station || !scope) return;
    const ctx = accessContext("grant", incidentKey, scope.eventIds, Boolean(scope.baseline));
    const review = investigatorAccessReview(ctx);
    if (!review) { setRun({ phase: "failed", incidentId: incidentKey, error: "The evidence scope couldn't be verified, so nothing was shared." }); return; }
    setRun({ phase: "granting", incidentId: incidentKey });
    const outcome = await changeInvestigatorAccess(ctx, review.fingerprint, perform);
    if (outcome.status !== "committed") { setRun({ phase: "failed", incidentId: incidentKey, error: "SpacetimeDB didn't confirm the access approval, so nothing was investigated." }); return; }
    setRun({ phase: "waiting_grant", incidentId: incidentKey, startedAt: Date.now() });
  };
  const waitingFor = run.phase === "waiting_grant" ? run.incidentId : undefined;
  const investigateRef = useRef(investigate); investigateRef.current = investigate;
  useEffect(() => {
    if (!waitingFor) return;
    if (waitingFor === incidentKey && grant === "granted") { void investigateRef.current(waitingFor); return; }
    const t = window.setTimeout(() => void investigateRef.current(waitingFor), 8000);  // the commit receipt already arrived; the audit row is a second confirmation
    return () => window.clearTimeout(t);
  }, [waitingFor, incidentKey, grant]);

  // Investigator grants have no expiry, so every one still in force is closed by Stop, and offered for revoking while disarmed.
  const openGrants = openGrantIncidentIds(scoped.audit, scoped.incidents);
  const earlierGrants = openGrants.filter(id => scoped.incidents.find(i => String(i.id) === id)?.sessionId !== liveId);
  const revokeIds = async (ids: string[]) => {
    let revoked = 0, failed = 0;
    for (const id of ids) {
      const ctx = accessContext("revoke", id);
      const review = investigatorAccessReview(ctx);
      const outcome = review ? await changeInvestigatorAccess(ctx, review.fingerprint, perform) : { status: "blocked" as const };
      if (outcome.status === "committed") revoked++; else failed++;
    }
    return { revoked, failed };
  };
  const revokeOpenGrants = async () => {
    if (!station || revoking) return;
    setRevoking(true);
    const ids = openGrantIncidentIds(scoped.audit, scoped.incidents);
    const { revoked, failed } = await revokeIds(ids);
    setRevoking(false);
    setNotice({ text: failed ? `Revoked investigator access to ${revoked} of ${ids.length} incidents. ${failed} could not be confirmed.` : `Investigator access revoked for ${revoked} incident${revoked === 1 ? "" : "s"}.`, error: failed > 0 });
  };
  // Stop protection: stop speech, disarm, then revoke every investigator grant still in force.
  const stopProtection = async () => {
    if (!station) return;
    const ids = openGrantIncidentIds(scoped.audit, scoped.incidents);
    setStop(prev => ({ ...prev, phase: "working", grants: ids.length }));
    try { await api.stopSpeaking(); } catch { /* best effort: a stopped speaker never blocks disarming */ }
    const disarmed = await perform("disarmSession", { workstationId: station.id });
    const { revoked, failed } = await revokeIds(ids);
    setRun({ phase: "idle" });
    setStop(prev => ({ ...prev, phase: "done", grants: ids.length, disarmed, revoked, failed }));
  };

  // The native alert window hands off to this owner console: Explain opens the same consent flow, View evidence the Investigation view,
  // Stop the same owner-confirmed Stop dialog. The window itself holds no credentials and changes nothing. Each request acts once; one
  // seen before a reload, or older than a minute, is ignored.
  const handoff = readTakeover(mac)?.handoff ?? null;
  const handledHandoff = useRef<string | null>(readHandledHandoff());
  const handoffActs = useRef({ explain, stop: () => {}, armed: false });
  handoffActs.current = { explain, stop: () => setStop({ phase: "confirm", grants: openGrants.length, earlier: earlierGrants.length }), armed: Boolean(liveS) };
  useEffect(() => {
    if (!handoff || typeof handoff.nonce !== "string" || handledHandoff.current === handoff.nonce) return;
    handledHandoff.current = handoff.nonce;
    markHandoffHandled(handoff.nonce);
    if (!(Date.now() - Number(handoff.at_ms) <= HANDOFF_MAX_AGE_MS)) return;
    const act = handoffActs.current;
    if (handoff.view === "explain") act.explain();
    else if (handoff.view === "stop" && act.armed) { setCview("protection"); act.stop(); }
    else setCview("investigation");
  }, [handoff?.nonce]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (new URLSearchParams(location.search).get("view") === "desk" && !targetUnavailable) {
    return <div className="airlock-app"><DeskView station={station} role={role} identity={live.identity} connected={connected} pending={pending} perform={perform} now={now}
      incidents={incidents} events={scoped.events} audit={scoped.audit} runs={scoped.runs} steps={scoped.steps} acknowledgements={scoped.acknowledgements} locks={scoped.locks} /></div>;
  }

  const model_: ConsoleModel = {
    now, mac, db: { connected, status: live.status, error: live.error || undefined, database: config.database },
    owner, role, profile: config.profile, identity: live.identity, station, stationLabel: station ? String(station.name || station.id) : targetUnavailable ? `${stationId} · unverified` : "No workstation",
    live: liveS, lastSession: sessions[0], state: protectionState({ connected, owner, armed: Boolean(liveS), incidentOpen: Boolean(incident), investigating }),
    incident, incidents: incSplit.current, events: evSplit.current, scope, grant, grantAt, explainBlocked, run, jetsonHealth, voiceOutput, voiceWarning,
    history: { sessions: past.length, open: historyOpen }, openGrants: openGrants.length, revoking, pending,
  };
  const actions: ConsoleActions = {
    setView: v => { setCview(v); if (v === "history") setIncidentId(""); }, start: () => setDialog("start"), stop: () => setStop({ phase: "confirm", grants: openGrants.length, earlier: earlierGrants.length }),
    explain, approve: () => void approve(), cancelConsent: () => setRun({ phase: "idle" }), retry: explain,
    stopSpeaking: () => { void api.stopSpeaking().catch(() => undefined); }, revokeOpenGrants: () => void revokeOpenGrants(), openAdvanced: () => setDrawer(true), openRecover: () => setDialog("recover"), openConnection: () => setDialog("connect"),
    copyIdentity: () => void copyIdentity(), reconnect: live.reconnect,
  };

  const historyNode = <>
    <section className="cc-history"><header><strong>Past sessions</strong><span>Read-only records from earlier sessions. Viewing them changes nothing.</span></header>
      <div className="cc-past-list"><button aria-pressed={!pastId} onClick={() => { setPastId(""); setIncidentId(""); }}>All past sessions</button>{past.map(p => <button key={String(p.session.id)} aria-pressed={pastId === p.session.id} onClick={() => { setPastId(String(p.session.id)); setIncidentId(""); }} title={String(p.session.id)}><code>{shortId(p.session.id)}</code><span>{clock(p.session.armedAtMs)}{p.ended ? ` → ${clock(p.ended)}` : ""}</span><span>{p.events} events · {p.incidents} incidents{p.open ? ` · ${p.open} open` : ""}</span><Badge tone={p.session.simulated ? "amber" : "quiet"}>{p.session.simulated ? "Synthetic" : "Live record"}</Badge></button>)}</div>
      <Timeline events={historyEvents} audit={liveS ? scoped.audit.filter(a => milliseconds(a.createdAtMs) < milliseconds(liveS.armedAtMs)) : scoped.audit} incidents={historyIncidents} selectedIncidentId={historySelected?.id || ""} onSelect={setIncidentId} /></section>
    {historySelected && <ReviewPanel key={String(historySelected.id)} incident={historySelected} locks={scoped.locks} owner={owner} reviewer={reviewer} connected={connected} pending={pending} perform={perform} />}
    {visibleGate && scoped.runs.length > 0 && <Investigation visible={visibleGate} runs={scoped.runs} steps={scoped.steps} incidentId={historySelected?.id || ""} />}
  </>;

  const drawerNode = <>
    <div className="al-primary-actions">{station && <><Button disabled={!connected || !owner || pending || Boolean(liveS)} onClick={() => setDialog("arm")}><Settings2 size={15} />Advanced arm…</Button><Button kind="warning" disabled={!connected || !owner || !liveS || pending} onClick={() => perform("panicSession", { workstationId: station.id, reason: "Owner requested a security alert from the authenticated console." }, "Owner panic intent committed. Lock execution still requires consent and the Mac adapter.")}><ShieldAlert size={15} />Panic</Button><Button disabled={!connected || !owner} onClick={() => setDialog("access")}><Users size={15} />Enroll identity</Button></>}{mayCreate && <Button kind="primary" disabled={!connected || pending} onClick={() => setDialog("register")}>Register a workstation</Button>}<Button onClick={() => setDialog("recover")}><KeyRound size={15} />Recover owner access</Button></div>
    {station && owner && <VoiceArmAuthorization key={`${station.id}:${live.connectionId}`} station={station} devices={scoped.devices} snapshots={scoped.baselines} health={scoped.health} grants={scoped.armAuthorizations} previous={sessions[0]} now={now} connected={connected} available={live.armAuthorizationAvailable} identity={live.identity} connectionId={live.connectionId} pending={pending} perform={perform} />}
    {owner ? <RecordedVoicePanel /> : <Panel title="Recorded Voice Input" icon={<Radio size={16} />}><p className="al-panel-note">Available to the workstation owner.</p></Panel>}
    {owner && station && <InvestigatorAccess station={station} incidents={incidents} events={scoped.events} snapshots={scoped.baselines} owner={owner} identity={live.identity} connectionId={live.connectionId} connected={connected} pending={pending} perform={perform} selectedIncidentId={incident?.id} />}
    <ReviewPanel key={incidentKey || "no-incident"} incident={incident} locks={scoped.locks} owner={owner} reviewer={reviewer} connected={connected} pending={pending} perform={perform} />
    <SensorHealth link={mac} dbConnected={connected} now={now}><div className="cc-published"><h4>Published to SpacetimeDB</h4><SensorRow icon={<Camera size={17} />} title="Desk camera" health={camera} now={now} enrolled={deviceFor("jetson")} connected={connected} /><SensorRow icon={<Cable size={17} />} title="USB watcher" health={usb} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Laptop size={17} />} title="App watcher" health={appHealth} now={now} enrolled={deviceFor("mac")} connected={connected} /><SensorRow icon={<Activity size={17} />} title="Model adapter" health={model} now={now} enrolled={deviceFor("service")} connected={connected} /></div></SensorHealth>
    <FaceIdCard link={mac} />
    {owner && <DispatchPanel incidentId={incidentKey} />}
    <Panel title="Response policy" icon={<SlidersHorizontal size={16} />} aside={<button className="al-icon-button" disabled={!owner || !connected} title="Configure response policy" aria-label="Configure response policy" onClick={() => setDialog("policy")}><Settings2 size={14} /></button>}><div className="al-policy-rows"><div><span>Automatic screen lock</span><Badge tone={liveS?.autoLockEnabled ? "amber" : "quiet"}>{liveS ? liveS.autoLockEnabled ? "Enabled" : "Disabled" : station?.autoLockEnabled ? "Next session: enabled" : "Disabled"}</Badge></div><div><span>Protected tool actions</span><Badge tone="cyan">Approval required</Badge></div><div><span>Physical observation alone</span><strong>No automatic lock</strong></div></div><details className="al-policy-details"><summary>Frozen device and app allowlists<ChevronDown size={13} /></summary><h4>USB baseline</h4><BaselineList value={liveS?.baselineJson} empty="No approved USB keys in this session" /><h4>Approved applications</h4><BaselineList value={liveS?.approvedAppsJson} empty="No approved app IDs in this session" /><p>Policy {shortId(liveS?.ownerPolicyHash || station?.ownerPolicyHash)}</p></details></Panel>
    <ProtectedActions key={station?.id || "no-workstation"} visible={visibleGate} actions={scoped.actions} approvals={scoped.approvals} identity={live.identity} ownerIdentity={textValue(station?.owner)} canApprove={canApprove} connected={connected} pending={pending} perform={perform} now={now} onRequest={station && (owner || role === "service") ? () => setDialog("action") : undefined} />
    <VoiceBridge />
    <div className="al-identity-card"><Fingerprint size={18} /><div><strong>This console's identity</strong><button className="al-identity-copy" onClick={() => void copyIdentity()} title={live.identity}><code>{shortId(live.identity || "Not connected")}</code>{copied ? <Check size={12} /> : <Copy size={12} />}</button><span>{config.profile} profile · permissions from server</span></div></div>
    <section className="cc-rehearsal"><header><FlaskConical size={16} /><strong>Synthetic rehearsal</strong><Badge tone="amber">Not live data</Badge></header><FixturePanel station={station} session={session} devices={scoped.devices} owner={owner} role={role} connected={connected} pending={pending} perform={perform} /></section>
  </>;

  const noticeNode = notice && <div className={`lk-notice ${notice.error ? "is-error" : ""}`} role={notice.error ? "alert" : "status"}>{notice.error ? <ShieldAlert size={16} /> : <Check size={16} />}<span>{notice.text}</span><button aria-label="Dismiss notification" onClick={() => setNotice(null)}><X size={14} /></button></div>;
  const dialogs = <>
    {run.phase === "consent" && run.incidentId === incidentKey && <ConsentDialog m={model_} busy={false} onApprove={() => void approve()} onCancel={() => setRun({ phase: "idle" })} />}
    {stop.phase !== "idle" && <StopDialog state={stop} onConfirm={() => void stopProtection()} onClose={() => { if (stop.phase !== "working") setStop({ phase: "idle", grants: 0 }); }} />}
    {dialog === "start" && station && <StartDialog station={station} snapshots={scoped.baselines} sessions={sessions} now={now} call={live.call} onDone={() => { setDialog(null); setCview("protection"); setNotice({ text: "Protection started. LOCKI is watching this workstation.", error: false }); }} onClose={() => setDialog(null)} />}
    {dialog === "recover" && <div className="airlock-app lk-legacy"><OwnerRecovery config={config} workstationId={station?.id || stationId} onClose={() => setDialog(null)} /></div>}
    {dialog === "connect" && <div className="airlock-app lk-legacy"><ConnectionForm config={config} identity={live.identity} onSave={saveConnection} onClose={() => setDialog(null)} /></div>}
    {dialog === "register" && mayCreate && <div className="airlock-app lk-legacy"><RegisterForm perform={perform} pending={pending} onClose={() => setDialog(null)} /></div>}
    {dialog === "arm" && station && <div className="airlock-app lk-legacy"><ArmForm station={station} previous={sessions[0]} snapshots={scoped.baselines} perform={perform} pending={pending} onClose={() => setDialog(null)} /></div>}
    {dialog === "access" && station && <div className="airlock-app lk-legacy"><AccessForm station={station} identity={live.identity} perform={perform} pending={pending} onClose={() => setDialog(null)} /></div>}
    {dialog === "policy" && station && <div className="airlock-app lk-legacy"><PolicyForm station={station} perform={perform} pending={pending} onClose={() => setDialog(null)} /></div>}
    {dialog === "action" && station && <div className="airlock-app lk-legacy"><RequestActionForm station={station} service={role === "service"} incidentId={incident?.id || ""} perform={perform} pending={pending} onClose={() => setDialog(null)} /></div>}
  </>;
  return <>{import.meta.env.DEV && FIXTURE_ACTIVE && <div className="lk-fixture-banner" role="status">Fixture data for design review. Not live: every action is refused. Remove ?fixture from the URL for the real console.</div>}<Console m={model_} a={actions} view={cview} history={historyNode} drawer={drawerNode} drawerOpen={drawer} closeDrawer={() => setDrawer(false)} notice={noticeNode} dialogs={dialogs} /></>;
}

export default function AirlockApp() { return <ToastProvider><ControlRoom /></ToastProvider>; }
