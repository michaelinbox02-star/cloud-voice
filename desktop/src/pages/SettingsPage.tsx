import { useEffect, useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import type { SystemInfo } from "../api";
import {
  brandVirtualMicrophone,
  downloadBackup,
  restoreBackup,
  restoreVirtualMicrophone,
  virtualMicrophoneStatus,
} from "../api";
import type { MicrophoneStatus } from "../api";

type Props = { online: boolean; system: SystemInfo | null; onRestored: () => void };

export function SettingsPage({ online, system, onRestored }: Props) {
  const [busy, setBusy] = useState<"backup" | "restore" | null>(null);
  const [microphone, setMicrophone] = useState<MicrophoneStatus | null>(null);
  const [micBusy, setMicBusy] = useState(false);
  const [micMessage, setMicMessage] = useState("");
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

  const refreshMicrophone = async () => {
    try {
      setMicrophone(await virtualMicrophoneStatus());
    } catch (error) {
      setMicMessage(String(error));
    }
  };

  useEffect(() => {
    void refreshMicrophone();
  }, []);

  const brandMicrophone = async () => {
    setMicBusy(true);
    setMicMessage("Waiting for administrator approval…");
    try {
      const updated = await brandVirtualMicrophone();
      setMicrophone(updated);
      setMicMessage(
        "Done. Restart the call app so it re-reads its device list, then pick Cloud Voice Microphone.",
      );
    } catch (error) {
      setMicMessage(String(error));
    } finally {
      setMicBusy(false);
    }
  };

  const undoMicrophone = async () => {
    setMicBusy(true);
    setMicMessage("Restoring the previous name…");
    try {
      setMicrophone(await restoreVirtualMicrophone());
      setMicMessage("The previous device name was restored.");
    } catch (error) {
      setMicMessage(String(error));
    } finally {
      setMicBusy(false);
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
            <h2>Virtual microphone</h2>
            <p>
              Call apps list the cable's other end. Naming it here means Zoom, WhatsApp, Skype and Discord show one
              obvious device instead of a cable whose function is not obvious.
            </p>
          </div>
          {microphone?.branded && <span className="pill good">Named</span>}
        </div>

        {microphone && (
          <div className="stat-grid">
            <div className="stat">
              <span className="stat-label">Cable</span>
              <span className="stat-value">{microphone.driver ?? "none found"}</span>
              <span className="stat-note">{microphone.current_name ?? "—"}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Apps will show</span>
              <span className="stat-value">{microphone.branded ? "Cloud Voice Microphone" : "the current name"}</span>
              <span className="stat-note">{microphone.available ? "ready to rename" : "install VB-CABLE first"}</span>
            </div>
          </div>
        )}

        <div className="actions">
          <button
            className="secondary"
            disabled={micBusy || !microphone?.available || microphone?.branded}
            onClick={() => void brandMicrophone()}
          >
            {micBusy ? "Working…" : "Name it Cloud Voice Microphone"}
          </button>
          <button
            className="secondary"
            disabled={micBusy || !microphone?.branded}
            onClick={() => void undoMicrophone()}
          >
            Restore original name
          </button>
        </div>
        <div className={`result ${micMessage.toLowerCase().includes("done") ? "good" : ""}`}>
          <span className="result-indicator" />
          <span>{micMessage || microphone?.note || "Checking for a virtual audio cable…"}</span>
        </div>
        <p className="muted small">
          Renaming asks for administrator approval once, because Windows stores device names system-wide. The previous
          name is saved so it can be put back.
        </p>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="step">3</span>
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
