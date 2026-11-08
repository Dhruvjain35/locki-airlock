import { useRef, useState, type CSSProperties } from "react";
import {
  Activity,
  ArrowDown,
  ArrowUpRight,
  AudioLines,
  Camera,
  Check,
  ChevronRight,
  CircleHelp,
  Cpu,
  Expand,
  Eye,
  Github,
  Laptop,
  LayoutDashboard,
  LockKeyhole,
  Monitor,
  Moon,
  PanelLeft,
  ShieldCheck,
  Sun,
  Usb,
  X,
} from "lucide-react";
import "./landing.css";

type Screen = "overview" | "activity" | "review";
type Device = "laptop" | "desktop";
type Angle = "front" | "left" | "right";

const screenNames: Record<Screen, string> = {
  overview: "Overview",
  activity: "Activity",
  review: "Review",
};
const angles: Record<Angle, number> = { front: 0, left: -22, right: 22 };
const sampleEvents = [
  {
    time: "10:42:07",
    title: "New USB device connected",
    detail: "Outside the session baseline",
    kind: "Review needed",
    icon: Usb,
  },
  {
    time: "10:42:03",
    title: "Person observed near desk",
    detail: "Camera presence observation",
    kind: "Observation",
    icon: Eye,
  },
  {
    time: "10:30:00",
    title: "Monitoring session armed",
    detail: "Owner policy recorded",
    kind: "Session",
    icon: ShieldCheck,
  },
];

function AirlockMark({ small = false }: { small?: boolean }) {
  return (
    <span
      className={`landing-mark${small ? " landing-mark-small" : ""}`}
      aria-hidden="true"
    >
      <span />
      <span />
      <span />
    </span>
  );
}

