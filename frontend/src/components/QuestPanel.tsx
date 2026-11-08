import { useState, type FormEvent } from "react";
import { Check, CircleDashed, FlaskConical, Lightbulb, Play, RotateCcw, Trash2, Wand2, X } from "lucide-react";
import { api } from "../services/api";
import type { QuestCheckpoint, QuestEdit, QuestMission, QuestSnap, Snapshot } from "../types/api";
import { Locki, MOOD_LABEL } from "./Locki";
import { useToasts } from "./Toasts";
import { SectionHeader } from "./ui";

/** Code Quest: every number here comes from a real test run on the backend. There is no button that
 *  finishes anything: buttons run the tests, ask for a hint, or decide on a suggested edit. */

function BossBar({ hp, max }: { hp: number; max: number }) {
  const cells = Math.max(max, 1);
  return (
    <div role="meter" aria-label="Boss health" aria-valuemin={0} aria-valuemax={max} aria-valuenow={hp}>
      <div className="flex items-baseline justify-between">
        <span className="label">Boss health</span>
        <span className="num text-[13px] text-ink-2">
          <span className={hp === 0 ? "text-ok" : "text-hit"}>{hp}</span> / {max}
        </span>
      </div>
      <div className="mt-1.5 flex gap-1">
        {Array.from({ length: cells }, (_, i) => (
          <span key={i} className="h-3 min-w-0 flex-1 rounded-[3px] transition-colors duration-500" style={{ background: i < hp ? "var(--color-hit)" : "var(--color-line-soft)" }} />
        ))}
      </div>
    </div>
  );
}

