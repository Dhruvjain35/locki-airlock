import { useEffect, useRef, useState, type ReactNode } from "react";
import { demoApi, type DemoSnap, type Mode, type Stat, type Step } from "./demoApi";

// The AIRLOCK demo view. Everything on it is read from the backend's real state (GET /api/security/demo):
// the camera picture is the pipeline's own annotated frame, and every number is a measured sample.

const MODE_TONE: Record<Mode, { text: string; ring: string; chip: string; label: string; note: string }> = {
  NORMAL: { text: "text-ink-2", ring: "oklch(0.6 0.012 228)", chip: "chip-dim", label: "Normal", note: "Calm companion. Nobody unmatched at the desk." },
  WATCHFUL: { text: "text-sim", ring: "var(--color-sim)", chip: "chip-sim", label: "Watchful", note: "Someone not matched to the owner is near the desk. Attentive, no accusation." },
  DEFENSIVE: { text: "text-hit", ring: "var(--color-hit)", chip: "chip-hit", label: "Defensive", note: "Someone not matched to the owner is using protected equipment. Short, firm, P0 speech." },
  CRITICAL: { text: "text-danger", ring: "var(--color-danger)", chip: "chip-danger", label: "Critical", note: "A real unknown USB device, or the database's correlated incident." },
  OWNER_RETURNED: { text: "text-ok", ring: "var(--color-ok)", chip: "chip-live", label: "Owner returned", note: "The enrolled owner's face matched. Normal tone; still armed until an authenticated disarm." },
};

const STEPS: { key: Step; label: string }[] = [
  { key: "start", label: "Start demo" },
  { key: "enroll", label: "Enroll owner" },
  { key: "recognized", label: "Recognized" },
  { key: "zones", label: "Zones" },
  { key: "arm", label: "Arm" },
  { key: "monitor", label: "Live monitor" },
  { key: "incident", label: "Incident" },
  { key: "owner_return", label: "Owner returns" },
  { key: "disarm", label: "Disarm" },
];

const TIMINGS: { key: string; label: string }[] = [
  { key: "first_person_after_camera_ms", label: "First person after camera on" },
  { key: "presence_ms", label: "Arrival to person_in_zone" },
  { key: "enrollment_ms", label: "Enrollment duration" },
  { key: "owner_recognition_after_enrollment_ms", label: "First recognition after enrollment" },
  { key: "owner_identity_ms", label: "Owner identified (face to match)" },
  { key: "unrecognized_identity_ms", label: "Visitor identified (face to no match)" },
  { key: "mode_switch_ms", label: "Event to mode change" },
  { key: "event_to_speech_ms", label: "Event to speech start" },
  { key: "investigation_ms", label: "Investigation" },
  { key: "person_detect_ms", label: "Person detection, per frame" },
  { key: "face_pass_ms", label: "Face pass, per frame" },
  { key: "face_compare_ms", label: "Face comparison" },
  { key: "capture_to_event_ms", label: "Capture to event" },
];

const READY_LABEL: Record<string, string> = {
  camera: "Camera",
  models: "Vision models",
  zones: "Interaction zones",
  usb_monitor: "macOS USB monitor",
  spacetimedb: "SpacetimeDB (Mac)",
  camera_to_database: "Camera to SpacetimeDB",
  speaker: "Speaker",
  microphone: "Microphone",
  investigator: "Investigator model",
  screen_lock: "Screen lock",
};