function PreviewScreen({
  screen,
  onScreenChange,
}: {
  screen: Screen;
  onScreenChange: (screen: Screen) => void;
}) {
  return (
    <div className="landing-preview-app">
      <aside
        className="landing-preview-sidebar"
        aria-label="Preview navigation"
      >
        <div className="landing-preview-brand">
          <AirlockMark small />
          <span>Airlock</span>
        </div>
        <div className="landing-preview-workspace">
          <span className="landing-workspace-avatar">W</span>
          <div>
            Workshop desk<small>Sample workspace</small>
          </div>
          <ChevronRight size={12} />
        </div>
        <div className="landing-preview-nav">
          {(
            [
              ["overview", LayoutDashboard],
              ["activity", Activity],
              ["review", ShieldCheck],
            ] as const
          ).map(([name, Icon]) => (
            <button
              key={name}
              type="button"
              onClick={() => onScreenChange(name)}
              aria-pressed={screen === name}
            >
              <Icon size={15} />
              {screenNames[name]}
              {name === "review" && (
                <span className="landing-preview-count">1</span>
              )}
            </button>
          ))}
        </div>
        <div className="landing-preview-sidebar-bottom">
          <LockKeyhole size={14} />
          <span>
            Owner-controlled policy<small>Screen lock is separate</small>
          </span>
        </div>
      </aside>
      <div className="landing-preview-main">
        <div className="landing-preview-topbar">
          <span>
            <PanelLeft size={13} />
            {screenNames[screen]}
          </span>
          <span className="landing-preview-sample">Sample data</span>
        </div>
        <div className="landing-preview-content" aria-live="polite">
          {screen === "overview" && (
            <>
              <div className="landing-preview-heading">
                <div>
                  <span className="landing-preview-kicker">Workshop desk</span>
                  <h3>Your desk, in context.</h3>
                  <p>Observations and policy, in one place.</p>
                </div>
                <span className="landing-armed-pill">
                  <span />
                  Session armed
                </span>
              </div>
              <div className="landing-overview-body">
                <div className="landing-desk-illustration" aria-hidden="true">
                  <div className="landing-desk-floor" />
                  <div className="landing-mini-screen">
                    <AirlockMark small />
                  </div>
                  <div className="landing-mini-base" />
                  <div className="landing-mini-camera">
                    <span />
                  </div>
                  <div className="landing-mini-signal">
                    <span />
                    <span />
                    <span />
                  </div>
                  <span className="landing-desk-caption">
                    Desk observations
                  </span>
                </div>
                <div className="landing-coverage-list">
                  <h4>Coverage in this example</h4>
                  <div>
                    <Camera size={14} />
                    <span>Desk camera</span>
                    <Check size={13} />
                  </div>
                  <div>
                    <Usb size={14} />
                    <span>USB observations</span>
                    <Check size={13} />
                  </div>
                  <div>
                    <Monitor size={14} />
                    <span>App launches</span>
                    <Check size={13} />
                  </div>
                  <p>
                    Presence is context.
                    <br />
                    It does not establish intent.
                  </p>
                </div>
              </div>
              <div className="landing-preview-section-label">
                <h4>Recent activity</h4>
                <button
                  type="button"
                  onClick={() => onScreenChange("activity")}
                >
                  View activity
                  <ChevronRight size={11} />
                </button>
              </div>
              <div className="landing-event-list">
                {sampleEvents
                  .slice(0, 2)
                  .map(({ time, title, detail, icon: Icon }, index) => (
                    <div className="landing-event-row" key={time}>
                      <span
                        className={`landing-event-icon${index === 0 ? " landing-event-icon-blue" : ""}`}
                      >
                        <Icon size={14} />
                      </span>
                      <div>
                        <strong>{title}</strong>
                        <small>{detail}</small>
                      </div>
                      <time>{time}</time>
                    </div>
                  ))}
              </div>
            </>
          )}
          {screen === "activity" && (
            <>
              <div className="landing-preview-heading">
                <div>
                  <span className="landing-preview-kicker">Event timeline</span>
                  <h3>What was observed.</h3>
                  <p>Keep the sequence. Keep the context.</p>
                </div>
                <span className="landing-armed-pill">
                  <span />3 observations
                </span>
              </div>
              <div className="landing-activity-table">
                <div className="landing-activity-table-head">
                  <span>Observation</span>
                  <span>Classification</span>
                </div>
                {sampleEvents.map(
                  ({ time, title, detail, kind, icon: Icon }, index) => (
                    <div className="landing-activity-table-row" key={time}>
                      <span
                        className={`landing-event-icon${index === 0 ? " landing-event-icon-blue" : ""}`}
                      >
                        <Icon size={15} />
                      </span>
                      <div>
                        <time>{time}</time>
                        <strong>{title}</strong>
                        <small>{detail}</small>
                      </div>
                      <span
                        className={`landing-event-kind${index === 0 ? " landing-event-kind-review" : ""}`}
                      >
                        {kind}
                      </span>
                    </div>
                  ),
                )}
              </div>
              <div className="landing-preview-note">
                <CircleHelp size={16} />
                <p>
                  Nearby events can be related.
                  <br />
                  <strong>
                    Timing alone does not tell us who connected the device.
                  </strong>
                </p>
              </div>
            </>
          )}
          {screen === "review" && (
            <>
              <div className="landing-preview-heading">
                <div>
                  <span className="landing-preview-kicker">
                    Incident review
                  </span>
                  <h3>A connection to review.</h3>
                  <p>A new USB device was outside the baseline.</p>
                </div>
              </div>
              <div className="landing-review-evidence">
                <div className="landing-review-icon">
                  <Usb size={22} />
                </div>
                <div>
                  <strong>Unapproved USB connection</strong>
                  <small>10:42:07 · Sample incident</small>
                </div>
                <span className="landing-event-kind landing-event-kind-review">
                  Awaiting review
                </span>
              </div>
              <div className="landing-review-columns">
                <div>
                  <h4>
                    <Check size={13} />
                    Observed
                  </h4>
                  <p>A new USB device connected.</p>
                  <p>Person presence was recorded four seconds earlier.</p>
                </div>
                <div>
                  <h4>
                    <CircleHelp size={13} />
                    Still unknown
                  </h4>
                  <p>Who connected the device.</p>
                  <p>Whether access was permitted or data was read.</p>
                </div>
              </div>
              <div className="landing-review-policy">
                <LockKeyhole size={17} />
                <div>
                  <strong>The owner sets the response.</strong>
                  <p>This preview executes no actions.</p>
                </div>
                <span>Read only</span>
              </div>
            </>
          )}
        </div>
        <div className="landing-preview-footer">
          <span>
            <span />
            Illustrative workspace
          </span>
          <span>Not connected to a device</span>
        </div>
      </div>
    </div>
  );
}

