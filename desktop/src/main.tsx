import React from "react";
import { createRoot } from "react-dom/client";
import type { ServerInput, SystemInfo } from "./api";
import { workerSystem } from "./api";
import { ServerPage } from "./pages/ServerPage";
import { VoicesPage } from "./pages/VoicesPage";
import { VoiceToVoicePage } from "./pages/VoiceToVoicePage";
import { RealtimePage } from "./pages/RealtimePage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import "./style.css";

type View = "realtime" | "voices" | "tts" | "voice-to-voice" | "training" | "server" | "settings";

const defaultServer: ServerInput = {
  host: "",
  port: 22,
  username: "",
  keyPath: "",
  repository: "",
};

const storageKey = "cloud-voice-server";

function loadServer(): ServerInput {
  try {
    return { ...defaultServer, ...JSON.parse(localStorage.getItem(storageKey) ?? "{}") };
  } catch {
    return defaultServer;
  }
}

function App() {
  const [view, setView] = React.useState<View>("server");
  const [server, setServerState] = React.useState<ServerInput>(loadServer);
  const [online, setOnline] = React.useState(false);
  const [system, setSystem] = React.useState<SystemInfo | null>(null);
  const [voiceCount, setVoiceCount] = React.useState(0);
  const [engineReady, setEngineReady] = React.useState(false);

  const setServer = (next: ServerInput) => {
    setServerState(next);
    localStorage.setItem(storageKey, JSON.stringify(next));
  };

  const refresh = React.useCallback(async () => {
    try {
      const info = await workerSystem();
      setSystem(info);
      setVoiceCount(info.voices);
      setEngineReady(info.engines?.["seed-vc"]?.models_loaded === true);
      setOnline(true);
    } catch {
      setOnline(false);
      setSystem(null);
    }
  }, []);

  React.useEffect(() => {
    if (!online) return;
    const timer = window.setInterval(refresh, 20000);
    return () => window.clearInterval(timer);
  }, [online, refresh]);

  const navigation: { group: string; items: { key: View; label: string; soon?: boolean }[] }[] = [
    {
      group: "Live",
      items: [{ key: "realtime", label: "Realtime" }],
    },
    {
      group: "Library",
      items: [{ key: "voices", label: "Voices" }],
    },
    {
      group: "Studio",
      items: [
        { key: "voice-to-voice", label: "Voice to Voice" },
        { key: "tts", label: "Text to Speech", soon: true },
      ],
    },
    {
      group: "Worker",
      items: [
        { key: "training", label: "Training", soon: true },
        { key: "server", label: "Server" },
        { key: "settings", label: "Settings", soon: true },
      ],
    },
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">C</span>
          <span>
            Cloud Voice <strong>Studio</strong>
          </span>
        </div>
        <nav>
          {navigation.map((section) => (
            <div className="nav-group" key={section.group}>
              <span className="nav-group-label">{section.group}</span>
              {section.items.map((item) => (
                <button
                  key={item.key}
                  className={`nav-item ${view === item.key ? "active" : ""}`}
                  onClick={() => setView(item.key)}
                >
                  <span>{item.label}</span>
                  {item.soon && <span className="chip">soon</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <span className={`status-dot ${online ? "good" : ""}`} />
          <div>
            <div>{online ? "Worker connected" : "Worker offline"}</div>
            <div className="muted small">
              {online
                ? engineReady
                  ? "Seed-VC ready"
                  : "Seed-VC idle"
                : server.host || "not configured"}
            </div>
          </div>
        </div>
      </aside>

      <main className="content">
        <header className="topbar">
          <span>{navigation.flatMap((section) => section.items).find((item) => item.key === view)?.label}</span>
          <span className="topbar-right">
            {online && system?.gpus[0] && <span className="muted">{system.gpus[0].name}</span>}
            <span className="version">{voiceCount} voices</span>
          </span>
        </header>

        {view === "server" && (
          <ServerPage
            server={server}
            setServer={setServer}
            online={online}
            system={system}
            onConnected={refresh}
            onDisconnected={() => {
              setOnline(false);
              setSystem(null);
            }}
          />
        )}
        {view === "voices" && <VoicesPage online={online} onCountChange={setVoiceCount} />}
        {view === "voice-to-voice" && <VoiceToVoicePage online={online} />}
        {view === "realtime" && <RealtimePage online={online} />}
        {view === "tts" && (
          <PlaceholderPage
            eyebrow="STUDIO"
            title="Text to speech"
            summary="Text becomes speech with Kokoro, then passes through the selected voice engine."
            missing={["Kokoro engine container", "Job type for TTS generation", "Preview and export controls"]}
          />
        )}
        {view === "training" && (
          <PlaceholderPage
            eyebrow="WORKER"
            title="Training"
            summary="Remote RVC v2 training with dataset preprocessing on the GPU worker."
            missing={[
              "RVC inference engine container",
              "Dataset upload, slicing and preprocessing pipeline",
              "Training job orchestration with checkpoint export",
            ]}
          />
        )}
        {view === "settings" && (
          <PlaceholderPage
            eyebrow="WORKER"
            title="Settings"
            summary="Audio devices, latency presets and backup management."
            missing={[
              "Input and output device selection with virtual cable detection",
              "Latency presets derived from measured worker benchmarks",
              "Voice library backup and restore",
            ]}
          />
        )}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