function fmtMs(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 10000 ? `${(v / 1000).toFixed(1)} s` : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(v < 10 ? 1 : 0)} ms`;
}

function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour12: false });
}

function Card({ title, tag, children }: { title: string; tag?: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="flex items-center justify-between gap-3 px-4 pt-3 pb-2">
        <h2 className="font-display text-[13px] font-semibold tracking-[0.18em] text-ink-2 uppercase">{title}</h2>
        {tag}
      </div>
      <div className="px-4 pb-4">{children}</div>
    </section>
  );
}

function useDemo() {
  const [snap, setSnap] = useState<DemoSnap | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const d = await demoApi.get();
        if (!stop) {
          setSnap(d);
          setError(null);
        }
      } catch (e) {
        if (!stop) setError(String((e as Error).message ?? e));
      }
      if (!stop) timer = window.setTimeout(tick, 350);
    };
    tick();
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, []);
  return { snap, error, setSnap };
}

export function AirlockDemo({ onExit }: { onExit: () => void }) {
  const { snap, error, setSnap } = useDemo();
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [streamKey, setStreamKey] = useState(0);
  const [flash, setFlash] = useState(0);
  const lastMode = useRef<Mode | null>(null);
  const mode: Mode = snap?.visual_state.current_security_mode ?? "NORMAL";

  useEffect(() => {
    if (lastMode.current !== null && lastMode.current !== mode) setFlash((n) => n + 1);
    lastMode.current = mode;
  }, [mode]);

  useEffect(() => {
    if (snap?.camera_running) setStreamKey((k) => k + 0);
  }, [snap?.camera_running]);

  const act = async (label: string, fn: () => Promise<unknown>, restream = false) => {
    setBusy(label);
    setProblem(null);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "active" in (out as object)) setSnap(out as DemoSnap);
      if (restream) setStreamKey((k) => k + 1);
    } catch (e) {
      setProblem(String((e as Error).message ?? e));
    } finally {
      setBusy(null);
    }
  };

  const tone = MODE_TONE[mode];
  const vs = snap?.visual_state;
  const active = !!snap?.active;

  return (
    <main className="mx-auto max-w-[1760px] px-5 pt-5 pb-16">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="font-display text-[22px] font-bold tracking-[0.16em] text-ink">AIRLOCK DEMO</h1>
        {active ? (
          <span className={`chip ${snap?.mode === "live" ? "chip-live" : "chip-sim"}`} title={snap?.label ?? ""}>
            {snap?.mode === "live" ? "Live: real hardware and services" : "Rehearsal: fixtures labelled"}
          </span>
        ) : (
          <span className="chip chip-dim">No demo running</span>
        )}
        {snap?.id ? <span className="num text-[12px] text-ink-3">{snap.id}</span> : null}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {!active ? (
            <>
              <button className="btn btn-primary" disabled={!!busy} onClick={() => act("start", () => demoApi.start("live"), true)}>
                Start demo
              </button>
              <button className="btn btn-sim" disabled={!!busy} onClick={() => act("start", () => demoApi.start("rehearsal"), true)}>
                Start rehearsal
              </button>
            </>
          ) : (
            <>
              <button className="btn btn-primary" disabled={!!busy || snap?.enrolling} onClick={() => act("enroll", demoApi.enroll)}>
                {snap?.enrolled ? "Re-enroll owner" : "Enroll owner"}
              </button>
              {snap?.mode === "rehearsal" ? (
                <button className="btn btn-sim" disabled={!!busy} onClick={() => act("fixture", demoApi.fixtureUsb)} title="Rehearsal only: a pretend USB arrival, labelled FIXTURE, never spoken">
                  Fixture USB
                </button>
              ) : null}
              <button className="btn btn-ghost" disabled={!!busy} onClick={() => act("reset", demoApi.reset, true)}>
                Reset demo
              </button>
              <button className="btn btn-ghost" disabled={!!busy} onClick={() => act("end", demoApi.end)}>
                End demo
              </button>
            </>
          )}
          <button className="btn btn-ghost btn-sm" onClick={onExit}>
            Dashboard
          </button>
        </div>
      </div>
      {problem || error ? (
        <div className="pop-in mt-3 rounded-[4px] border border-danger/60 bg-danger/10 px-3 py-2 text-[13px] text-[oklch(0.85_0.1_27)]">{problem ?? error}</div>
      ) : null}

      <div className="mt-5 grid items-start gap-5" style={{ gridTemplateColumns: "minmax(0, 7fr) minmax(0, 5fr)" }}>
        <div className="flex min-w-0 flex-col gap-5">
          <section className="panel overflow-hidden">
            <div key={flash} className="relative" style={{ boxShadow: flash ? `0 0 0 3px ${tone.ring}` : undefined, transition: "box-shadow 900ms var(--ease-out-quart)" }}>
              <div className="aspect-[4/3] w-full bg-well">
                {snap?.camera_running ? (
                  <img key={streamKey} src={`/api/security/demo/stream.mjpg?k=${streamKey}`} alt="Live camera with the detections drawn on it" className="h-full w-full object-contain" />
                ) : (
                  <div className="flex h-full flex-col items-start justify-end gap-2 p-6">
                    <div className="font-display text-[18px] font-semibold tracking-[0.12em] text-ink-2">CAMERA OFF</div>
                    <p className="max-w-[52ch] text-[13px] text-ink-3">
                      Start a demo to turn on the camera. The picture stays on this Mac: frames are kept in memory, never saved or sent anywhere.
                    </p>
                  </div>
                )}
              </div>
              <div className="absolute top-3 right-3 left-3 flex items-start justify-between gap-3">
                <div key={`m${flash}`} className="pop-in rounded-[4px] border border-line bg-void/85 px-3 py-2">
                  <div className="label">Security mode</div>
                  <div className={`font-display text-[20px] font-bold tracking-[0.12em] ${tone.text}`}>{tone.label.toUpperCase()}</div>
                  {vs?.previous_mode && vs.previous_mode !== mode ? (
                    <div className="num mt-0.5 text-[11px] text-ink-3">
                      from {MODE_TONE[vs.previous_mode].label.toLowerCase()} · {vs.mode_reason.replace(/_/g, " ")}
                    </div>
                  ) : null}
                </div>
                <div className="rounded-[4px] border border-line bg-void/85 px-3 py-2 text-right">
                  <div className="label">{snap?.armed ? "Armed" : "Disarmed"}</div>
                  <div className="num text-[12px] text-ink-2">
                    {typeof snap?.timings.fps === "number" ? `${snap.timings.fps} fps` : "—"} · camera {vs?.camera_health ?? "off"}
                  </div>
                </div>
              </div>
            </div>
            <div className="px-4 py-3 text-[13px] text-ink-3">{tone.note}</div>
          </section>

          <section className="panel px-4 py-3">
            <div className="label mb-2">Demo steps</div>
            <ol className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(118px, 1fr))" }}>
              {STEPS.map((s, i) => {
                const st = snap?.steps?.[s.key] ?? "pending";
                const cls = st === "done" ? "border-[oklch(0.82_0.12_165/0.5)] text-ok" : st === "default" ? "border-[oklch(0.83_0.14_80/0.45)] text-sim" : "border-line-soft text-ink-3";
                return (
                  <li key={s.key} className={`rounded-[4px] border px-2.5 py-2 ${cls}`} title={st === "default" ? "Using the default desk zone only" : st}>
                    <div className="num text-[10.5px] opacity-70">{String(i + 1).padStart(2, "0")}</div>
                    <div className="text-[12.5px] font-semibold">{s.label}</div>
                  </li>
                );
              })}
            </ol>
          </section>

          <Card title="Timeline" tag={<span className="chip chip-dim">real events{snap?.fixtures?.length ? " + fixtures" : ""}</span>}>
            {snap?.timeline?.length ? (
              <ul className="flex max-h-[340px] flex-col gap-1 overflow-y-auto">
                {[...snap.timeline].reverse().map((e, i) => {
                  const fixture = e.kind === "fixture" || e.text.startsWith("FIXTURE");
                  return (
                    <li key={`${e.at}-${i}`} className="flex items-baseline gap-3 text-[13px]">
                      <span className="num shrink-0 text-[11.5px] text-ink-4">{clock(e.at)}</span>
                      <span className={`chip shrink-0 ${fixture ? "chip-sim" : e.kind === "mode" ? "chip-hit" : e.kind === "incident" ? "chip-danger" : e.kind === "usb" ? "chip-live" : "chip-dim"}`}>{fixture ? "fixture" : e.kind}</span>
                      <span className="min-w-0 text-ink-2">
                        {e.text.replace(/_/g, " ")}
                        {e.zone ? <span className="text-ink-4"> · {e.zone.replace(/_/g, " ")}</span> : null}
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-[13px] text-ink-3">Nothing yet. Camera, USB, mode, incident and enrollment events appear here as they happen.</p>
            )}
          </Card>
        </div>

        <div className="flex min-w-0 flex-col gap-5">
          <Card title="Owner face" tag={<span className={`chip ${snap?.enrolled ? "chip-live" : "chip-dim"}`}>{snap?.enrolled ? `enrolled · ${snap.enrolled_samples} samples` : "not enrolled"}</span>}>
            <Identity snap={snap} />
          </Card>

          {active && (snap?.enrolling || (snap?.enrollment && !snap.enrolled)) ? (
            <Card title="Enrollment" tag={<span className="chip chip-live">camera</span>}>
              <Enrollment snap={snap} />
            </Card>
          ) : null}

          {active ? (
            <Card title="Recognition trials" tag={<span className="chip chip-dim">consenting volunteers</span>}>
              <Trials snap={snap} busy={!!busy} onTrial={(label, who) => act("trial", () => demoApi.trial(label, who))} />
            </Card>
          ) : null}

          <Card title="Arm and disarm" tag={<span className="chip chip-dim">{snap?.arming === "database" ? "SpacetimeDB arms" : "owner code"}</span>}>
            {snap?.arming === "database" ? (
              <p className="text-[13px] leading-relaxed text-ink-3">
                SpacetimeDB owns arming for this workstation. Arm and disarm from its dashboard (or the owner's <span className="num text-ink-2">spacetime call ... arm_session</span>); this Mac follows within a
                second. Saying "secure my workstation" asks for that confirmation.
              </p>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="password"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="Owner code"
                  className="h-9 w-40 rounded-[4px] border border-line bg-well px-3 text-[13px] text-ink outline-none focus:border-live"
                  aria-label="Owner code"
                />
                <button className="btn" disabled={!!busy || snap?.armed || code.length < 4} onClick={() => act("arm", () => demoApi.arm(code))}>
                  Arm
                </button>
                <button className="btn btn-ghost" disabled={!!busy || !snap?.armed || code.length < 4} onClick={() => act("disarm", () => demoApi.disarm(code))}>
                  Disarm
                </button>
                <span className="text-[12px] text-ink-4">A face never disarms. Screen lock stays a dry run.</span>
              </div>
            )}
          </Card>

          <Card title="Measured" tag={<span className="chip chip-dim">p50 · p95 · n</span>}>
            <table className="w-full text-[12.5px]">
              <tbody>
                {TIMINGS.map((t) => {
                  const v = snap?.timings?.[t.key] as Stat | undefined;
                  return (
                    <tr key={t.key} className="border-b border-line-soft/60 last:border-0">
                      <td className="py-1.5 pr-3 text-ink-2">{t.label}</td>
                      <td className={`num py-1.5 text-right ${v?.p50 == null ? "text-ink-4" : "text-ink"}`}>{fmtMs(v?.p50)}</td>
                      <td className="num py-1.5 text-right text-ink-3">{fmtMs(v?.p95)}</td>
                      <td className="num py-1.5 pl-2 text-right text-ink-4">{v?.n ?? (v?.p50 != null ? "·" : "")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <p className="mt-2 text-[11.5px] text-ink-4">Measured on this M4 from real events. — means not measured yet. Fixtures are never measured. Jetson: not run.</p>
          </Card>

          <Card title="Readiness">
            <ul className="flex flex-col gap-1.5">
              {Object.entries(snap?.readiness ?? {}).map(([k, r]) => (
                <li key={k} className="flex items-baseline gap-2.5 text-[13px]">
                  <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${r.ok ? "bg-ok" : "bg-ink-4"}`} />
                  <span className="w-[160px] shrink-0 text-ink-2">{READY_LABEL[k] ?? k}</span>
                  <span className="min-w-0 truncate text-ink-3" title={r.detail}>
                    {r.detail}
                    {k === "investigator" && r.ok ? (r.nemotron ? " · Nemotron" : " · not Nemotron") : ""}
                  </span>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </main>
  );
}

