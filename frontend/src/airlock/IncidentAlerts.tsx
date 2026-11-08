import { useEffect, useRef, useState } from "react";
import { Bell, BellRing } from "lucide-react";
import { type Row, ago } from "./format";
import { acknowledged, alertText, incidentsToAlert, isHighSeverity, SEVERITY_RANK } from "./alerts";
import { Badge, Button } from "./ui";

const storeKey = (profile: string) => `airlock.alerted.v1:${profile}`;
const load = (profile: string): Record<string, number> => { try { return JSON.parse(localStorage.getItem(storeKey(profile)) || "{}"); } catch { return {}; } };
const save = (profile: string, v: Record<string, number>) => { try { localStorage.setItem(storeKey(profile), JSON.stringify(v)); } catch { /* private window */ } };

/** The owner's alert: an in-page banner for the newest open incident, and one desktop notification per incident (again only if it
 *  gets more severe). Everything shown comes from the module's incident row; nothing is inferred about the person. */
export function IncidentAlerts({ incidents, acknowledgements, workstation, profile, now, onSelect }: { incidents: Row[]; acknowledgements: Row[]; workstation: string; profile: string; now: number; onSelect: (id: string) => void }) {
  const alerted = useRef<Record<string, number>>(load(profile));
  const [permission, setPermission] = useState<string>(typeof Notification === "undefined" ? "unsupported" : Notification.permission);
  const [banner, setBanner] = useState<Row | null>(null);

  useEffect(() => {
    const fresh = incidentsToAlert(incidents, alerted.current, Date.now());
    if (!fresh.length) return;
    for (const incident of fresh) {
      alerted.current[String(incident.id)] = SEVERITY_RANK[String(incident.severity)] ?? 0;
      if (permission === "granted") {
        const { title, body } = alertText(incident, workstation);
        try {
          const n = new Notification(title, { body, tag: String(incident.id), requireInteraction: isHighSeverity(incident.severity) });
          n.onclick = () => { window.focus(); onSelect(String(incident.id)); n.close(); };
        } catch { /* the in-page banner still shows it */ }
      }
    }
    save(profile, alerted.current);
    setBanner(fresh[0]);
  }, [incidents, permission, workstation, profile, onSelect]);

  const current = banner && incidents.find(i => i.id === banner.id);
  return <div className="al-inline">
    {permission === "default" && <Button onClick={async () => setPermission(await Notification.requestPermission())}><Bell size={15} />Enable incident alerts</Button>}
    {permission === "denied" && <span className="al-small al-muted">Desktop alerts are blocked in this browser; alerts still show here.</span>}
    {current && current.state !== "resolved" && <button className="al-text-button" onClick={() => onSelect(String(current.id))}>
      <Badge tone={isHighSeverity(current.severity) ? "magenta" : "amber"} dot><BellRing size={12} />{alertText(current, workstation).title}</Badge>
      <span className="al-small">{current.summary} · {ago(current.updatedAtMs, now)} · {acknowledged(current, acknowledgements) ? "acknowledged" : "not acknowledged"}</span>
    </button>}
  </div>;
}