function CheckpointRow({ c }: { c: QuestCheckpoint }) {
  const done = c.status === "done";
  const broken = c.status === "regressed";
  return (
    <li className="px-4 py-2">
      <div className="flex items-center gap-2.5">
        {done ? <Check size={15} className="shrink-0 text-ok" /> : <CircleDashed size={15} className={`shrink-0 ${c.current ? "text-live" : "text-ink-4"}`} />}
        <span className={`min-w-0 flex-1 text-[14px] ${done ? "text-ink-3" : c.current ? "text-ink" : "text-ink-2"}`}>{c.title}</span>
        {broken ? <span className="chip chip-hit">broke again</span> : null}
        <span className="num text-[11px] text-ink-3">
          {c.tests.filter((t) => t.outcome === "passed").length}/{c.tests.length}
        </span>
      </div>
      {c.current ? (
        <ul className="mt-1.5 ml-[25px] space-y-1">
          {c.tests.map((t) => (
            <li key={t.id} className="text-[12px] leading-snug">
              <span className={t.outcome === "passed" ? "text-ok" : "text-ink-2"}>{t.name}</span>
              {t.summary ? <div className="num mt-0.5 break-words text-[11px] text-ink-3">{t.summary}</div> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function EditCard({ e }: { e: QuestEdit }) {
  const { run } = useToasts();
  return (
    <div className="mx-4 mb-3 rounded-md border border-line p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] text-ink">Suggested change to <span className="num">{e.path}</span></span>
        <span className="chip chip-sim">waiting for you</span>
      </div>
      {e.why ? <p className="mt-1 text-[12px] text-ink-3">{e.why}</p> : null}
      <pre className="num mt-2 overflow-x-auto rounded bg-well p-2 text-[11px] leading-relaxed text-hit">{e.old.split("\n").map((l) => `- ${l}`).join("\n")}</pre>
      <pre className="num mt-1 overflow-x-auto rounded bg-well p-2 text-[11px] leading-relaxed text-ok">{e.new.split("\n").map((l) => `+ ${l}`).join("\n")}</pre>
      <div className="mt-2 flex gap-2">
        <button className="btn btn-sm btn-primary" onClick={() => run(api.missionDecideEdit(e.id, "approve"))}>Apply it</button>
        <button className="btn btn-sm btn-ghost" onClick={() => run(api.missionDecideEdit(e.id, "reject"))}>Drop it</button>
      </div>
      <p className="mt-2 text-[11px] text-ink-4">Applying only changes the file. The tests decide whether it worked.</p>
    </div>
  );
}

function RunLine({ q }: { q: QuestSnap }) {
  const r = q.last_run;
  if (!r) return null;
  const passed = r.counts.passed ?? 0;
  const failed = (r.counts.failed ?? 0) + (r.counts.error ?? 0);
  return (
    <div className="px-4 pb-2.5">
      <div className="num flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-3">
        <FlaskConical size={12} className="shrink-0" />
        <span className="text-ink-2">pytest</span>
        {r.status === "ok" ? (
          <>
            <span className="text-ok">{passed} passed</span>
            <span className={failed ? "text-hit" : ""}>{failed} failed</span>
          </>
        ) : (
          <span className="text-danger">{r.status === "broken" ? "code does not load" : r.status}</span>
        )}
        <span>{r.duration_s.toFixed(1)} s</span>
        <span>{r.trigger}</span>
      </div>
      {r.tail ? <pre className="num mt-1.5 max-h-28 overflow-auto rounded bg-well p-2 text-[11px] text-ink-3">{r.tail}</pre> : null}
    </div>
  );
}

function ActiveMission({ q, m }: { q: QuestSnap; m: QuestMission }) {
  const { run } = useToasts();
  const [dropping, setDropping] = useState(false);
  const testing = q.busy === "testing";
  return (
    <>
      <div className="px-4 pb-3">
        <BossBar hp={m.hp} max={m.hp_max} />
        <div className="mt-3">
          <div className="label">Next objective</div>
          <p className="mt-1 font-display text-[18px] leading-snug font-semibold text-ink">{m.next_objective}</p>
        </div>
      </div>
      {m.flagged_tests.length ? (
        <div className="mx-4 mb-3 rounded-md border border-sim/50 bg-sim-wash p-3 text-[13px] text-ink-2">
          <p>You changed {m.flagged_tests.join(", ")}. Runs do not count until you accept it, so the boss can't be beaten by editing the exam.</p>
          <button className="btn btn-sm btn-sim mt-2" onClick={() => run(api.missionAcceptTests())}>Accept the test change</button>
        </div>
      ) : null}
      {q.help ? (
        <div className="mx-4 mb-3 flex gap-2.5 rounded-md bg-live-wash p-3 text-[13px] leading-snug text-ink">
          <Lightbulb size={15} className="mt-0.5 shrink-0 text-live" />
          <div>
            <p>{q.help.text}</p>
            <p className="num mt-1 text-[11px] text-ink-3">{q.help.source === "rule" ? "from the test output" : `from ${q.help.source}, on this Mac`}</p>
          </div>
        </div>
      ) : null}
      {q.edits.map((e) => <EditCard key={e.id} e={e} />)}
      <div className="rule" />
      <ul className="py-1">
        {m.checkpoints.map((c) => <CheckpointRow key={c.id} c={c} />)}
        {m.checkpoints.length === 0 ? <li className="px-4 py-2 text-[13px] text-ink-3">No checkpoints yet. A failing test creates the first one.</li> : null}
      </ul>
      <div className="rule" />
      <div className="flex flex-wrap items-center gap-2 px-4 py-2.5">
        <button className="btn btn-sm" disabled={testing} onClick={() => run(api.missionRun())}>
          <Play size={13} /> {testing ? "Running…" : "Run tests"}
        </button>
        <button className="btn btn-sm" disabled={!!q.busy} onClick={() => run(api.missionHelp())}>
          <Lightbulb size={13} /> Why is it failing?
        </button>
        <button className="btn btn-sm" disabled={!!q.busy} onClick={() => run(api.missionProposeEdit())}>
          <Wand2 size={13} /> Suggest a fix
        </button>
        <span className="flex-1" />
        {dropping ? (
          <>
            <button className="btn btn-sm btn-hit" onClick={() => { setDropping(false); run(api.missionAbandon()); }}>Drop mission</button>
            <button className="btn btn-sm btn-ghost" aria-label="Keep the mission" onClick={() => setDropping(false)}><X size={13} /></button>
          </>
        ) : (
          <button className="btn btn-sm btn-ghost" onClick={() => setDropping(true)} aria-label="Drop the mission"><Trash2 size={13} /></button>
        )}
      </div>
      <RunLine q={q} />
      <div className="num px-4 pb-3 text-[11px] leading-relaxed text-ink-4">
        <div className="break-all">{m.project}</div>
        <div>plan: {m.plan_source === "rule" ? "fixed grouping" : m.plan_source} · {m.guard_tests} tests must keep passing{m.removed_tests ? ` · ${m.removed_tests} removed by you` : ""}{q.watching ? " · watching for saves" : ""}</div>
      </div>
    </>
  );
}

function StartForm({ q }: { q: QuestSnap }) {
  const { run } = useToasts();
  const [objective, setObjective] = useState("");
  const [project, setProject] = useState<string>("");
  const [folder, setFolder] = useState("");
  const [showApprove, setShowApprove] = useState(false);
  const planning = q.busy === "planning";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (objective.trim().length < 3) return;
    run(api.missionStart({ objective: objective.trim(), project: project || undefined }));
  };
  return (
    <form onSubmit={submit} className="px-4 pb-3">
      <label className="label" htmlFor="quest-objective">What are you building?</label>
      <input id="quest-objective" className="field mt-1.5 !h-10 !text-[15px]" placeholder="Finish the cart checkout" value={objective} maxLength={300} onChange={(e) => setObjective(e.target.value)} />
      <p className="mt-1.5 text-[12px] text-ink-3">Or say it: “I'm building the checkout for my cart.”</p>
      <label className="label mt-3 block" htmlFor="quest-project">Project</label>
      <select id="quest-project" className="field mt-1.5" value={project} onChange={(e) => setProject(e.target.value)}>
        <option value="">{q.projects.length ? "Last approved folder" : "Demo project (broken shopping cart)"}</option>
        {q.projects.map((p) => <option key={p} value={p}>{p}</option>)}
        {q.projects.length ? <option value={q.demo_project}>Demo project (broken shopping cart)</option> : null}
      </select>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="submit" className="btn btn-primary" disabled={planning || objective.trim().length < 3}>{planning ? "Reading your tests…" : "Start mission"}</button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => run(api.missionDemoProject(true))} title="Put the demo project's bugs back">
          <RotateCcw size={13} /> Reset demo
        </button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setShowApprove((v) => !v)}>Approve a folder</button>
      </div>
      {showApprove ? (
        <div className="mt-3 rounded-md border border-line p-3">
          <p className="text-[12px] leading-snug text-ink-2">Approving a folder lets LOCKED IN run that project's tests on this Mac every time you save, and read its files (never .env or keys). Tests run with no network and can only write inside the project.</p>
          <input className="field mt-2" placeholder="~/code/my-project" value={folder} maxLength={400} onChange={(e) => setFolder(e.target.value)} aria-label="Project folder" />
          <button type="button" className="btn btn-sm mt-2" disabled={!folder.trim()} onClick={() => run(api.missionApproveProject(folder.trim()))}>Approve this folder</button>
        </div>
      ) : null}
      {q.demo_ready ? <p className="num mt-3 text-[11px] break-all text-ink-4">Demo project to open in your editor: {q.demo_project}</p> : null}
      {q.error ? <p className="mt-2 text-[12px] text-danger">{q.error}</p> : null}
    </form>
  );
}

export function QuestPanel({ snap }: { snap: Snapshot }) {
  const q = snap.quest;
  const m = q.mission;
  const c = q.character;
  const active = m?.status === "active";
  return (
    <div className="panel">
      <SectionHeader title="Code Quest" tag={active ? <span className="chip chip-live">tests decide</span> : undefined}>
        <span className="num text-[12px] text-ink-3" title="XP from real test results, all missions">{q.xp_total} XP</span>
      </SectionHeader>
      <div className="flex items-center gap-3 px-4 pb-3">
        <Locki mood={c.mood} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="font-display text-[15px] font-bold tracking-[0.12em] text-ink">{c.name}</span>
            <span className="text-[12px] text-ink-3">{MOOD_LABEL[c.mood]}</span>
          </div>
          {m ? <p className="mt-0.5 truncate text-[14px] text-ink-2" title={m.objective}>{m.title}</p> : null}
          {c.line ? <p className="mt-1 line-clamp-2 text-[13px] leading-snug text-ink-3">“{c.line}”</p> : null}
        </div>
      </div>
      {active ? <ActiveMission q={q} m={m} /> : null}
      {m?.status === "won" ? (
        <div className="px-4 pb-3">
          <p className="font-display text-[20px] font-bold text-ok">Boss beaten</p>
          <p className="mt-1 text-[13px] text-ink-2">All {m.checkpoints.reduce((n, x) => n + x.tests.length, 0)} tests pass, {m.guard_tests} more stayed green, in {m.runs} test runs. {m.xp} XP.</p>
        </div>
      ) : null}
      {!active ? <StartForm q={q} /> : null}
    </div>
  );
}
