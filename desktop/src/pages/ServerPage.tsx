import { useState } from "react";
import type { ServerInput, SystemInfo } from "../api";
import { connectWorker, deployServer, disconnectWorker, probeServer } from "../api";

type Props = {
  server: ServerInput;
  setServer: (server: ServerInput) => void;
  online: boolean;
  system: SystemInfo | null;
  onConnected: () => void;
  onDisconnected: () => void;
};

type Status = { tone: "neutral" | "good" | "bad"; message: string };

export function ServerPage({ server, setServer, online, system, onConnected, onDisconnected }: Props) {
  const [busy, setBusy] = useState<"probe" | "deploy" | "connect" | null>(null);
  const [status, setStatus] = useState<Status>({
    tone: "neutral",
    message: "Enter your GPU server details, then test the connection.",
  });
  const [log, setLog] = useState("");
  const [probed, setProbed] = useState(false);

  const update = (key: keyof ServerInput, value: string) => {
    setServer({ ...server, [key]: key === "port" ? Number(value) : value });
  };

  const run = async (action: "probe" | "deploy" | "connect") => {
    setBusy(action);
    setLog("");
    setStatus({
      tone: "neutral",
      message:
        action === "probe"
          ? "Inspecting the server…"
          : action === "deploy"
            ? "Installing the worker. First run downloads several gigabytes of models."
            : "Opening an encrypted tunnel to the worker…",
    });
    try {
      if (action === "probe") {
        const result = await probeServer(server);
        setLog(result.log);
        setProbed(true);
        setStatus({ tone: "good", message: "Server prerequisites look good." });
      } else if (action === "deploy") {
        const result = await deployServer(server);
        setLog(result.log);
        setStatus({
          tone: "good",
          message: `Worker installed at revision ${result.release?.slice(0, 8) ?? "unknown"} and its credential was saved to Windows Credential Manager.`,
        });
      } else {
        const info = await connectWorker(server);
        onConnected();
        setStatus({
          tone: "good",
          message: `Connected. ${info.gpus.length} GPU available.`,
        });
      }
    } catch (error) {
      setStatus({ tone: "bad", message: String(error) });
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async () => {
    setBusy("connect");
    try {
      await disconnectWorker();
      onDisconnected();
      setStatus({ tone: "neutral", message: "Disconnected from the worker." });
    } catch (error) {
      setStatus({ tone: "bad", message: String(error) });
    } finally {
      setBusy(null);
    }
  };

  const seed = system?.engines?.["seed-vc"];

  return (
    <div className="page">
      <div className="eyebrow">REMOTE COMPUTE</div>
      <h1>GPU worker</h1>
      <p className="lede">
        All models run on your rented NVIDIA server. This computer only handles audio devices, transport and the
        interface.
      </p>

      <section className="panel">
        <div className="panel-head">
          <span className="step">01</span>
          <div>
            <h2>SSH connection</h2>
            <p>Ubuntu 24.04 with an NVIDIA driver and an account that has passwordless sudo.</p>
          </div>
          {online && <span className="pill good">Connected</span>}
        </div>
        <div className="grid two">
          <label>
            Host
            <input value={server.host} onChange={(event) => update("host", event.target.value)} placeholder="gpu.example.com" spellCheck={false} />
          </label>
          <label>
            Port
            <input type="number" min={1} max={65535} value={server.port} onChange={(event) => update("port", event.target.value)} />
          </label>
          <label>
            Username
            <input value={server.username} onChange={(event) => update("username", event.target.value)} placeholder="ubuntu" spellCheck={false} />
          </label>
          <label>
            Private key path
            <input value={server.keyPath} onChange={(event) => update("keyPath", event.target.value)} placeholder="C:\Users\you\.ssh\gpu-key" spellCheck={false} />
          </label>
        </div>
        <div className="actions">
          <button className="secondary" disabled={busy !== null} onClick={() => run("probe")}>
            {busy === "probe" ? "Testing…" : "Test server"}
          </button>
          {online ? (
            <button className="secondary" disabled={busy !== null} onClick={disconnect}>
              Disconnect
            </button>
          ) : (
            <button className="primary" disabled={busy !== null} onClick={() => run("connect")}>
              {busy === "connect" ? "Connecting…" : "Connect"}
            </button>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="step">02</span>
          <div>
            <h2>Install worker</h2>
            <p>Clones a pinned revision, validates CUDA, starts the services and stores the API credential.</p>
          </div>
        </div>
        <label>
          GitHub repository
          <input value={server.repository} onChange={(event) => update("repository", event.target.value)} placeholder="https://github.com/owner/repository" spellCheck={false} />
        </label>
        <div className="actions">
          <button className="primary" disabled={busy !== null || !probed} onClick={() => run("deploy")} title={probed ? undefined : "Test the server first"}>
            {busy === "deploy" ? "Installing…" : "Install on GPU"}
          </button>
        </div>
      </section>

      <div className={`result ${status.tone}`}>
        <span className="result-indicator" />
        <span>{status.message}</span>
      </div>

      {system && (
        <section className="panel">
          <div className="panel-head">
            <span className="step">03</span>
            <div>
              <h2>Worker status</h2>
              <p>Live view of the hardware and each engine container.</p>
            </div>
          </div>
          <div className="stat-grid">
            {system.gpus.map((gpu) => (
              <div className="stat" key={gpu.name}>
                <span className="stat-label">GPU</span>
                <span className="stat-value">{gpu.name}</span>
                <span className="stat-note">
                  driver {gpu.driver} · {gpu.memory_used_mib} / {gpu.memory_total_mib} MiB
                </span>
              </div>
            ))}
            <div className="stat">
              <span className="stat-label">Disk</span>
              <span className="stat-value">{system.disk.free_gib} GiB free</span>
              <span className="stat-note">{system.disk.total_gib} GiB total</span>
            </div>
            <div className="stat">
              <span className="stat-label">Seed-VC</span>
              <span className="stat-value">
                {seed?.status === "ready" ? "Models loaded" : seed?.status === "cold" ? "Idle" : seed?.status ?? "Unknown"}
              </span>
              <span className="stat-note">{seed?.device ?? seed?.detail ?? "not reported"}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Voices</span>
              <span className="stat-value">{system.voices}</span>
              <span className="stat-note">stored on the worker</span>
            </div>
          </div>
        </section>
      )}

      {log && (
        <details className="log">
          <summary>Technical log</summary>
          <pre>{log}</pre>
        </details>
      )}
    </div>
  );
}
