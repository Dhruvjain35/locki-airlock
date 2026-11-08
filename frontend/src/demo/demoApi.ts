// AIRLOCK demo mode: the backend's own routes (backend/app/api/security_routes.py). Nothing here invents state:
// every field shown in the demo view comes from GET /api/security/demo.

export type Step = "start" | "enroll" | "recognized" | "zones" | "arm" | "monitor" | "incident" | "owner_return" | "disarm";
export type Mode = "NORMAL" | "WATCHFUL" | "DEFENSIVE" | "CRITICAL" | "OWNER_RETURNED";

export interface Stat {
  p50?: number | null;
  p95?: number | null;
  n?: number;
}

export interface Ready {
  ok: boolean;
  detail: string;
  nemotron?: boolean;
}

export interface VisualState {
  armed_status: boolean;
  person_present: boolean;
  at_desk: boolean;
  visit: number;
  person_track_id: number | null;
  face_identity_state: "owner" | "unrecognized" | "uncertain" | null;
  identity_confidence: number | null;
  identity_detail: string | null;
  keyboard_interaction: boolean;
  laptop_interaction: boolean;
  protected_device_interaction: boolean;
  usb_zone_interaction: boolean;
  last_seen_timestamp: number | null;
  camera_health: string;
  current_security_mode: Mode;
  previous_mode: Mode | null;
  mode_since: number;
  mode_reason: string;
  evidence_ids: string[];
}

export interface DemoSnap {
  face_identification_enabled?: boolean;
  active: boolean;
  id: string | null;
  mode: "live" | "rehearsal" | null;
  label: string | null;
  started_at: number | null;
  enrolling: boolean;
  enrollment: { accepted: number; needed: number; poses: Record<string, number>; hint: string; last_problem: string | null; rejected: Record<string, number>; complete: boolean; error?: string } | null;
  enrolled: boolean;
  enrolled_samples: number | null;
  recognized: boolean;
  template: string | null;
  thresholds: { match: number; reject: number; calibrated: boolean } | null;
  steps: Partial<Record<Step, "done" | "pending" | "default">>;
  readiness: Record<string, Ready>;
  timings: Record<string, Stat | number>;
  history: Record<string, Stat>;
  timeline: { at: number; kind: string; text: string; zone?: string; mode?: string }[];
  fixtures: { kind: string; event_id: string; at: number }[];
  trials: Trial[];
  trial_summary: TrialSummary;
  error: string | null;
  visual_state: VisualState;
  armed: boolean;
  arming: "database" | "local";
  camera_running: boolean;
  preview: string;
}

export interface Trial {
  n: number;
  label: "genuine" | "impostor";
  participant: string;
  state: "running" | "done";
  outcome: "owner" | "unrecognized" | "uncertain" | "missed" | null;
  identity_ms: number | null;
  arrival_to_identity_ms?: number | null;
  presence_ms: number | null;
  agreement: number | null;
  correct?: boolean;
}

export interface TrialSummary {
  trials: number;
  participants: string[];
  genuine: { n: number; matched: number; false_unrecognized: number; uncertain: number; missed: number };
  impostor: { n: number; false_owner_match: number; unrecognized: number; uncertain: number; missed: number };
  identity_ms: Record<"owner" | "unrecognized", Stat & { max?: number | null }>;
  arrival_to_identity_ms: Stat & { max?: number | null };
  note: string;
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/security${path}`, {
    method,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const demoApi = {
  get: () => call<DemoSnap>("GET", "/demo"),
  start: (mode: "live" | "rehearsal") => call<DemoSnap>("POST", "/demo/start", { mode }),
  enroll: () => call<DemoSnap>("POST", "/demo/enroll", {}),
  reset: () => call<DemoSnap>("POST", "/demo/reset", {}),
  end: () => call<unknown>("POST", "/demo/end", {}),
  fixtureUsb: () => call<DemoSnap>("POST", "/demo/rehearsal/usb", {}),
  trial: (label: "genuine" | "impostor", participant: string) => call<DemoSnap>("POST", "/demo/trial", { label, participant }),
  arm: (owner_code: string) => call<unknown>("POST", "/arm", { owner_code }),
  disarm: (owner_code: string) => call<unknown>("POST", "/disarm", { owner_code }),
};
