import React from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@tauri-apps/api/core";
import "./style.css";

type ServerInput = {
  host: string;
  port: number;
  username: string;
  keyPath: string;
  repository: string;
};

type ServerResult = { log: string; release: string | null };

const initial: ServerInput = {
  host: "",
  port: 22,
  username: "",
  keyPath: "",
  repository: "",
};

function App() {
  const [server, setServer] = React.useState<ServerInput>(() => {
    try {
      return { ...initial, ...JSON.parse(localStorage.getItem("cloud-voice-server") || "{}") };
    } catch {
      return initial;
    }
  });
  const [busy, setBusy] = React.useState<"probe" | "deploy" | null>(null);
  const [status, setStatus] = React.useState<"idle" | "verified" | "deployed" | "error">("idle");
  const [message, setMessage] = React.useState("Enter your GPU server details to begin.");
  const [log, setLog] = React.useState("");

  const update = (key: keyof ServerInput, value: string) => {
    setServer((current) => ({ ...current, [key]: key === "port" ? Number(value) : value }));
  };

  const run = async (action: "probe" | "deploy") => {
    setBusy(action);
    setMessage(action === "probe" ? "Checking the GPU server…" : "Installing the worker…");
    setLog("");
    try {
      const result = await invoke<ServerResult>(action === "probe" ? "probe_server" : "deploy_server", { input: server });
      localStorage.setItem("cloud-voice-server", JSON.stringify(server));
      setLog(result.log);
      setStatus(action === "probe" ? "verified" : "deployed");
      setMessage(action === "probe" ? "Server prerequisites passed." : "Worker installed and API credential saved to Windows Credential Manager.");
    } catch (error) {
      setStatus("error");
      setMessage(String(error));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">C</span><span>Cloud Voice <strong>Studio</strong></span></div>
        <div className="sidebar-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          <span className="nav-item inactive">Realtime</span>
          <span className="nav-item inactive">Voices</span>
          <span className="nav-item inactive">Studio</span>
          <span className="nav-item inactive">Training</span>
          <span className="nav-item active">Server</span>
          <span className="nav-item inactive">Settings</span>
        </nav>
        <div className="sidebar-bottom"><span className="status-dot" /> GPU worker setup</div>
      </aside>
      <main className="content">
        <header className="topbar"><span>Server</span><span className="version">v0.1.0</span></header>
        <div className="page">
          <div className="eyebrow">REMOTE COMPUTE</div>
          <h1>Connect a GPU worker</h1>
          <p className="intro">The worker runs all voice models on your NVIDIA server. This computer handles audio devices and controls.</p>
          <section className="panel" aria-label="GPU server connection">
            <div className="section-heading"><span className="step">01</span><div><h2>SSH connection</h2><p>Use an account with passwordless sudo on Ubuntu 24.04.</p></div></div>
            <div className="grid two">
              <label>Host<input value={server.host} onChange={(event) => update("host", event.target.value)} placeholder="gpu.example.com" autoComplete="off" /></label>
              <label>Port<input type="number" min="1" max="65535" value={server.port} onChange={(event) => update("port", event.target.value)} /></label>
              <label>Username<input value={server.username} onChange={(event) => update("username", event.target.value)} placeholder="ubuntu" autoComplete="off" /></label>
              <label>Private key path<input value={server.keyPath} onChange={(event) => update("keyPath", event.target.value)} placeholder="C:\\Users\\you\\.ssh\\gpu-key" autoComplete="off" /></label>
            </div>
            <div className="actions"><button className="secondary" disabled={busy !== null} onClick={() => run("probe")}>{busy === "probe" ? "Checking…" : "Test server"}</button></div>
          </section>
          <section className="panel" aria-label="Worker deployment">
            <div className="section-heading"><span className="step">02</span><div><h2>Install worker</h2><p>Clone the repository, validate Docker and CUDA, then start the service.</p></div></div>
            <label>GitHub repository<input value={server.repository} onChange={(event) => update("repository", event.target.value)} placeholder="https://github.com/owner/repository" autoComplete="off" /></label>
            <div className="actions"><button className="primary" disabled={busy !== null || status === "idle"} onClick={() => run("deploy")}>{busy === "deploy" ? "Installing…" : "Install on GPU"}</button></div>
          </section>
          <div className={`result ${status}`} role="status"><span className="result-indicator" /><span>{message}</span></div>
          {log && <details className="log"><summary>Technical log</summary><pre>{log}</pre></details>}
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
