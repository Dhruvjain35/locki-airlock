import { useEffect, useRef, useState } from "react";
import { FileAudio, Mic, MicOff, Send, Square, X } from "lucide-react";
import { api } from "../services/api";
import { Badge, Button, Panel } from "./ui";

type Turn = { nemotron?: { investigation_id?: string; status?: string; evidence_badge?: string; total_ms?: number } | null; file: string; ok: boolean; transcript?: string | null; reply?: string | null; intent?: string | null; error?: string; message?: string; stt_ms?: number; handled_ms?: number; audio?: { seconds: number } };

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
    try { const t = await api.sendRecorded(file); setTurns(prev => [t, ...prev].slice(0, 12)); }
    catch (e) { setTurns(prev => [{ file: file.name, ok: false, error: e instanceof Error ? e.message : String(e) }, ...prev].slice(0, 12)); }
    finally { setBusy(null); }
  };
  const stt = status?.stt?.state as string | undefined;
  const mic = status?.live_mic;
  return <Panel title="Recorded Voice Input" icon={<FileAudio size={16} />} aside={<Badge tone="amber" dot>Prerecorded input</Badge>}>
    <button className="al-stop-speaking" onClick={() => void api.stopSpeaking()} title="Stops LOCKI's current and queued lines now">
      <Square size={22} fill="currentColor" />STOP SPEAKING
    </button>
    <p className="al-small al-muted">Security warnings still play after a stop.</p>
    <p className="al-small al-muted">Recordings support conversation. Use the live microphone or owner controls to arm or disarm.</p>
    <div className="al-inline">
      <Badge tone={stt === "ready" ? "cyan" : "amber"}>Speech-to-text: {stt ?? (error ? "unreachable" : "…")}</Badge>
      <Badge tone="quiet">{mic?.running ? <><Mic size={12} />Live mic {mic.held_for_recorded ? "paused for recording" : mic.mode}</> : <><MicOff size={12} />Live mic off</>}</Badge>
    </div>
    {error && <p className="al-small al-text-amber">The Mac's backend is not answering: {error}</p>}
    <input ref={picker} type="file" accept=".wav,audio/wav,audio/x-wav" multiple hidden onChange={e => { const add = Array.from(e.target.files ?? []); setFiles(prev => [...prev, ...add.filter(f => !prev.some(p => p.name === f.name))]); e.target.value = ""; }} />
    <Button onClick={() => picker.current?.click()}><FileAudio size={15} />Add WAV recordings</Button>
    <div className="al-recordings">{files.map(f => <div key={f.name} className="al-inline al-spread">
      <span className="al-small">{f.name} · {(f.size / 1024).toFixed(0)} KB</span>
      <span className="al-inline"><Button kind="primary" disabled={busy !== null || status?.busy === true} onClick={() => void send(f)}><Send size={14} />{busy === f.name ? "Listening…" : "Send to LOCKI"}</Button>
        <button className="al-text-button" aria-label={`remove ${f.name}`} onClick={() => setFiles(prev => prev.filter(p => p !== f))}><X size={13} /></button></span>
    </div>)}</div>
    {turns.map((t, n) => <div key={n} className="al-recorded-turn">
      <div className="al-inline al-spread"><Badge tone={t.ok ? "cyan" : "amber"}>{t.file}</Badge><span className="al-small">{t.stt_ms != null ? `heard in ${Math.round(t.stt_ms)} ms` : ""}{t.handled_ms != null ? ` · answered in ${Math.round(t.handled_ms)} ms` : ""}</span></div>
      {t.transcript != null && <p className="al-small">Recognized: <strong>“{t.transcript}”</strong>{t.intent ? ` · ${t.intent}` : ""}</p>}
      {t.nemotron?.investigation_id && <p className="al-small"><Badge tone={t.nemotron.evidence_badge === "LIVE" ? "cyan" : "amber"}>Nemotron {t.nemotron.evidence_badge ?? "?"}</Badge> {t.nemotron.status} · {t.nemotron.investigation_id}{t.nemotron.total_ms != null ? ` · ${Math.round(t.nemotron.total_ms)} ms` : ""}</p>}
      {t.reply && <p className="al-small">LOCKI: {t.reply}</p>}
      {t.error && <p className="al-small al-text-amber">{t.error}</p>}
      {!t.error && !t.ok && t.message && <p className="al-small al-muted">{t.message}</p>}
    </div>)}
  </Panel>;
}
