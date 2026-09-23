import { useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import type { SystemInfo } from "../api";
import { downloadBackup, restoreBackup } from "../api";

type Props = { online: boolean; system: SystemInfo | null; onRestored: () => void };

export function SettingsPage({ online, system, onRestored }: Props) {
  const [busy, setBusy] = useState<"backup" | "restore" | null>(null);
  const [status, setStatus] = useState<{ tone: "neutral" | "good" | "bad"; message: string }>({
    tone: "neutral",
    message: "Export a backup before you destroy a GPU server, and restore it on the replacement.",
  });

  const exportBackup = async () => {
    const stamp = new Date().toISOString().slice(0, 10);
    const destination = await save({
      defaultPath: `cloud-voice-backup-${stamp}.tar.gz`,
      filters: [{ name: "Cloud Voice backup", extensions: ["gz"] }],
    });
    if (typeof destination !== "string") return;
    setBusy("backup");
    setStatus({ tone: "neutral", message: "Packaging the voice library and database…" });
    try {
      const saved = await downloadBackup(destination);
      setStatus({ tone: "good", message: `Backup written to ${saved}` });
    } catch (error) {
      setStatus({ tone: "bad", message: String(error) });
    } finally {
      setBusy(null);
    }
  };

  const importBackup = async () => {
    const selection = await open({ multiple: false, filters: [{ name: "Cloud Voice backup", extensions: ["gz", "tgz"] }] });
    if (typeof selection !== "string") return;
    setBusy("restore");
    setStatus({ tone: "neutral", message: "Restoring voices onto this worker…" });
    try {
      const result = await restoreBackup(selection);
      onRestored();
      setStatus({
        tone: "good",
        message: `Restored ${result.restored_voices} voices. The library now holds ${result.voices}.`,
      });
    } catch (error) {
      setStatus({ tone: "bad", message: String(error) });
    } finally {
      setBusy(null);
    }
  };

  if (!online) {
    return (
      <div className="page">
        <div className="eyebrow">WORKER</div>
        <h1>Settings</h1>
        <div className="empty">Connect to a GPU worker to manage backups and engines.</div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="eyebrow">WORKER</div>
      <h1>Settings</h1>
      <p className="lede">
        Voices, models and the job database live on the worker. A backup is everything you would need to rebuild it
        somewhere else.
      </p>

      <section className="panel">
        <div className="panel-head">
          <span className="step">1</span>
          <div>
            <h2>Backup and restore</h2>
            <p>The archive contains the voice library plus the database, including RVC models and indexes.</p>
          </div>
        </div>
        <div className="actions">
          <button className="secondary" disabled={busy !== null} onClick={() => void exportBackup()}>
            {busy === "backup" ? "Exporting…" : "Export backup…"}
          </button>
          <button className="secondary" disabled={busy !== null} onClick={() => void importBackup()}>
            {busy === "restore" ? "Restoring…" : "Restore from backup…"}
          </button>
        </div>
        <div className={`result ${status.tone}`}>
          <span className="result-indicator" />
          <span>{status.message}</span>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="step">2</span>
          <div>
            <h2>Engine status</h2>
            <p>Each model family runs in its own container so dependencies never collide.</p>
          </div>
        </div>
        <div className="stat-grid">
          {Object.entries(system?.engines ?? {}).map(([name, engine]) => (
            <div className="stat" key={name}>
              <span className="stat-label">{name}</span>
              <span className="stat-value">{engine.status ?? "unknown"}</span>
              <span className="stat-note">
                {engine.models_loaded !== undefined
                  ? engine.models_loaded
                    ? "models loaded"
                    : "idle"
                  : engine.device ?? engine.detail ?? "—"}
              </span>
            </div>
          ))}
          <div className="stat">
            <span className="stat-label">Disk</span>
            <span className="stat-value">{system?.disk.free_gib ?? "—"} GiB free</span>
            <span className="stat-note">{system?.disk.total_gib ?? "—"} GiB total</span>
          </div>
        </div>
      </section>
    </div>
  );
}
