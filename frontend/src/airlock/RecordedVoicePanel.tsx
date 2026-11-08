import { useEffect, useRef, useState } from "react";
import { FileAudio, Mic, MicOff, Send, Square, X } from "lucide-react";
import { api } from "../services/api";
import { Badge, Button, Panel } from "./ui";
import { addTurn } from "./recordedTurns";

type Turn = { key: string; nemotron?: { investigation_id?: string; status?: string; evidence_badge?: string | null; total_ms?: number; error?: string } | null; file: string; ok: boolean; transcript?: string | null; reply?: string | null; intent?: string | null; error?: string; message?: string; stt_ms?: number; handled_ms?: number; audio?: { seconds: number } };

/** Mustafa's investigation record for one turn, read-only through this Mac's backend (the key never reaches the browser): headline first,
 *  then the grounded facts with their evidence ids, the tools that ran and on what data, and the total time. */
function InvestigationRecord({ id }: { id: string }) {
  const [rec, setRec] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { let stop = false; setRec(null); setError(null); api.investigation(id).then(r => { if (!stop) setRec(r); }).catch(e => { if (!stop) setError(e instanceof Error ? e.message : String(e)); }); return () => { stop = true; }; }, [id]);
  if (error) return <p className="al-small al-text-amber">Investigation record unavailable: {error}</p>;
  if (!rec) return <p className="al-small al-muted">Loading the investigation record…</p>;
  return <div className="al-investigation-record">
    {rec.headline && <p className="al-small"><strong>{rec.headline}</strong></p>}
    {(rec.observed_facts ?? []).map((f: any, n: number) => <p key={n} className="al-small">• {f.fact} {(f.evidence_ids ?? []).map((e: string) => <code key={e}>{e}</code>)}</p>)}
    {(rec.tools ?? []).length > 0 && <p className="al-small al-muted">Tools: {(rec.tools as any[]).map(t => `${t.tool} ${t.status}${t.live_data ? " (live)" : t.live_data === false ? " (not live)" : ""}`).join(" · ")}</p>}
    {rec.total_latency_ms != null && <p className="al-small al-muted">Investigation time {Math.round(rec.total_latency_ms)} ms · {rec.status}</p>}
  </div>;
}

/** Recorded Voice Input: the owner's own spoken commands, recorded somewhere quiet as WAV files, sent in any order. Each one is
 *  transcribed by LOCKI's own speech-to-text and answered fresh by the same conversation, security rules and voice as the microphone.
 *  Labelled PRERECORDED INPUT so judges know the room's microphone is not being used. */
export function RecordedVoicePanel() {
  const [files, setFiles] = useState<File[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [error, setError] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => { try { const s = await api.recordedStatus(); if (!stop) { setStatus(s); setError(null); } } catch (e) { if (!stop) setError(e instanceof Error ? e.message : String(e)); } };
    void load();
    const id = window.setInterval(load, 3000);
    return () => { stop = true; window.clearInterval(id); };
  }, []);
  const send = async (file: File) => {
    setBusy(file.name);
    const key = crypto.randomUUID();  // one stable identity per turn, so a new turn never inherits another's investigation view
    try { const t = await api.sendRecorded(file); setTurns(prev => addTurn<Omit<Turn, "key">>(prev, t, key)); }
    catch (e) { setTurns(prev => addTurn<Omit<Turn, "key">>(prev, { file: file.name, ok: false, error: e instanceof Error ? e.message : String(e) }, key)); }
    finally { setBusy(null); }
  };
  const stt = status?.stt?.state as string | undefined;
  const mic = status?.live_mic;
  return <Panel title="Recorded Voice Input" icon={<FileAudio size={16} />} aside={<Badge tone="amber" dot>Prerecorded input</Badge>}>
    <button className="al-stop-speaking" onClick={() => void api.stopSpeaking()} title="Stops LOCKI's current and queued lines now">
      <Square size={22} fill="currentColor" />STOP SPEAKING
    </button>
    <p className="al-small al-muted">Security warnings still play after a stop.</p>
    <div className="al-inline">
      <Badge tone={stt === "ready" ? "cyan" : "amber"}>Speech-to-text: {stt ?? (error ? "unreachable" : "…")}</Badge>
      <Badge tone="quiet">{mic?.running ? <><Mic size={12} />Live mic {mic.held_for_recorded ? "paused for recording" : mic.mode}</> : <><MicOff size={12} />Live mic off</>}</Badge>
    </div>
    {error && <p className="al-small al-text-amber">The Mac's backend is not answering: {error}</p>}
    <input ref={picker} type="file" accept=".wav,audio/wav,audio/x-wav" multiple hidden onChange={e => { const add = Array.from(e.target.files ?? []); setFiles(prev => [...prev, ...add.filter(f => !prev.some(p => p.name === f.name))]); e.target.value = ""; }} />
    <Button onClick={() => picker.current?.click()}><FileAudio size={15} />Add WAV recordings</Button>
    <div className="al-recordings">{files.map(f => <div key={f.name} className="al-inline al-spread">
      <span className="al-small">{f.name} · {(f.size / 1024).toFixed(0)} KB</span>
      <span className="al-inline"><Button kind="primary" disabled={busy !== null || stt !== "ready"} onClick={() => void send(f)}><Send size={14} />{busy === f.name ? "Listening…" : "Send to LOCKI"}</Button>
        <button className="al-text-button" aria-label={`remove ${f.name}`} onClick={() => setFiles(prev => prev.filter(p => p !== f))}><X size={13} /></button></span>
    </div>)}</div>
    {turns.map(t => <div key={t.key} className="al-recorded-turn">
      <div className="al-inline al-spread"><Badge tone={t.ok ? "cyan" : "amber"}>{t.file}</Badge><span className="al-small">{t.stt_ms != null ? `heard in ${Math.round(t.stt_ms)} ms` : ""}{t.handled_ms != null ? ` · answered in ${Math.round(t.handled_ms)} ms` : ""}</span></div>
      {t.transcript != null && <p className="al-small">Recognized: <strong>“{t.transcript}”</strong>{t.intent ? ` · ${t.intent}` : ""}</p>}
      {t.nemotron?.status === "unavailable" && <p className="al-small al-text-amber">Nemotron unavailable for this turn: {t.nemotron.error}</p>}
      {t.nemotron?.investigation_id && t.nemotron.status !== "unavailable" && <p className="al-small"><Badge tone={t.nemotron.evidence_badge === "LIVE" ? "cyan" : "amber"}>Nemotron {t.nemotron.evidence_badge === "LIVE" ? "LIVE" : "not live"}</Badge> {t.nemotron.status} · {t.nemotron.investigation_id}{t.nemotron.total_ms != null ? ` · ${Math.round(t.nemotron.total_ms)} ms` : ""}</p>}
      {t.nemotron?.investigation_id && t.nemotron.evidence_badge === "LIVE" && <InvestigationRecord key={t.nemotron.investigation_id} id={t.nemotron.investigation_id} />}
      {t.reply && <p className="al-small">LOCKI: {t.reply}</p>}
      {t.error && <p className="al-small al-text-amber">{t.error}</p>}
      {!t.error && !t.ok && t.message && <p className="al-small al-muted">{t.message}</p>}
    </div>)}
  </Panel>;
}
