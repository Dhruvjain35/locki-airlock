import { useEffect, useRef, useState } from "react";
import type { ConnectionConfig } from "./client";
import { Button, FormField, Modal } from "./ui";
import { credentialKey, MAX_CREDENTIAL_BYTES, parseOwnerCredential, recoverOwnerProfile, recoveryDestination, savedOwnerProfiles } from "./ownerRecoveryPolicy";

export function OwnerRecovery({ config, workstationId, onClose }: { config: ConnectionConfig; workstationId: string; onClose: () => void }) {
  const [station, setStation] = useState(workstationId);
  const [profile, setProfile] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [consentedDestination, setConsentedDestination] = useState("");
  const destinationKey = JSON.stringify([config.uri, config.database, config.profile]);
  const confirmed = consentedDestination === destinationKey;
  const setConfirmed = (value: boolean) => setConsentedDestination(value ? destinationKey : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  const active = useRef<AbortController | null>(null);
  useEffect(() => {
    setConfirmed(false); setBusy(false); setUrl("");
    return () => active.current?.abort();
  }, [config.uri, config.database, config.profile]);
  let destination = "", profiles: string[] = [];
  try { destination = recoveryDestination(config); profiles = savedOwnerProfiles(localStorage, config); } catch { /* disabled below */ }
  const close = () => { active.current?.abort(); onClose(); };
  return <Modal title="Recover existing owner access" subtitle="Use an existing owner credential. This does not create a workstation or grant a role." onClose={close}>
    <form className="al-form" onSubmit={async e => {
      e.preventDefault();
      if (busy || !confirmed || !destination || (!profile && !file)) return;
      const controller = new AbortController(); active.current = controller;
      setBusy(true); setError("");
      try {
        let token: string;
        if (file) {
          if (file.size > MAX_CREDENTIAL_BYTES) throw new Error("Credential file exceeds 64 KiB.");
          token = parseOwnerCredential(await file.text());
        } else {
          const existing = localStorage.getItem(credentialKey({ ...config, profile }));
          if (!existing) throw new Error("The selected saved credential is no longer available.");
          token = parseOwnerCredential(existing);
        }
        const recovered = await recoverOwnerProfile(config, station.trim(), token, controller.signal, localStorage, location.href);
        if (!controller.signal.aborted) { setUrl(recovered); setFile(null); }
      } catch (err) {
        if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Recovery failed.");
      } finally { if (!controller.signal.aborted) setBusy(false); }
    }}>
      <p>The selected file is parsed in this browser. Only its credential token is sent to the destination below for authentication; the whole file is never uploaded. Verification reads two owner-scoped views and calls no reducers.</p>
      <FormField label="Existing workstation ID"><input required value={station} disabled={busy || !!url} onChange={e => { setStation(e.target.value); setConfirmed(false); }} /></FormField>
      <FormField label="Existing saved client slot"><select value={profile} disabled={busy || !!url} onChange={e => { setProfile(e.target.value); setFile(null); setConfirmed(false); }}><option value="">Choose an existing client slot</option>{profiles.map(p => <option key={p} value={p}>{p}</option>)}</select></FormField>
      <FormField label="Or select a local existing credential file" hint="Plain token, JSON token/spacetimedb_token, or a CLI TOML top-level spacetimedb_token. Maximum 64 KiB."><input type="file" disabled={busy || !!url} onChange={e => { setFile(e.target.files?.[0] || null); setProfile(""); setConfirmed(false); }} /></FormField>
      <p>Authentication destination: <strong>{destination || "Unsupported destination; change connection settings first."}</strong></p>
      <label className="al-checkbox"><input type="checkbox" checked={confirmed} disabled={busy || !!url} onChange={e => setConfirmed(e.target.checked)} /><span>I trust this destination and want to authenticate with my existing owner credential. A verified credential will be saved in a new owner client slot.</span></label>
      {error && <p role="alert">{error}</p>}
      {url && <p role="status">Owner verified. Your original client slot was preserved. <a href={url}>Open the verified owner console</a></p>}
      <div className="al-modal-actions"><Button type="button" onClick={close}>{url ? "Close" : "Cancel"}</Button><Button type="submit" disabled={busy || !!url || !confirmed || !destination || (!profile && !file)}>{busy ? "Verifying owner…" : "Verify existing owner"}</Button></div>
    </form>
  </Modal>;
}
