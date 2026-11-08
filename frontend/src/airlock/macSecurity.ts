import { useEffect, useState } from "react";

// The Mac security backend's own report (GET /api/security on this Mac, port 8077 behind the dev server's proxy).
// Nothing here invents state: when the backend can't be reached the command center shows "unavailable", never a guess.

export type Stage = "NORMAL" | "WATCHFUL" | "DEFENSIVE" | "CRITICAL" | "OWNER_RETURNED";
export const STAGES: Stage[] = ["NORMAL", "WATCHFUL", "DEFENSIVE", "CRITICAL", "OWNER_RETURNED"];

export type MacSecurity = Record<string, any>;
export type MacLink = { status: "connecting" | "live" | "offline"; data: MacSecurity | null; error: string | null; at: number | null };

export const REMOTE_STALE_MS = 15_000;

/** Polls the Mac backend once a second. Keeps the last good report but marks the link offline on failure, so stale data
 *  is never shown as live. */
export function useMacSecurity(intervalMs = 1000): MacLink {
  const [link, setLink] = useState<MacLink>({ status: "connecting", data: null, error: null, at: null });
  useEffect(() => {
    let stop = false;
    let timer = 0;
    const load = async () => {
      try {
        const res = await fetch("/api/security", { cache: "no-store" });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = await res.json();
        if (!stop) setLink({ status: "live", data, error: null, at: Date.now() });
      } catch (e) {
        if (!stop) setLink(prev => ({ ...prev, status: "offline", error: e instanceof Error ? e.message : String(e) }));
      } finally {
        if (!stop) timer = window.setTimeout(load, intervalMs);
      }
    };
    void load();
    return () => { stop = true; window.clearTimeout(timer); };
  }, [intervalMs]);
  return link;
}

/** The stage the backend actually reports, or null when it can't be read (offline, or a value this UI doesn't know). */
export function reportedStage(link: MacLink): Stage | null {
  if (link.status !== "live") return null;
  const mode = link.data?.visual_state?.current_security_mode;
  return STAGES.includes(mode) ? mode : null;
}

/** Jetson presence freshness from the server's own receipt time (the camera's clock is never trusted for staleness). */
export function remoteFreshness(remote: any, now: number): "fresh" | "stale" | "none" {
  const received = Number(remote?.server_received_at_ms);
  if (!remote || !Number.isFinite(received)) return "none";
  return now - received <= REMOTE_STALE_MS ? "fresh" : "stale";
}

export function latestInvestigationId(turns: any[] | undefined): { id: string; turn: any } | null {
  for (const t of turns ?? []) {
    const id = t?.nemotron?.investigation_id;
    if (typeof id === "string" && /^inv_[A-Za-z0-9_-]{1,80}$/.test(id)) return { id, turn: t };
  }
  return null;
}
