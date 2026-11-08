import { useEffect, useState, type ReactNode } from "react";
import { useLockedIn } from "./hooks/useLockedIn";
import { TopBar } from "./components/TopBar";
import { FocusTimer } from "./components/FocusTimer";
import { StateRail } from "./components/StateRail";
import { DecisionCard } from "./components/DecisionCard";
import { CameraPanel } from "./components/CameraPanel";
import { LoopTrace } from "./components/LoopTrace";
import { RobotPanel } from "./components/RobotPanel";
import { Timeline } from "./components/Timeline";
import { VoicePanel } from "./components/VoicePanel";
import { CompanionPanel } from "./components/CompanionPanel";
import { MetricsPanel } from "./components/MetricsPanel";
import { QuestPanel } from "./components/QuestPanel";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { DebugDrawer } from "./components/DebugDrawer";
import { VoiceStudio } from "./components/VoiceStudio";
import { ToastProvider } from "./components/Toasts";
import { AirlockDemo } from "./demo/AirlockDemo";
import AirlockApp from "./airlock/AirlockApp";

type AreaName = "quest" | "timer" | "camera" | "mind" | "trace" | "robot" | "timeline" | "voice" | "metrics";

function useMedia(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setMatch(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return match;
}

/** Independent flex columns (no shared grid rows), so a tall panel in one column never
 *  opens gaps in another. Priority order: timer > camera/robot > companion > loop > timeline > rest. */
function Columns({ areas }: { areas: Record<AreaName, ReactNode> }) {
  const wide = useMedia("(min-width: 1440px)");
  const mid = useMedia("(min-width: 1100px)");
  const layout: { cols: AreaName[][]; template: string } = wide
    ? { cols: [["quest", "timer", "metrics"], ["camera", "mind", "trace"], ["robot", "voice", "timeline"]], template: "minmax(0,33fr) minmax(0,40fr) minmax(0,27fr)" }
    : mid
      ? { cols: [["quest", "timer", "robot", "voice"], ["camera", "mind", "trace", "timeline", "metrics"]], template: "minmax(0,5fr) minmax(0,7fr)" }
      : { cols: [["quest", "timer", "camera", "mind", "trace", "robot", "timeline", "voice", "metrics"]], template: "minmax(0,1fr)" };
  return (
    <main className="mx-auto grid max-w-[1760px] items-start gap-5 px-5 pt-5 pb-16" style={{ gridTemplateColumns: layout.template }}>
      {layout.cols.map((col, i) => (
        <div key={i} className="flex min-w-0 flex-col gap-5">
          {col.map((a) => areas[a])}
        </div>
      ))}
    </main>
  );
}

function Connecting({ link }: { link: string }) {
  return (
    <div className="mx-auto max-w-[62ch] px-6 py-24">
      <div className="font-display text-[26px] font-bold tracking-[0.14em] text-ink-2">{link === "closed" ? "BACKEND OFFLINE" : "CONNECTING…"}</div>
      <p className="mt-3 text-[14px] leading-relaxed text-ink-3">
        The dashboard talks to the LOCKED IN backend on port 8077. Start it with <span className="num text-ink-2">make backend</span> (or{" "}
        <span className="num text-ink-2">cd backend && uv run uvicorn app.main:app --port 8077</span>). This page reconnects on its own.
      </p>
    </div>
  );
}

function useHashView(): ["dashboard" | "demo", (v: "dashboard" | "demo") => void] {
  const read = () => (location.hash === "#demo" ? "demo" : "dashboard");
  const [view, setView] = useState<"dashboard" | "demo">(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [view, (v) => { location.hash = v === "demo" ? "demo" : ""; setView(v); }];
}

function LegacyApp() {
  const live = useLockedIn();
  const [view, setView] = useHashView();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [debugOpen, setDebugOpen] = useState(false);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const snap = live.snapshot;

  return (
    <ToastProvider>
      <div className="min-h-dvh">
        <TopBar snap={snap} link={live.link} wsLatencyMs={live.wsLatencyMs} onSettings={() => setSettingsOpen(true)} onDebug={() => setDebugOpen((v) => !v)} onVoice={() => setVoiceOpen(true)} onDemo={() => setView(view === "demo" ? "dashboard" : "demo")} demoOpen={view === "demo"} />
        {view === "demo" ? (
          <AirlockDemo onExit={() => setView("dashboard")} />
        ) : !snap ? (
          <Connecting link={live.link} />
        ) : (
          <Columns
            areas={{
              quest: (
                <section key="quest" className="rise" style={{ ["--i" as any]: 0 }}>
                  <QuestPanel snap={snap} />
                </section>
              ),
              timer: (
                <section key="timer" className="panel rise flex flex-col" style={{ ["--i" as any]: 0 }}>
                  <FocusTimer snap={snap} snapshotAt={live.snapshotAt} events={live.events} />
                  <div className="rule" />
                  <StateRail snap={snap} />
                  <div className="rule" />
                  <DecisionCard snap={snap} events={live.events} />
                </section>
              ),
              camera: (
                <section key="camera" className="rise" style={{ ["--i" as any]: 1 }}>
                  <CameraPanel snap={snap} events={live.events} lastLiveEvent={live.lastLiveEvent} />
                </section>
              ),
              mind: (
                <section key="mind" className="rise" style={{ ["--i" as any]: 1 }}>
                  <CompanionPanel snap={snap} />
                </section>
              ),
              trace: (
                <section key="trace" className="rise" style={{ ["--i" as any]: 2 }}>
                  <LoopTrace snap={snap} events={live.events} />
                </section>
              ),
              robot: (
                <section key="robot" className="rise" style={{ ["--i" as any]: 2 }}>
                  <RobotPanel snap={snap} />
                </section>
              ),
              timeline: (
                <section key="timeline" className="rise min-h-0" style={{ ["--i" as any]: 3 }}>
                  <Timeline events={live.events} />
                </section>
              ),
              voice: (
                <section key="voice" className="rise min-h-0" style={{ ["--i" as any]: 4 }}>
                  <VoicePanel snap={snap} events={live.events} audioDb={live.audioDb} send={live.send} />
                </section>
              ),
              metrics: (
                <section key="metrics" className="rise" style={{ ["--i" as any]: 5 }}>
                  <MetricsPanel snap={snap} events={live.events} />
                </section>
              ),
            }}
          />
        )}
        {snap ? (
          <>
            <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} snap={snap} onOpenVoice={() => { setSettingsOpen(false); setVoiceOpen(true); }} />
            <VoiceStudio open={voiceOpen} onClose={() => setVoiceOpen(false)} snap={snap} />
            <DebugDrawer open={debugOpen} onClose={() => setDebugOpen(false)} snap={snap} events={live.events} />
          </>
        ) : null}
      </div>
    </ToastProvider>
  );
}

/** The LOCKI AIRLOCK command center by default. #face-setup (or the older #demo link) opens the Mac's face-enrollment and labelled
 *  rehearsal workflow on its own page, without the old focus console around it. ?product=legacy keeps the old focus console reachable. */
function readRoute(): "airlock" | "setup" | "legacy" {
  if (new URLSearchParams(window.location.search).get("product") === "legacy") return "legacy";
  return location.hash === "#face-setup" || location.hash === "#demo" ? "setup" : "airlock";
}

function SetupPage() {
  return <div className="min-h-dvh" style={{ background: "#070d11" }}>
    <div className="mx-auto max-w-[1500px] px-5 pt-4 text-[12px] text-ink-3">LOCKI AIRLOCK · Face setup and labelled rehearsal. Live mode uses this Mac's real camera; rehearsal fixtures are labelled and never spoken.</div>
    <AirlockDemo onExit={() => { history.replaceState(null, "", location.pathname + location.search); window.dispatchEvent(new HashChangeEvent("hashchange")); }} />
  </div>;
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const on = () => setRoute(readRoute());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route === "legacy" ? <LegacyApp /> : route === "setup" ? <SetupPage /> : <AirlockApp />;
}