function DevicePreview({
  device,
  angle,
  screen,
  onScreenChange,
}: {
  device: Device;
  angle: Angle;
  screen: Screen;
  onScreenChange: (screen: Screen) => void;
}) {
  return (
    <div className={`landing-device-stage landing-device-stage-${device}`}>
      <div className="landing-device-shadow" />
      <div
        className={`landing-device landing-device-${device}`}
        style={{ "--landing-angle": `${angles[angle]}deg` } as CSSProperties}
      >
        <div className="landing-device-lid">
          <div className="landing-device-webcam" />
          <div className="landing-device-display">
            <PreviewScreen screen={screen} onScreenChange={onScreenChange} />
          </div>
          {device === "desktop" && (
            <span className="landing-monitor-chin">
              <AirlockMark small />
            </span>
          )}
        </div>
        {device === "laptop" ? (
          <div className="landing-laptop-base" aria-hidden="true">
            <div className="landing-keyboard-speaker landing-keyboard-speaker-left" />
            <div className="landing-keyboard">
              {Array.from({ length: 60 }, (_, index) => (
                <i key={index} />
              ))}
            </div>
            <div className="landing-keyboard-speaker landing-keyboard-speaker-right" />
            <div className="landing-trackpad" />
            <div className="landing-base-edge" />
          </div>
        ) : (
          <div className="landing-monitor-support" aria-hidden="true">
            <div className="landing-monitor-neck" />
            <div className="landing-monitor-foot" />
          </div>
        )}
      </div>
    </div>
  );
}

