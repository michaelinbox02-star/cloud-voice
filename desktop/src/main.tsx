import React from "react";
import { createRoot } from "react-dom/client";
import type { ServerInput, SystemInfo } from "./api";
import { workerSystem } from "./api";
import { ServerPage } from "./pages/ServerPage";
import { VoicesPage } from "./pages/VoicesPage";
import { VoiceToVoicePage } from "./pages/VoiceToVoicePage";
import { RealtimePage } from "./pages/RealtimePage";
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { TtsPage } from "./pages/TtsPage";
import { TrainingPage } from "./pages/TrainingPage";
import { SettingsPage } from "./pages/SettingsPage";
import { setJobScope } from "./jobStore";
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
  const [degraded, setDegraded] = React.useState(false);
  const failures = React.useRef(0);

  const setServer = (next: ServerInput) => {
    setServerState(next);
    localStorage.setItem(storageKey, JSON.stringify(next));
  };

  const refresh = React.useCallback(async () => {
    try {
      const info = await workerSystem();
      failures.current = 0;
      setDegraded(false);
      setSystem(info);
      setVoiceCount(info.voices);
      setEngineReady(info.engines?.["seed-vc"]?.status === "ready");
      setOnline(true);
    } catch {
      // Services restart during an install and a long build restarts every
      // container, so a single failed poll is expected. Only give up after
      // several in a row, and never discard the last known status.
      failures.current += 1;
      if (failures.current >= 3) {
        setOnline(false);
        setSystem(null);
        setDegraded(false);
      } else {
        setDegraded(true);
      }
    }
  }, []);

  React.useEffect(() => {
    if (!online) return;
    const timer = window.setInterval(refresh, 30000);
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
        { key: "tts", label: "Text to Speech" },
      ],
    },
    {
      group: "Worker",
      items: [
        { key: "training", label: "Training" },
        { key: "server", label: "Server" },
        { key: "settings", label: "Settings" },
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
          <span className={`status-dot ${online ? (degraded ? "warn" : "good") : ""}`} />
          <div>
            <div>{online ? (degraded ? "Worker busy" : "Worker connected") : "Worker offline"}</div>
            <div className="muted small">
              {online
                ? degraded
                  ? "retrying…"
                  : engineReady
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
            onConnected={(connectedServer) => {
              setJobScope(`${connectedServer.username}@${connectedServer.host}:${connectedServer.port}`);
              void refresh();
            }}
            onDisconnected={() => {
              setJobScope(null);
              setOnline(false);
              setSystem(null);
            }}
          />
        )}
        {view === "voices" && <VoicesPage online={online} onCountChange={setVoiceCount} />}
        {view === "voice-to-voice" && <VoiceToVoicePage online={online} />}
        {view === "realtime" && <RealtimePage online={online} />}
        {view === "tts" && <TtsPage online={online} />}
        {view === "training" && <TrainingPage online={online} onVoicesChanged={refresh} />}
        {view === "settings" && <SettingsPage online={online} system={system} onRestored={refresh} />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