function Trials({ snap, busy, onTrial }: { snap: DemoSnap; busy: boolean; onTrial: (label: "genuine" | "impostor", who: string) => void }) {
  const [who, setWho] = useState("v1");
  const s = snap.trial_summary;
  const running = snap.trials?.some((t) => t.state === "running");
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[12px] leading-relaxed text-ink-4">
        Only with the person's consent. Press a button, then the person steps into view: the first face that appears decides the trial. Kept: outcome and timings, never a face.
        Genuine = the person enrolled in this demo; impostor = anyone else.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input value={who} onChange={(e) => setWho(e.target.value)} maxLength={16} aria-label="Participant label" placeholder="v1"
          className="h-9 w-24 rounded-[4px] border border-line bg-well px-3 text-[13px] text-ink outline-none focus:border-live" />
        <button className="btn btn-sm" disabled={busy || running || !snap.enrolled} onClick={() => onTrial("genuine", who)}>Genuine trial</button>
        <button className="btn btn-sm" disabled={busy || running || !snap.enrolled} onClick={() => onTrial("impostor", who)}>Impostor trial</button>
        {running ? <span className="chip chip-live blink">waiting for a face</span> : null}
      </div>
      {s && s.trials ? (
        <div className="grid grid-cols-2 gap-3 text-[12.5px]">
          <div>
            <div className="label">Genuine ({s.genuine.n})</div>
            <div className="num mt-1 text-ink">matched {s.genuine.matched} · <span className={s.genuine.false_unrecognized ? "text-hit" : ""}>false unrecognized {s.genuine.false_unrecognized}</span> · uncertain {s.genuine.uncertain} · missed {s.genuine.missed}</div>
          </div>
          <div>
            <div className="label">Impostor ({s.impostor.n})</div>
            <div className="num mt-1 text-ink"><span className={s.impostor.false_owner_match ? "text-danger" : ""}>false owner match {s.impostor.false_owner_match}</span> · unrecognized {s.impostor.unrecognized} · uncertain {s.impostor.uncertain} · missed {s.impostor.missed}</div>
          </div>
          <div>
            <div className="label">Owner identified</div>
            <div className="num mt-1 text-ink">{fmtMs(s.identity_ms.owner?.p50)} · {fmtMs(s.identity_ms.owner?.p95)} · max {fmtMs(s.identity_ms.owner?.max)} (n {s.identity_ms.owner?.n ?? 0})</div>
          </div>
          <div>
            <div className="label">Visitor identified</div>
            <div className="num mt-1 text-ink">{fmtMs(s.identity_ms.unrecognized?.p50)} · {fmtMs(s.identity_ms.unrecognized?.p95)} · max {fmtMs(s.identity_ms.unrecognized?.max)} (n {s.identity_ms.unrecognized?.n ?? 0})</div>
          </div>
          <div className="col-span-2 text-[11.5px] text-ink-4">Arrival to identity: {fmtMs(s.arrival_to_identity_ms?.p50)} p50 · {fmtMs(s.arrival_to_identity_ms?.p95)} p95 (n {s.arrival_to_identity_ms?.n ?? 0}). {s.note}.</div>
        </div>
      ) : null}
      {snap.trials?.length ? (
        <ul className="flex max-h-[160px] flex-col gap-1 overflow-y-auto text-[12px]">
          {[...snap.trials].reverse().map((t) => (
            <li key={t.n} className="flex items-baseline gap-2">
              <span className="num w-6 text-ink-4">{t.n}</span>
              <span className="w-16 text-ink-3">{t.label}</span>
              <span className="w-12 text-ink-3">{t.participant}</span>
              <span className={`w-28 ${t.state === "running" ? "text-live" : t.correct ? "text-ok" : t.outcome === "owner" ? "text-danger" : "text-sim"}`}>{t.state === "running" ? "running" : t.outcome}</span>
              <span className="num text-ink-3">{t.identity_ms != null ? fmtMs(t.identity_ms) : ""}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function Identity({ snap }: { snap: DemoSnap | null }) {
  const vs = snap?.visual_state;
  const state = vs?.face_identity_state ?? null;
  const label = state === "owner" ? "Matches the enrolled owner" : state === "unrecognized" ? "Not recognized as the owner" : state === "uncertain" ? "Uncertain" : "Nobody in view";
  const cls = state === "owner" ? "text-ok" : state === "unrecognized" ? "text-hit" : state === "uncertain" ? "text-sim" : "text-ink-3";
  const th = snap?.thresholds;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline gap-3">
        <div className={`font-display text-[19px] font-semibold tracking-[0.08em] ${cls}`}>{label}</div>
        {vs?.person_track_id != null ? <span className="num text-[12px] text-ink-3">track #{vs.person_track_id}</span> : null}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className="label">Agreement</div>
          <div className="num mt-1 text-[13px] text-ink">{vs?.identity_confidence != null ? vs.identity_confidence.toFixed(2) : "—"}</div>
        </div>
        <div>
          <div className="label">At the desk</div>
          <div className="num mt-1 text-[13px] text-ink">{vs?.at_desk ? "yes" : vs?.person_present ? "in view" : "no"}</div>
        </div>
        <div>
          <div className="label">Touching</div>
          <div className="num mt-1 text-[13px] text-ink">
            {[vs?.keyboard_interaction && "keyboard", vs?.laptop_interaction && "laptop", vs?.usb_zone_interaction && "USB ports", vs?.protected_device_interaction && "device"].filter(Boolean).join(", ") || "—"}
          </div>
        </div>
      </div>
      {vs?.identity_detail ? <div className="text-[12.5px] text-ink-3">{vs.identity_detail}</div> : null}
      <p className="text-[12px] leading-relaxed text-ink-4">
        {snap?.enrolled
          ? `The score on the picture is the mean of the 3 best cosine similarities between this face and this demo's enrolled samples: ${th?.match ?? 0.5} or more matches, ${th?.reject ?? 0.3} or less does not, anything between is uncertain${th?.calibrated ? "" : " (default thresholds, not calibrated on this room)"}. Agreement is the share of recent frames that agreed, not a probability of identity. A match is context only: it never disarms.`
          : "Nobody is enrolled in this demo, so a face can only be uncertain. Nothing is recognized until you enroll."}
      </p>
    </div>
  );
}

function Enrollment({ snap }: { snap: DemoSnap }) {
  const e = snap.enrollment;
  if (!e) return null;
  const pct = Math.round((100 * e.accepted) / Math.max(1, e.needed));
  const enrollMs = (snap.timings.enrollment_ms as Stat | undefined)?.p50;
  const need: Record<string, number> = { center: 6, left: 3, right: 3 };
  return (
    <div className="flex flex-col gap-3">
      <div className="font-display text-[18px] font-semibold tracking-[0.06em] text-live">{e.complete ? "Done" : e.hint}</div>
      <div className="h-2.5 w-full overflow-hidden rounded-[2px] border border-line-soft bg-well">
        <div className="h-full bg-live" style={{ width: `${pct}%`, transition: "width 240ms var(--ease-out-quart)" }} />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {Object.entries(need).map(([pose, n]) => (
          <span key={pose} className={`chip ${(e.poses?.[pose] ?? 0) >= n ? "chip-live" : "chip-dim"}`}>
            {pose} {e.poses?.[pose] ?? 0}/{n}
          </span>
        ))}
        <span className="num ml-auto text-[12px] text-ink-3">
          {e.accepted}/{e.needed}
          {enrollMs ? ` · ${fmtMs(enrollMs)}` : ""}
        </span>
      </div>
      {Object.keys(e.rejected ?? {}).length ? (
        <div className="text-[12px] text-ink-4">
          Not used: {Object.entries(e.rejected).map(([k, n]) => `${k.replace(/_/g, " ")} ${n}`).join(", ")}
        </div>
      ) : null}
      {e.error ? <div className="text-[12px] text-danger">{e.error}</div> : null}
      <p className="text-[12px] text-ink-4">Embeddings only (128 numbers per sample), kept in this demo's own folder on this Mac, deleted by Reset or End. Never sent to SpacetimeDB.</p>
    </div>
  );
}
