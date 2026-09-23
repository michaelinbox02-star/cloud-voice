import { useEffect, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { convertFileSrc } from "@tauri-apps/api/core";
import type { Voice } from "../api";
import { createVoice, deleteVoice, listVoices, workerFetchArtifact } from "../api";
import { engineLabel, formatBytes, formatTimestamp } from "../format";

type Props = { online: boolean; onCountChange: (count: number) => void };

export function VoicesPage({ online, onCountChange }: Props) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState<{ id: string; path: string } | null>(null);
  const [draft, setDraft] = useState({ name: "", engine: "seed-vc", description: "", referencePath: "" });

  const reload = async () => {
    try {
      const result = await listVoices();
      setVoices(result.voices);
      onCountChange(result.voices.length);
      setError("");
    } catch (problem) {
      setError(String(problem));
    }
  };

  useEffect(() => {
    if (online) void reload();
    else setVoices([]);
  }, [online]);

  const chooseReference = async () => {
    const selection = await open({
      multiple: false,
      filters: [{ name: "Audio", extensions: ["wav", "flac", "mp3", "m4a", "ogg"] }],
    });
    if (typeof selection === "string") {
      setDraft((current) => ({ ...current, referencePath: selection }));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await createVoice({
        name: draft.name,
        engine: draft.engine,
        description: draft.description || undefined,
        referencePath: draft.referencePath || undefined,
      });
      setDraft({ name: "", engine: draft.engine, description: "", referencePath: "" });
      await reload();
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  const preview = async (voice: Voice) => {
    if (!voice.reference_audio) return;
    if (playing?.id === voice.id) {
      setPlaying(null);
      return;
    }
    try {
      const extension = voice.reference_audio.split(".").pop() || "wav";
      const local = await workerFetchArtifact(`/v1/voices/${voice.id}/reference`, `${voice.id}.${extension}`);
      setPlaying({ id: voice.id, path: local });
    } catch (problem) {
      setError(String(problem));
    }
  };

  const remove = async (voice: Voice) => {
    setBusy(true);
    try {
      await deleteVoice(voice.id);
      await reload();
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  if (!online) {
    return (
      <div className="page">
        <div className="eyebrow">VOICE LIBRARY</div>
        <h1>Voices</h1>
        <div className="empty">
          Connect to a GPU worker to manage voices. The library lives on the worker so a replacement server can be
          restored from a backup.
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="eyebrow">VOICE LIBRARY</div>
      <h1>Voices</h1>
      <p className="lede">
        A Seed-VC voice is a reference recording, so a new voice takes seconds and needs no training.
      </p>

      <section className="panel">
        <div className="panel-head">
          <span className="step">+</span>
          <div>
            <h2>New Seed-VC voice</h2>
            <p>Use 5–30 seconds of clean speech with no music or background noise.</p>
          </div>
        </div>
        <div className="grid two">
          <label>
            Name
            <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="Narrator" />
          </label>
          <label>
            Engine
            <select value={draft.engine} onChange={(event) => setDraft({ ...draft, engine: event.target.value })}>
              <option value="seed-vc">Seed-VC (zero-shot)</option>
            </select>
          </label>
        </div>
        <label>
          Description
          <input value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="Optional note" />
        </label>
        <label>
          Reference recording
          <div className="file-row">
            <input value={draft.referencePath} readOnly placeholder="No file selected" />
            <button className="secondary" onClick={chooseReference}>Choose file…</button>
          </div>
        </label>
        <div className="actions">
          <button className="primary" disabled={busy || !draft.name || !draft.referencePath} onClick={submit}>
            {busy ? "Saving…" : "Create voice"}
          </button>
        </div>
      </section>

      {error && (
        <div className="result bad">
          <span className="result-indicator" />
          <span>{error}</span>
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          <span className="step">{voices.length}</span>
          <div>
            <h2>Stored voices</h2>
            <p>Reference audio stays on the worker and is never committed to Git.</p>
          </div>
        </div>
        {voices.length === 0 ? (
          <div className="empty">No voices yet.</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Engine</th>
                <th>Reference</th>
                <th>Created</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {voices.map((voice) => (
                <tr key={voice.id}>
                  <td>
                    <strong>{voice.name}</strong>
                    {voice.description && <span className="muted"> · {voice.description}</span>}
                  </td>
                  <td>{engineLabel(voice.engine)}</td>
                  <td>{formatBytes(voice.size_bytes)}</td>
                  <td>{formatTimestamp(voice.created_at)}</td>
                  <td className="row-actions">
                    <button className="link" onClick={() => preview(voice)}>
                      {playing?.id === voice.id ? "Hide" : "Play"}
                    </button>
                    <button className="link danger" onClick={() => remove(voice)} disabled={busy}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {playing && (
        <div className="player">
          <audio key={playing.path} src={convertFileSrc(playing.path)} controls autoPlay />
        </div>
      )}
    </div>
  );
}
