import { useEffect, useRef, useState } from "react";
import { save } from "@tauri-apps/plugin-dialog";
import { convertFileSrc } from "@tauri-apps/api/core";
import type { Job, Voice } from "../api";
import { KOKORO_VOICES, getJob, listVoices, startTts, workerDownload, workerFetchArtifact } from "../api";
import { Waveform } from "../components/Waveform";
import { engineLabel, formatSeconds, statusLabel } from "../format";

type Props = { online: boolean };

export function TtsPage({ online }: Props) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [text, setText] = useState("");
  const [ttsVoice, setTtsVoice] = useState("af_heart");
  const [speed, setSpeed] = useState(1);
  const [targetVoice, setTargetVoice] = useState("");
  const [format, setFormat] = useState<"wav" | "mp3" | "flac">("wav");
  const [job, setJob] = useState<Job | null>(null);
  const [artifact, setArtifact] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (!online) {
      setVoices([]);
      return;
    }
    listVoices()
      .then((result) => setVoices(result.voices))
      .catch((problem) => setError(String(problem)));
  }, [online]);

  useEffect(() => () => {
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  const generate = async () => {
    setBusy(true);
    setError("");
    setArtifact(null);
    setJob(null);
    try {
      const created = await startTts({
        text,
        voice_id: targetVoice || null,
        tts_voice: ttsVoice,
        speed,
        output_format: format,
      });
      setJob(created);
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const latest = await getJob(created.id);
          setJob(latest);
          if (latest.status === "succeeded" || latest.status === "failed") {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            if (latest.status === "succeeded") {
              const extension = String(latest.metrics?.output_format ?? "wav");
              setArtifact(await workerFetchArtifact(`/v1/jobs/${latest.id}/audio`, `${latest.id}.${extension}`));
            } else {
              setError(latest.error ?? "Synthesis failed.");
            }
          }
        } catch (problem) {
          setError(String(problem));
          if (pollRef.current) window.clearInterval(pollRef.current);
        }
      }, 1500);
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(false);
    }
  };

  const saveCopy = async () => {
    if (!job) return;
    const extension = String(job.metrics?.output_format ?? "wav");
    const destination = await save({
      defaultPath: `speech-${job.id.slice(-6)}.${extension}`,
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
        <h1>Text to speech</h1>
        <div className="empty">Connect to a GPU worker to synthesise speech.</div>
      </div>
    );
  }

  const running = job?.status === "queued" || job?.status === "running";

  return (
    <div className="page">
      <div className="eyebrow">STUDIO</div>
      <h1>Text to speech</h1>
      <p className="lede">
        Kokoro reads the text, then optionally passes the result through one of your voices so it sounds like them.
      </p>

      <div className="columns">
        <section className="panel">
          <div className="panel-head">
            <span className="step">1</span>
            <div>
              <h2>Script</h2>
              <p>{text.trim().length} characters · up to 5000</p>
            </div>
          </div>
          <label>
            Text
            <textarea
              className="script"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="Type or paste the words to speak."
              rows={9}
            />
          </label>
          <div className="inline-row">
            <label>
              Reader voice
              <select value={ttsVoice} onChange={(event) => setTtsVoice(event.target.value)}>
                {KOKORO_VOICES.map((voice) => (
                  <option key={voice.id} value={voice.id}>
                    {voice.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Speed
              <input
                type="number"
                min={0.5}
                max={2}
                step={0.05}
                value={speed}
                onChange={(event) => setSpeed(Number(event.target.value))}
              />
            </label>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="step">2</span>
            <div>
              <h2>Voice</h2>
              <p>Skip this to keep the plain Kokoro reader.</p>
            </div>
          </div>
          <label>
            Convert through
            <select value={targetVoice} onChange={(event) => setTargetVoice(event.target.value)}>
              <option value="">None · base speech</option>
              {voices.map((voice) => (
                <option key={voice.id} value={voice.id}>
                  {voice.name} · {engineLabel(voice.engine)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Export format
            <select value={format} onChange={(event) => setFormat(event.target.value as typeof format)}>
              <option value="wav">WAV</option>
              <option value="mp3">MP3</option>
              <option value="flac">FLAC</option>
            </select>
          </label>
          <div className="actions">
            <button className="primary" disabled={busy || running || !text.trim()} onClick={() => void generate()}>
              {running ? "Generating…" : busy ? "Starting…" : "Generate speech"}
            </button>
          </div>

          {job && (
            <>
              <div className="job-status">
                <span className={`pill ${job.status}`}>{statusLabel[job.status] ?? job.status}</span>
                <span className="muted">
                  {job.metrics?.characters ? `${job.metrics.characters} chars · ` : ""}
                  {formatSeconds(job.metrics?.audio_seconds)}
                </span>
              </div>
              <div className="progress">
                <div style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
              </div>
            </>
          )}

          {artifact && (
            <>
              <Waveform path={artifact} />
              <audio className="inline-audio" src={convertFileSrc(artifact)} controls autoPlay />
              <div className="actions">
                <button className="secondary" onClick={() => void saveCopy()}>
                  Save a copy…
                </button>
              </div>
            </>
          )}

          {error && (
            <div className="result bad">
              <span className="result-indicator" />
              <span>{error}</span>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
