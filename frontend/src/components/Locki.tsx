import type { QuestMood } from "../types/api";

/** LOCKI's face on the dashboard. The mood comes from the backend (missions/service.py), which works
 *  it out from real test results, so the face can never celebrate something that did not happen. */

const TONE: Record<QuestMood, string> = {
  idle: "var(--color-ink-4)",
  waiting: "var(--color-ink-2)",
  focused: "var(--color-live)",
  watching: "var(--color-live)",
  thinking: "var(--color-live-2)",
  hyped: "var(--color-ok)",
  celebrating: "var(--color-ok)",
  helping: "var(--color-live)",
  concerned: "var(--color-sim)",
  suspicious: "var(--color-sim)",
  worried: "var(--color-hit)",
  alarmed: "var(--color-danger)",
};

export const MOOD_LABEL: Record<QuestMood, string> = {
  idle: "Idle",
  waiting: "Waiting for a failing test",
  focused: "Watching your code",
  watching: "Running your tests",
  thinking: "Reading the failure",
  hyped: "That landed",
  celebrating: "Boss beaten",
  helping: "Has a hint",
  concerned: "You look stuck",
  suspicious: "A test file changed",
  worried: "The boss healed",
  alarmed: "The tests can't run",
};

function Eye({ x, mood }: { x: number; mood: QuestMood }) {
  const y = 40;
  switch (mood) {
    case "idle":
      return <path d={`M${x - 7} ${y}h14`} strokeWidth="3.5" />;
    case "hyped":
    case "helping":
      return <path d={`M${x - 7} ${y + 2}q7 -10 14 0`} strokeWidth="3.5" />;
    case "celebrating":
      return <path d={`M${x} ${y - 8}l2.2 5.6 5.8 .4-4.5 3.8 1.5 5.7-5-3.1-5 3.1 1.5-5.7-4.5-3.8 5.8-.4z`} fill="currentColor" stroke="none" />;
    case "suspicious":
      return (
        <>
          <path d={`M${x - 8} ${y - 3}h16`} strokeWidth="3" />
          <circle cx={x + 3} cy={y + 2} r="3" fill="currentColor" stroke="none" />
        </>
      );
    case "alarmed":
      return <circle cx={x} cy={y} r="6.5" strokeWidth="3" fill="none" />;
    case "thinking":
      return x < 50 ? <path d={`M${x - 7} ${y}h14`} strokeWidth="3.5" /> : <circle cx={x} cy={y} r="4.5" fill="currentColor" stroke="none" />;
    case "worried":
    case "concerned":
      return (
        <>
          <path d={x < 50 ? `M${x - 7} ${y - 6}l12 -4` : `M${x - 5} ${y - 10}l12 4`} strokeWidth="2.5" />
          <circle cx={x} cy={y + 1} r="4" fill="currentColor" stroke="none" />
        </>
      );
    default:
      return (
        <circle cx={x} cy={y} r="4.5" fill="currentColor" stroke="none" className={mood === "watching" ? "locki-scan" : "locki-blink"} style={{ transformOrigin: `${x}px ${y}px` }} />
      );
  }
}

function Mouth({ mood }: { mood: QuestMood }) {
  switch (mood) {
    case "hyped":
    case "celebrating":
    case "helping":
      return <path d="M38 58q12 12 24 0" strokeWidth="3.5" />;
    case "alarmed":
      return <circle cx="50" cy="62" r="5" strokeWidth="3" fill="none" />;
    case "worried":
    case "concerned":
      return <path d="M40 65q10 -8 20 0" strokeWidth="3" />;
    case "suspicious":
      return <path d="M40 62l20 -3" strokeWidth="3" />;
    case "idle":
      return <path d="M45 62h10" strokeWidth="3" />;
    default:
      return <path d="M41 60q9 4 18 0" strokeWidth="3" />;
  }
}

export function Locki({ mood, size = 84 }: { mood: QuestMood; size?: number }) {
  return (
    <svg viewBox="0 0 100 100" width={size} height={size} role="img" aria-label={`LOCKI: ${MOOD_LABEL[mood]}`} className={mood === "celebrating" ? "locki-bounce" : undefined} style={{ color: TONE[mood], flex: "none" }}>
      <g fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        <rect x="14" y="20" width="72" height="62" rx="20" strokeWidth="3.5" />
        <path d="M50 20v-9" strokeWidth="3.5" />
        <circle cx="50" cy="8" r="3.5" fill="currentColor" stroke="none" className={mood === "watching" || mood === "thinking" ? "blink" : undefined} />
        <Eye x={35} mood={mood} />
        <Eye x={65} mood={mood} />
        <Mouth mood={mood} />
      </g>
    </svg>
  );
}
