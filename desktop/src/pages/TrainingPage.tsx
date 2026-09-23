import { useEffect, useRef, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import type { Job } from "../api";
import { createVoice, getJob, listVoices, registerWorkerVoice, startTraining } from "../api";
import { statusLabel } from "../format";

type Props = { online: boolean; onVoicesChanged: () => void };

export function TrainingPage({ online, onVoicesChanged }: Props) {
  const [name, setName] = useState("");
  const [datasetPath, setDatasetPath] = useState("");
  const [epochs, setEpochs] = useState(200);
  const [batchSize, setBatchSize] = useState(8);
  const [f0, setF0] = useState(true);
  const [sampleRate, setSampleRate] = useState("40k");
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  const chooseDataset = async () => {
    const selection = await open({ multiple: false, filters: [{ name: "Dataset archive", extensions: ["zip"] }] });
    if (typeof selection === "string") {
      setDatasetPath(selection);
      if (!name) {
        const stem = selection.split(/[\\/]/).pop()?.replace(/\.zip$/i, "") ?? "";
        setName(stem.replace(/[^A-Za-z0-9_-]/g, ""));
      }
    }
  };

  const start = async () => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const created = await startTraining(
        { name, voice_name: name, epochs, batch_size: batchSize, f0, sample_rate_option: sampleRate },
        datasetPath,
      );
      setJob(created);
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const latest = await getJob(created.id);
          setJob(latest);
          if (latest.status === "succeeded" || latest.status === "failed") {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            if (latest.status === "failed") setError(latest.error ?? "Training failed.");
          }
        } catch (problem) {
          setError(String(problem));
          if (pollRef.current) window.clearInterval(pollRef.current);
        }
      }, 4000);
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  const adopt = async () => {
    if (!job?.metrics?.model_path) return;
    setBusy(true);
    try {
      await registerWorkerVoice({
        name: name || "Trained voice",
        description: `Trained on the worker from ${job.metrics.experiment ?? name}`,
        model_path: String(job.metrics.model_path),
        index_path: job.metrics.index_path ? String(job.metrics.index_path) : undefined,
      });
      const refreshed = await listVoices();
      onVoicesChanged();
      setNotice(`Added ${refreshed.voices.length} voices to the library.`);
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  if (!online) {
    return (
      <div className="page">
        <div className="eyebrow">WORKER</div>
        <h1>Training</h1>
        <div className="empty">Connect to a GPU worker to train an RVC voice.</div>
      </div>
    );
  }

  const running = job?.status === "queued" || job?.status === "running";
  const finished = job?.status === "succeeded";

  return (
    <div className="page">
      <div className="eyebrow">WORKER</div>
      <h1>Training</h1>
      <p className="lede">
        RVC v2 fine-tuning runs entirely on the GPU worker: slicing, pitch tracking, feature extraction, training and
        the retrieval index.
      </p>

      <section className="panel">
        <div className="panel-head">
          <span className="step">1</span>
          <div>
            <h2>Dataset</h2>
            <p>Zip of clean speech, 5–30 minutes is ideal. Slicing happens on the worker.</p>
          </div>
        </div>
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="my-voice" />
        </label>
        <label>
          Dataset archive
          <div className="file-row">
            <input value={datasetPath} readOnly placeholder="No archive selected" />
            <button className="secondary" onClick={chooseDataset}>
              Choose…
            </button>
          </div>
        </label>
        <div className="grid two">
          <label>
            Epochs
            <input
              type="number"
              min={10}
              max={1200}
              value={epochs}
              onChange={(event) => setEpochs(Number(event.target.value))}
            />
          </label>
          <label>
            Batch size
            <input
              type="number"
              min={1}
              max={32}
              value={batchSize}
              onChange={(event) => setBatchSize(Number(event.target.value))}
            />
          </label>
          <label>
            Sample rate
            <select value={sampleRate} onChange={(event) => setSampleRate(event.target.value)}>
              <option value="32k">32 kHz</option>
              <option value="40k">40 kHz</option>
              <option value="48k">48 kHz</option>
            </select>
          </label>
          <label className="checkbox">
            <input type="checkbox" checked={f0} onChange={(event) => setF0(event.target.checked)} />
            <span>Pitch guidance (recommended for singing)</span>
          </label>
        </div>
        <div className="actions">
          <button className="primary" disabled={busy || running || !name || !datasetPath} onClick={() => void start()}>
            {running ? "Training…" : busy ? "Starting…" : "Start training"}
          </button>
        </div>
      </section>

      {job && (
        <section className="panel">
          <div className="panel-head">
            <span className="step">2</span>
            <div>
              <h2>Progress</h2>
              <p>Training keeps running on the worker even if you close this window.</p>
            </div>
          </div>
          <div className="job-status">
            <span className={`pill ${job.status}`}>{statusLabel[job.status] ?? job.status}</span>
            <span className="muted">{job.id}</span>
          </div>
          <div className="progress">
            <div style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
          </div>
          {finished && (
            <>
              <dl className="metrics">
                <div>
                  <dt>Model</dt>
                  <dd>{String(job.metrics?.model_path ?? "—").split("/").pop()}</dd>
                </div>
                <div>
                  <dt>Index</dt>
                  <dd>{String(job.metrics?.index_path ?? "—").split("/").pop()}</dd>
                </div>
              </dl>
              <div className="actions">
                <button className="secondary" disabled={busy} onClick={() => void adopt()}>
                  Add to voice library
                </button>
              </div>
            </>
          )}
        </section>
      )}

      {notice && (
        <div className="result good">
          <span className="result-indicator" />
          <span>{notice}</span>
        </div>
      )}
      {error && (
        <div className="result bad">
          <span className="result-indicator" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}
