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

const STORAGE_PREFIX = "cloud-voice-jobs:";
const POLL_MS = 2500;
const KEEP = 20;

type Listener = () => void;

const listeners = new Set<Listener>();
let storageKey: string | null = null;
let jobs: Job[] = [];
let timer: number | null = null;

function load(key: string): Job[] {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? (JSON.parse(raw) as Job[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persist() {
  if (storageKey === null) return;
  try {
    localStorage.setItem(storageKey, JSON.stringify(jobs.slice(0, KEEP)));
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

export function setJobScope(scope: string | null) {
  const nextKey = scope ? `${STORAGE_PREFIX}${scope}` : null;
  if (nextKey === storageKey) return;
  stopPolling();
  storageKey = nextKey;
  jobs = nextKey ? load(nextKey) : [];
  emit();
  if (jobs.some(isActive)) startPolling();
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
      } catch (error) {
        if (String(error).includes("HTTP 404")) {
          return { ...job, status: "failed" as const, error: "This job no longer exists on the connected worker." };
        }
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