export default function LandingPage() {
  const [device, setDevice] = useState<Device>("laptop");
  const [angle, setAngle] = useState<Angle>("front");
  const [screen, setScreen] = useState<Screen>("overview");
  const [dark, setDark] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openExpandedPreview = () => {
    if (dialogRef.current && !dialogRef.current.open)
      dialogRef.current.showModal();
  };

  return (
    <div className={`landing-page${dark ? " landing-dark" : ""}`}>
      <a className="landing-skip-link" href="#landing-main">
        Skip to content
      </a>
      <header className="landing-header">
        <div className="landing-header-inner">
          <a href="#" className="landing-wordmark" aria-label="Airlock home">
            <AirlockMark />
            Airlock
          </a>
          <nav className="landing-header-nav" aria-label="Main navigation">
            <a href="#experience">Explore</a>
            <a href="#how-it-works">How it works</a>
            <a
              className="landing-repo-link"
              href="https://github.com/Dhruvjain35/locki-airlock"
              target="_blank"
              rel="noreferrer"
            >
              <Github size={16} />
              <span>GitHub</span>
            </a>
          </nav>
          <div className="landing-header-actions">
            <button
              type="button"
              className="landing-theme-toggle"
              onClick={() => setDark(!dark)}
              aria-label={
                dark
                  ? "Switch to white background"
                  : "Switch to black background"
              }
              aria-pressed={dark}
            >
              {dark ? <Sun size={17} /> : <Moon size={17} />}
            </button>
            <a className="landing-console-link" href="?product=airlock">
              Open console
              <ArrowUpRight size={14} />
            </a>
          </div>
        </div>
      </header>
      <main id="landing-main">
        <section className="landing-hero" aria-labelledby="landing-title">
          <h1 id="landing-title">
            Know what happened
            <br />
            at your desk.
          </h1>
          <p>
            Camera context. Workstation events. Your response.
            <br className="landing-desktop-break" /> A clearer view of the
            workspace you stepped away from.
          </p>
          <div className="landing-hero-actions">
            <a className="landing-primary-button" href="#experience">
              Explore Airlock
              <ArrowDown size={16} />
            </a>
            <a className="landing-text-link" href="?product=airlock">
              Open the console
              <ArrowUpRight size={16} />
            </a>
          </div>
        </section>
        <section
          className="landing-experience"
          id="experience"
          aria-label="Interactive product preview"
        >
          <div className="landing-experience-top">
            <div
              className="landing-segmented"
              role="group"
              aria-label="Preview device"
            >
              <button
                type="button"
                aria-pressed={device === "laptop"}
                onClick={() => setDevice("laptop")}
              >
                <Laptop size={15} />
                MacBook
              </button>
              <button
                type="button"
                aria-pressed={device === "desktop"}
                onClick={() => setDevice("desktop")}
              >
                <Monitor size={15} />
                Desktop
              </button>
            </div>
            <div className="landing-experience-tools">
              <span className="landing-sample-label">
                <span />
                Interactive preview · sample data
              </span>
              <button
                className="landing-expand-button"
                type="button"
                onClick={openExpandedPreview}
                aria-haspopup="dialog"
              >
                Expand screen
                <Expand size={14} />
              </button>
            </div>
          </div>
          <DevicePreview
            device={device}
            angle={angle}
            screen={screen}
            onScreenChange={setScreen}
          />
          <div className="landing-experience-controls">
            <div className="landing-control-group">
              <span>View</span>
              <div
                className="landing-segmented"
                role="group"
                aria-label="Viewing angle"
              >
                {(["left", "front", "right"] as const).map((value) => (
                  <button
                    type="button"
                    key={value}
                    aria-pressed={angle === value}
                    onClick={() => setAngle(value)}
                  >
                    {value === "left"
                      ? "Left"
                      : value === "right"
                        ? "Right"
                        : "Front"}
                  </button>
                ))}
              </div>
            </div>
            <div className="landing-control-divider" />
            <div className="landing-control-group">
              <span>Screen</span>
              <div
                className="landing-segmented"
                role="group"
                aria-label="Preview screen"
              >
                {(["overview", "activity", "review"] as const).map((value) => (
                  <button
                    type="button"
                    key={value}
                    aria-pressed={screen === value}
                    onClick={() => setScreen(value)}
                  >
                    {screenNames[value]}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <p className="landing-experience-caption">
            One workspace. Different perspectives.
            <br className="landing-mobile-break" /> Switch the view and look
            through the evidence.
          </p>
        </section>
        <section
          className="landing-workflow landing-section"
          id="how-it-works"
          aria-labelledby="landing-workflow-title"
        >
          <div className="landing-section-heading">
            <h2 id="landing-workflow-title">
              An observation.
              <br />
              Then an informed response.
            </h2>
            <p>
              A person near your desk is context, not a verdict. Airlock brings
              physical observations and selected computer events into the same
              conversation.
            </p>
          </div>
          <div className="landing-steps">
            <article>
              <span className="landing-step-icon">
                <Eye size={23} strokeWidth={1.5} />
              </span>
              <h3>Observe the change.</h3>
              <p>
                Camera presence, USB connections and visible app launches give
                you a record of what was observed.
              </p>
            </article>
            <article>
              <span className="landing-step-icon">
                <Activity size={23} strokeWidth={1.5} />
              </span>
              <h3>Review the context.</h3>
              <p>
                Inspect the timeline from an authorized console. The
                investigator explains the evidence and what remains unknown.
              </p>
            </article>
            <article>
              <span className="landing-step-icon">
                <ShieldCheck size={23} strokeWidth={1.5} />
              </span>
              <h3>Keep control.</h3>
              <p>
                The owner chooses the response policy. Presence alone does not
                request an automatic screen lock.
              </p>
            </article>
          </div>
        </section>
        <section
          className="landing-hardware-section landing-section"
          aria-labelledby="landing-hardware-title"
        >
          <div
            className="landing-hardware-composition"
            aria-label="A fixed camera observes the desk, a Jetson processes observations, and a speaker delivers local warnings"
          >
            <div className="landing-hardware-line" />
            <div className="landing-hardware-part landing-camera-part">
              <div className="landing-camera-object">
                <div className="landing-camera-lens">
                  <span />
                </div>
                <div className="landing-camera-grille" />
              </div>
              <span>
                <Camera size={14} />
                Desk camera
              </span>
            </div>
            <div className="landing-hardware-part landing-jetson-part">
              <div className="landing-jetson-object">
                <div className="landing-jetson-fan">
                  {Array.from({ length: 8 }, (_, index) => (
                    <i
                      key={index}
                      style={{ transform: `rotate(${index * 45}deg)` }}
                    />
                  ))}
                  <b />
                </div>
                <span />
              </div>
              <span>
                <Cpu size={14} />
                Jetson
              </span>
            </div>
            <div className="landing-hardware-part landing-speaker-part">
              <div className="landing-speaker-object">
                <span />
                <span />
                <span />
              </div>
              <span>
                <AudioLines size={14} />
                Local speaker
              </span>
            </div>
          </div>
          <div className="landing-hardware-copy">
            <h2 id="landing-hardware-title">
              A fixed view.
              <br />A useful point of view.
            </h2>
            <p>
              A camera and Jetson provide a separate view of the desk. Local
              speech can report an event without waiting for the investigation
              model.
            </p>
            <p className="landing-secondary-copy">
              No patrol required. The current product is built around a
              stationary workspace.
            </p>
          </div>
        </section>
        <section
          className="landing-boundary-section landing-section"
          aria-labelledby="landing-boundary-title"
        >
          <div className="landing-boundary-symbol">
            <LockKeyhole size={38} strokeWidth={1.3} />
          </div>
          <div>
            <h2 id="landing-boundary-title">
              Your Mac’s lock
              <br />
              still comes first.
            </h2>
            <p>
              Airlock adds context and review around ordinary access protection.
              It cannot see every action inside an already-open app, infer
              intent, or physically prevent theft.
            </p>
            <div className="landing-boundary-details">
              <span>
                <Check size={15} />
                Owner-controlled responses
              </span>
              <span>
                <X size={15} />
                No “hacker” classification
              </span>
            </div>
            <p className="landing-prototype-note">
              Integration prototype. Screen locks default to dry run. Physical
              accuracy, recovery and quiet behavior around coworkers are still
              being evaluated.
            </p>
          </div>
        </section>
        <section className="landing-closing landing-section">
          <h2>
            See the workspace.
            <br />
            Understand the event.
          </h2>
          <a className="landing-primary-button" href="?product=airlock">
            Open the console
            <ArrowUpRight size={16} />
          </a>
        </section>
      </main>
      <dialog
        className="landing-expanded-dialog"
        ref={dialogRef}
        aria-labelledby="landing-expanded-title"
        onClick={(event) => {
          if (event.target === event.currentTarget) dialogRef.current?.close();
        }}
      >
        <div className="landing-expanded-header">
          <div>
            <h2 id="landing-expanded-title">Workspace preview</h2>
            <p>Sample data. No connected devices.</p>
          </div>
          <button
            type="button"
            onClick={() => dialogRef.current?.close()}
            autoFocus
            aria-label="Close expanded preview"
          >
            <X size={20} />
          </button>
        </div>
        <div className="landing-expanded-tabs">
          <div
            className="landing-segmented"
            role="group"
            aria-label="Expanded preview screen"
          >
            {(["overview", "activity", "review"] as const).map((value) => (
              <button
                type="button"
                key={value}
                aria-pressed={screen === value}
                onClick={() => setScreen(value)}
              >
                {screenNames[value]}
              </button>
            ))}
          </div>
        </div>
        <div className="landing-expanded-body">
          <PreviewScreen screen={screen} onScreenChange={setScreen} />
        </div>
      </dialog>
      <footer className="landing-footer">
        <a className="landing-wordmark" href="#">
          <AirlockMark />
          Airlock
        </a>
        <span>Built for HackUTD 2026.</span>
        <a
          href="https://github.com/Dhruvjain35/locki-airlock"
          target="_blank"
          rel="noreferrer"
        >
          Explore the repository
          <ArrowUpRight size={14} />
        </a>
      </footer>
    </div>
  );
}
