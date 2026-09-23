import { invoke } from "@tauri-apps/api/core";

export type ServerInput = {
  host: string;
  port: number;
  username: string;
  keyPath: string;
  repository: string;
};

export type ServerResult = { log: string; release: string | null };

export type Gpu = {
  name: string;
  driver: string;
  memory_total_mib: string;
  memory_used_mib: string;
};

export type Disk = { total_gib: number; used_gib: number; free_gib: number };

export type EngineHealth = {
  status: string;
  models_loaded?: boolean;
  device?: string;
  detail?: string;
  model_load_seconds?: number | null;
};

export type SystemInfo = {
  gpus: Gpu[];
  disk: Disk;
  engines: Record<string, EngineHealth>;
  voices: number;
};

export type Voice = {
  id: string;
  name: string;
  engine: string;
  description: string | null;
  language: string | null;
  reference_audio: string | null;
  settings: Record<string, unknown>;
  size_bytes: number;
  created_at: string;
};

export type JobMetrics = {
  inference_seconds?: number;
  audio_seconds?: number;
  realtime_factor?: number;
  peak_vram_mib?: number;
  sample_rate?: number;
  output_format?: string;
  // Training and synthesis report their own extras.
  synthesis_seconds?: number;
  characters?: number;
  model_path?: string | null;
  index_path?: string | null;
  experiment?: string;
};

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export type Job = {
  id: string;
  kind: string;
  status: JobStatus;
  engine: string;
  voice_id: string | null;
  params: Record<string, unknown>;
  output_path: string | null;
  error: string | null;
  progress: number;
  metrics: JobMetrics;
  created_at: string;
  finished_at: string | null;
};

export type VoiceDraft = {
  name: string;
  engine: string;
  description?: string;
  language?: string;
  settings?: string;
  referencePath?: string;
};

export type ConversionDraft = {
  voiceId: string;
  engine: string;
  sourcePath: string;
  params?: string;
};

export const probeServer = (input: ServerInput) => invoke<ServerResult>("probe_server", { input });
export const deployServer = (input: ServerInput) => invoke<ServerResult>("deploy_server", { input });
export const connectWorker = (input: ServerInput) => invoke<SystemInfo>("connect_worker", { input });
export const disconnectWorker = () => invoke<void>("disconnect_worker");
export const workerSystem = () => invoke<SystemInfo>("worker_system");
export const listVoices = () => invoke<{ voices: Voice[] }>("list_voices");
export const createVoice = (draft: VoiceDraft) => invoke<Voice>("create_voice", { draft });
export const deleteVoice = (voiceId: string) => invoke<{ deleted: string }>("delete_voice", { voiceId });
export const listJobs = (limit = 25) => invoke<{ jobs: Job[] }>("list_jobs", { limit });
export const getJob = (jobId: string) => invoke<Job>("get_job", { jobId });
export const startConversion = (draft: ConversionDraft) => invoke<Job>("start_conversion", { draft });
export const workerDownload = (path: string, destinationPath: string) =>
  invoke<string>("worker_download", { path, destinationPath });
export const workerFetchArtifact = (path: string, fileName: string) =>
  invoke<string>("worker_fetch_artifact", { path, fileName });
export const stagePreview = (sourcePath: string) => invoke<string>("stage_preview", { sourcePath });
export const revealPath = (path: string) => invoke<void>("reveal_path", { path });

export type RealtimeTicket = {
  session_id: string;
  token: string;
  expires_at: number;
  voice_id: string;
  voice_name: string;
  preset: string;
};

export type RealtimeAnswer = {
  sdp: string;
  type: RTCSdpType;
  model_rate: number;
  block_seconds: number;
  output_sample_rate: number;
};

export type RealtimeStats = {
  state: string;
  blocks: number;
  inference_ms_mean: number | null;
  inference_ms_p95: number | null;
  queued_frames: number;
  dropped_frames: number;
  uptime_seconds: number;
  connection_state: string | null;
};

export type RealtimePreset = "low-latency" | "balanced" | "quality";

export const realtimeBegin = (
  voiceId: string,
  preset: RealtimePreset,
  diffusionSteps?: number,
) => invoke<RealtimeTicket>("realtime_begin", { draft: { voiceId, preset, diffusionSteps } });

export const realtimeOffer = (sessionId: string, token: string, sdp: string, kind: string) =>
  invoke<RealtimeAnswer>("realtime_offer", { sessionId, token, sdp, kind });

export const realtimeStats = (sessionId: string) =>
  invoke<RealtimeStats>("realtime_stats", { sessionId });

export const realtimeEnd = (sessionId: string, voiceId: string) =>
  invoke<void>("realtime_end", { sessionId, voiceId });

export const realtimeHealth = () => invoke<Record<string, unknown>>("realtime_health");

export type TtsDraft = {
  text: string;
  voice_id?: string | null;
  tts_voice?: string;
  lang_code?: string;
  speed?: number;
  output_format?: string;
};

export const startTts = (draft: TtsDraft) => invoke<Job>("start_tts", { draft });

export type TrainingDraft = {
  name: string;
  voice_name?: string;
  epochs?: number;
  batch_size?: number;
  f0?: boolean;
  sample_rate_option?: string;
};

export const startTraining = (draft: TrainingDraft, datasetPath: string) =>
  invoke<Job>("start_training", { draft, datasetPath });

export const createRvcVoice = (draft: VoiceDraft, modelPath: string, indexPath?: string) =>
  invoke<Voice>("create_rvc_voice", { draft, modelPath, indexPath });

export const registerWorkerVoice = (draft: {
  name: string;
  description?: string;
  model_path: string;
  index_path?: string;
}) => invoke<Voice>("register_worker_voice", { draft });

export const downloadBackup = (destinationPath: string) =>
  invoke<string>("download_backup", { destinationPath });

export const restoreBackup = (archivePath: string) =>
  invoke<{ restored_voices: number; voices: number }>("restore_backup", { archivePath });

export const KOKORO_VOICES = [
  { id: "af_heart", label: "Heart · US female" },
  { id: "af_bella", label: "Bella · US female" },
  { id: "af_nicole", label: "Nicole · US female" },
  { id: "af_sarah", label: "Sarah · US female" },
  { id: "am_adam", label: "Adam · US male" },
  { id: "am_michael", label: "Michael · US male" },
  { id: "bf_emma", label: "Emma · UK female" },
  { id: "bm_george", label: "George · UK male" },
];
