import { useEffect, useRef, useState } from "react";
import { open, save } from "@tauri-apps/plugin-dialog";
import { convertFileSrc } from "@tauri-apps/api/core";
import type { Job, Voice } from "../api";
import { listVoices, stagePreview, startConversion, workerDownload, workerFetchArtifact } from "../api";
import { trackJob, useJobs } from "../jobStore";
import { Waveform } from "../components/Waveform";
import { engineLabel, formatSeconds, statusLabel } from "../format";

type Props = { online: boolean };

type Settings = {
  diffusion_steps: number;
  similarity_cfg_rate: number;
  intelligibility_cfg_rate: number;
  length_adjust: number;
  output_format: "wav" | "flac" | "mp3";
};

const defaults: Settings = {
  diffusion_steps: 30,
  similarity_cfg_rate: 0.7,
  intelligibility_cfg_rate: 0.7,
  length_adjust: 1.0,
  output_format: "wav",
};

export function VoiceToVoicePage({ online }: Props) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [settings, setSettings] = useState<Settings>(defaults);
  const [advanced, setAdvanced] = useState(false);
  const [artifact, setArtifact] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const artifacts = useRef<Record<string, string>>({});

  useEffect(() => {
    if (!online) {
      setVoices([]);
      return;
    }
    listVoices()
      .then((result) => {
        setVoices(result.voices.filter((voice) => voice.engine === "seed-vc"));
        if (result.voices.length > 0) setVoiceId((current) => current || result.voices[0].id);
      })
      .catch((problem) => setError(String(problem)));
  }, [online]);

  const jobs = useJobs();
  const job = jobs.find((entry) => entry.kind === "convert");

  // Fetch the finished audio once per job, and again after a tab switch.
  useEffect(() => {
    if (!job || job.status !== "succeeded") {
      setArtifact(null);
      return;
    }
    const cached = artifacts.current[job.id];
    if (cached) {
      setArtifact(cached);
      return;
    }
    const extension = String(job.metrics?.output_format ?? "wav");
    let cancelled = false;
    workerFetchArtifact(`/v1/jobs/${job.id}/audio`, `${job.id}.${extension}`)
      .then((path) => {
        if (cancelled) return;
        artifacts.current[job.id] = path;
        setArtifact(path);
      })
      .catch((problem) => {
        if (!cancelled) setError(String(problem));
      });
    return () => {
      cancelled = true;
    };
  }, [job?.id, job?.status]);

  const chooseSource = async () => {
    const selection = await open({
      multiple: false,
      filters: [{ name: "Audio", extensions: ["wav", "flac", "mp3", "m4a", "ogg"] }],
    });
    if (typeof selection !== "string") return;
    setSourcePath(selection);
    setArtifact(null);
    setError("");
    try {
      setPreviewPath(await stagePreview(selection));
    } catch {
      setPreviewPath(null);
    }
  };

  const convert = async () => {
    if (!voiceId || !sourcePath) return;
    setBusy(true);
    setError("");
    setArtifact(null);
    try {
      const voice = voices.find((entry) => entry.id === voiceId);
      const created = await startConversion({
        voiceId,
        engine: voice?.engine ?? "seed-vc",
        sourcePath,
        params: JSON.stringify(settings),
      });
      trackJob(created);
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  const saveArtifact = async () => {
    if (!job) return;
    const extension = String(job.metrics?.output_format ?? "wav");
    const destination = await save({
      defaultPath: `converted-${job.id.slice(-6)}.${extension}`,
      filters: [{ name: extension.toUpperCase(), extensions: [extension] }],
    });
    if (typeof destination !== "string") return;
    try {
      await workerDownload(`/v1/jobs/${job.id}/audio`, destination);
      setError("");
    } catch (problem) {
      setError(String(problem));
    }
  };

  if (!online) {
    return (
      <div className="page">
        <div className="eyebrow">STUDIO</div>
        <h1>Voice to voice</h1>
        <div className="empty">Connect to a GPU worker to convert audio files.</div>
      </div>
    );
  }

  const running = job?.status === "queued" || job?.status === "running";

  return (
    <div className="page">
      <div className="eyebrow">STUDIO</div>
      <h1>Voice to voice</h1>
      <p className="lede">
        Upload a recording, convert it into a stored voice, then preview and export the result.
      </p>

      <div className="columns">
        <section className="panel">
          <div className="panel-head">
            <span className="step">1</span>
            <div>
              <h2>Source</h2>
              <p>WAV, FLAC, MP3, M4A or OGG.</p>
            </div>
          </div>
          <div className="file-row">
            <input value={sourcePath} readOnly placeholder="No file selected" />
            <button className="secondary" onClick={chooseSource}>Choose…</button>
          </div>
          <Waveform path={previewPath} />
          {previewPath && <audio className="inline-audio" src={convertFileSrc(previewPath)} controls />}

          <div className="panel-head spaced">
            <span className="step">2</span>
            <div>
              <h2>Target voice</h2>
              <p>{voices.length === 0 ? "Create a voice first." : `${voices.length} Seed-VC voice${voices.length === 1 ? "" : "s"} available.`}</p>
            </div>
          </div>
          <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)} disabled={voices.length === 0}>
            {voices.length === 0 && <option value="">No voices</option>}
            {voices.map((voice) => (
              <option key={voice.id} value={voice.id}>
                {voice.name} · {engineLabel(voice.engine)}
              </option>
            ))}
          </select>
          <div className="inline-row">
            <label>
              Export format
              <select
                value={settings.output_format}
                onChange={(event) => setSettings({ ...settings, output_format: event.target.value as Settings["output_format"] })}
              >
                <option value="wav">WAV</option>
                <option value="flac">FLAC</option>
                <option value="mp3">MP3</option>
              </select>
            </label>
          </div>

          <button className="disclosure" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}>
            {advanced ? "Hide" : "Show"} advanced parameters
          </button>
          {advanced && (
            <div className="advanced">
              <Slider
                label="Diffusion steps"
                hint="Quality against speed"
                min={4}
                max={60}
                step={1}
                value={settings.diffusion_steps}
                onChange={(value) => setSettings({ ...settings, diffusion_steps: value })}
              />
              <Slider
                label="Similarity"
                hint="How closely the result tracks the target voice"
                min={0}
                max={1.5}
                step={0.05}
                value={settings.similarity_cfg_rate}
                onChange={(value) => setSettings({ ...settings, similarity_cfg_rate: value })}
              />
              <Slider
                label="Intelligibility"
                hint="How closely the result tracks the source words"
                min={0}
                max={1.5}
                step={0.05}
                value={settings.intelligibility_cfg_rate}
                onChange={(value) => setSettings({ ...settings, intelligibility_cfg_rate: value })}
              />
              <Slider
                label="Length adjust"
                hint="Time-stretch factor"
                min={0.5}
                max={2}
                step={0.01}
                value={settings.length_adjust}
                onChange={(value) => setSettings({ ...settings, length_adjust: value })}
              />
            </div>
          )}

          <div className="actions">
            <button className="primary" disabled={busy || running || !voiceId || !sourcePath} onClick={convert}>
              {running ? "Converting…" : busy ? "Starting…" : "Convert"}
            </button>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="step">3</span>
            <div>
              <h2>Result</h2>
              <p>A/B the original against the converted take.</p>
            </div>
          </div>

          {error && (
            <div className="result bad">
              <span className="result-indicator" />
              <span>{error}</span>
            </div>
          )}

          {job && (
            <>
              <div className="job-status">
                <span className={`pill ${job.status}`}>{statusLabel[job.status] ?? job.status}</span>
                <span className="muted">{job.id}</span>
              </div>
              <div className="progress">
                <div style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
              </div>
              <dl className="metrics">
                <div>
                  <dt>Inference</dt>
                  <dd>{formatSeconds(job.metrics?.inference_seconds)}</dd>
                </div>
                <div>
                  <dt>Audio</dt>
                  <dd>{formatSeconds(job.metrics?.audio_seconds)}</dd>
                </div>
                <div>
                  <dt>Real-time factor</dt>
                  <dd>{job.metrics?.realtime_factor ?? "—"}</dd>
                </div>
                <div>
                  <dt>Peak VRAM</dt>
                  <dd>{job.metrics?.peak_vram_mib ? `${job.metrics.peak_vram_mib} MiB` : "—"}</dd>
                </div>
              </dl>
            </>
          )}

          {!job && <div className="empty">Run a conversion to see timing and output here.</div>}

          {artifact && (
            <>
              <Waveform path={artifact} />
              <audio className="inline-audio" src={convertFileSrc(artifact)} controls />
              <div className="actions">
                <button className="secondary" onClick={saveArtifact}>Save a copy…</button>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

type SliderProps = {
  label: string;
  hint: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: number) => void;
};

function Slider({ label, hint, min, max, step, value, onChange }: SliderProps) {
  return (
    <label className="slider">
      <span className="slider-head">
        <span>{label}</span>
        <span className="slider-value">{value}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <span className="slider-hint">{hint}</span>
    </label>
  );
}
