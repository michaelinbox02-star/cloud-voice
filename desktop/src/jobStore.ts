import { useSyncExternalStore } from "react";
import type { Job } from "./api";
import { getJob } from "./api";

/**
 * Jobs live outside the page components.
 *
 * Each screen used to own its job in component state, so switching tabs threw
 * the job away and the in-flight training or conversion vanished from the UI.
 * A single store keeps polling regardless of which page is mounted, and the
 * last few jobs are persisted so a window restart re-attaches too.
 */

const STORAGE_KEY = "cloud-voice-jobs";
const POLL_MS = 2500;
const KEEP = 20;

type Listener = () => void;

const listeners = new Set<Listener>();
let jobs: Job[] = load();
let timer: number | null = null;

function load(): Job[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Job[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs.slice(0, KEEP)));
  } catch {
    // Storage being unavailable must not stop job tracking.
  }
}

function emit() {
  for (const listener of listeners) listener();
}

const isActive = (job: Job) => job.status === "queued" || job.status === "running";

function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  // A page mounting mid-job should see progress immediately, not after a tick.
  if (jobs.some(isActive)) startPolling();
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): Job[] {
  return jobs;
}

export function useJobs(): Job[] {
  return useSyncExternalStore(subscribe, snapshot);
}

export function latestJob(kinds: string[]): Job | undefined {
  return jobs.find((job) => kinds.includes(job.kind));
}

export function trackJob(job: Job) {
  jobs = [job, ...jobs.filter((entry) => entry.id !== job.id)].slice(0, KEEP);
  persist();
  emit();
  startPolling();
}

export function clearJob(jobId: string) {
  jobs = jobs.filter((entry) => entry.id !== jobId);
  persist();
  emit();
}

function startPolling() {
  if (timer !== null) return;
  timer = window.setInterval(poll, POLL_MS);
}

function stopPolling() {
  if (timer === null) return;
  window.clearInterval(timer);
  timer = null;
}

async function poll() {
  const active = jobs.filter(isActive);
  if (active.length === 0) {
    stopPolling();
    return;
  }
  const updates = await Promise.all(
    active.map(async (job) => {
      try {
        return await getJob(job.id);
      } catch {
        // Transient failures are expected while services restart; keep the job
        // and try again on the next tick instead of dropping it.
        return null;
      }
    }),
  );
  const resolved = updates.filter((job): job is Job => job !== null);
  if (resolved.length > 0) {
    const byId = new Map(resolved.map((job) => [job.id, job]));
    jobs = jobs.map((job) => byId.get(job.id) ?? job);
    persist();
    emit();
  }
}

// Resume polling on startup if the previous session left a job in flight.
if (jobs.some(isActive)) startPolling();
