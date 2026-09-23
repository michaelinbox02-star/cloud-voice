import { useEffect, useMemo, useRef, useState } from "react";
import type { RealtimePreset, RealtimeStats, Voice } from "../api";
import { listVoices, realtimeBegin, realtimeEnd, realtimeOffer, realtimeStats } from "../api";

type Props = { online: boolean };

type DeviceOption = { id: string; label: string };

type SinkElement = HTMLAudioElement & { setSinkId?: (deviceId: string) => Promise<void> };

const presets: { value: RealtimePreset; label: string; note: string }[] = [
  { value: "low-latency", label: "Low latency", note: "0.12 s blocks · best for conversation" },
  { value: "balanced", label: "Balanced", note: "0.24 s blocks · steady quality" },
  { value: "quality", label: "Quality", note: "0.40 s blocks · most natural, more delay" },
];

function looksLikeVirtualCable(label: string): boolean {
  const text = label.toLowerCase();
  // VB-CABLE names its playback side "CABLE Input (VB-Audio Virtual Cable)";
  // extra cables are "CABLE-A Input"; Voicemeeter uses "Voicemeeter Input".
  return (
    /\bcable[- ]?[a-d]?\s*input\b/.test(text) ||
    /voicemeeter.*input/.test(text) ||
    text.includes("virtual cable")
  );
}

export function RealtimePage({ online }: Props) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [preset, setPreset] = useState<RealtimePreset>("balanced");
  const [inputs, setInputs] = useState<DeviceOption[]>([]);
  const [outputs, setOutputs] = useState<DeviceOption[]>([]);
  const [inputId, setInputId] = useState("");
  const [virtualId, setVirtualId] = useState("");
  const [monitorId, setMonitorId] = useState("");
  const [monitor, setMonitor] = useState(true);
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stats, setStats] = useState<RealtimeStats | null>(null);
  const [blockSeconds, setBlockSeconds] = useState<number | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [micAccess, setMicAccess] = useState<"unknown" | "granted" | "denied">("unknown");
  const [linkState, setLinkState] = useState<string>("idle");

  const pcRef = useRef<RTCPeerConnection | null>(null);
  const localRef = useRef<MediaStream | null>(null);
  const sessionRef = useRef<{ id: string; token: string; voiceId: string } | null>(null);
  const pollRef = useRef<number | null>(null);
  const virtualAudioRef = useRef<HTMLAudioElement | null>(null);
  const monitorAudioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (!online) {
      setVoices([]);
      return;
    }
    listVoices()
      .then((result) => {
        const seedVoices = result.voices.filter((voice) => voice.engine === "seed-vc");
        setVoices(seedVoices);
        if (seedVoices.length > 0) setVoiceId((current) => current || seedVoices[0].id);
      })
      .catch((problem) => setError(String(problem)));
  }, [online]);

  // Device labels are only populated once the page holds microphone permission.
  const refreshDevices = async () => {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputList = devices
      .filter((device) => device.kind === "audioinput")
      .map((device) => ({ id: device.deviceId, label: device.label || "Microphone" }));
    const outputList = devices
      .filter((device) => device.kind === "audiooutput")
      .map((device) => ({ id: device.deviceId, label: device.label || "Output" }));
    setInputs(inputList);
    setOutputs(outputList);

    const cable = outputList.find((device) => looksLikeVirtualCable(device.label));
    setVirtualId((current) => current || cable?.id || "");
    setInputId((current) => current || inputList[0]?.id || "");
    return { inputList, outputList, cable };
  };

  useEffect(() => {
    if (!online) return;

    // Windows withholds device names until the app holds microphone permission,
    // which makes the virtual cable impossible to identify. Ask once, then
    // re-enumerate so the list is readable.
    const unlock = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((track) => track.stop());
        setMicAccess("granted");
      } catch {
        setMicAccess("denied");
      }
      await refreshDevices().catch(() => undefined);
    };
    void unlock();

    const onDeviceChange = () => {
      void refreshDevices().catch(() => undefined);
    };
    navigator.mediaDevices?.addEventListener("devicechange", onDeviceChange);
    return () => navigator.mediaDevices?.removeEventListener("devicechange", onDeviceChange);
  }, [online]);

  useEffect(() => {
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, []);

  // Send the converted stream to the virtual cable and the monitor copy to the
  // user's own headphones. setSinkId is Chromium-only, which WebView2 provides.
  useEffect(() => {
    const element = virtualAudioRef.current as SinkElement | null;
    if (element?.setSinkId && virtualId) {
      void element.setSinkId(virtualId).catch(() => undefined);
    }
  }, [virtualId, live]);

  useEffect(() => {
    const element = monitorAudioRef.current as SinkElement | null;
    if (element?.setSinkId && monitorId) {
      void element.setSinkId(monitorId).catch(() => undefined);
    }
  }, [monitorId, live, monitor]);

  const stop = async () => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
    const session = sessionRef.current;
    sessionRef.current = null;
    pcRef.current?.close();
    pcRef.current = null;
    localRef.current?.getTracks().forEach((track) => track.stop());
    localRef.current = null;
    setLive(false);
    setLinkState("idle");
    setStats(null);
    setStartedAt(null);
    if (session) {
      try {
        await realtimeEnd(session.id, session.voiceId);
      } catch {
        // The tunnel may already be gone; nothing useful to surface here.
      }
    }
  };

  const start = async () => {
    setBusy(true);
    setError("");
    try {
      const devices = await refreshDevices();
      if (!devices.cable) {
        setError(
          "No virtual audio cable found. Install VB-CABLE (or Voicemeeter), then reopen this page and pick its input as the output device.",
        );
        return;
      }

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: inputId ? { exact: inputId } : undefined,
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      localRef.current = stream;
      await refreshDevices();

      const ticket = await realtimeBegin(voiceId, preset);
      sessionRef.current = { id: ticket.session_id, token: ticket.token, voiceId };

      // The worker gathers its own candidates, but the desktop has to produce
      // usable ones too. Chromium normally hides local addresses behind mDNS
      // `.local` names, which aiortc cannot resolve, so ICE never completes and
      // the server sits waiting for audio that never arrives. STUN gives both
      // sides a routable address to try.
      const pc = new RTCPeerConnection({
        iceServers: [
          { urls: "stun:stun.l.google.com:19302" },
          { urls: "stun:stun1.l.google.com:19302" },
        ],
      });
      pcRef.current = pc;
      setLinkState("connecting");
      pc.onconnectionstatechange = () => {
        setLinkState(pc.connectionState);
        if (pc.connectionState === "failed") {
          setError(
            "The audio link could not be established. Your network is probably blocking the direct UDP path to the worker; try a different network or move the worker closer.",
          );
        }
      };
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));

      const remote = new MediaStream();
      pc.ontrack = (event) => {
        remote.addTrack(event.track);
        if (virtualAudioRef.current) {
          virtualAudioRef.current.srcObject = remote;
          void virtualAudioRef.current.play().catch(() => undefined);
        }
        if (monitorAudioRef.current) {
          monitorAudioRef.current.srcObject = remote;
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const answer = await realtimeOffer(
        ticket.session_id,
        ticket.token,
        pc.localDescription?.sdp ?? offer.sdp ?? "",
        pc.localDescription?.type ?? "offer",
      );
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      setBlockSeconds(answer.block_seconds);
      setLive(true);
      setStartedAt(Date.now());

      // If media never starts flowing, say so instead of sitting silently at
      // "live" with nothing happening.
      window.setTimeout(() => {
        if (sessionRef.current?.id !== ticket.session_id) return;
        if (pcRef.current?.connectionState !== "connected") {
          setError(
            "No audio path was established within 25 seconds. The worker is reachable for control but the media connection did not come up.",
          );
        }
      }, 25000);

      pollRef.current = window.setInterval(async () => {
        const session = sessionRef.current;
        if (!session) return;
        try {
          setStats(await realtimeStats(session.id));
        } catch {
          // Transient failures while the stream settles are not worth surfacing.
        }
      }, 1500);
    } catch (problem) {
      setError(String(problem));
      await stop();
    } finally {
      setBusy(false);
    }
  };

  const estimatedLatency = useMemo(() => {
    if (!stats?.inference_ms_mean || !blockSeconds) return null;
    // Two blocks of pipeline priming, one inference, plus a nominal network leg.
    return Math.round(blockSeconds * 2 * 1000 + stats.inference_ms_mean + 120);
  }, [stats, blockSeconds]);

  const uptime = startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : 0;
  const labelsHidden = outputs.length > 0 && outputs.every((device) => device.label === "Output");
  const cableDetected = outputs.some((device) => looksLikeVirtualCable(device.label));

  if (!online) {
    return (
      <div className="page">
        <div className="eyebrow">LIVE</div>
        <h1>Realtime</h1>
        <div className="empty">Connect to a GPU worker to stream a voice.</div>
      </div>
    );
  }

  return (
    <div className="page">
      <div className="eyebrow">LIVE</div>
      <h1>Realtime</h1>
      <p className="lede">
        Your microphone is converted on the GPU and played into a virtual cable, so Discord, Zoom and OBS can use it
        as their microphone.
      </p>

      <section className="panel">
        <div className="panel-head">
          <span className="step">1</span>
          <div>
            <h2>Routing</h2>
            <p>Output must be the virtual cable's playback device. Pick its recording side in your call app.</p>
          </div>
          {live && <span className="pill good">Live {uptime}s</span>}
        </div>

        <label>
          Microphone
          <select value={inputId} onChange={(event) => setInputId(event.target.value)}>
            {inputs.map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          Converted audio output (virtual cable)
          <select value={virtualId} onChange={(event) => setVirtualId(event.target.value)}>
            <option value="">No virtual cable detected</option>
            {outputs.map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
                {looksLikeVirtualCable(device.label) ? " — virtual cable" : ""}
              </option>
            ))}
          </select>
        </label>

        <div className="actions left">
          <button className="secondary" onClick={() => void refreshDevices()}>
            Refresh devices
          </button>
        </div>

        {micAccess === "denied" && (
          <div className="result bad">
            <span className="result-indicator" />
            <span>
              Windows is blocking microphone access, which also hides device names. Open Settings → Privacy &amp;
              security → Microphone, turn on microphone access for desktop apps, then reopen this page.
            </span>
          </div>
        )}
        {micAccess !== "denied" && labelsHidden && (
          <div className="result">
            <span className="result-indicator" />
            <span>
              Device names are hidden until microphone access is granted. Click Refresh devices; if names stay blank,
              allow microphone access for desktop apps in Windows privacy settings.
            </span>
          </div>
        )}
        {micAccess !== "denied" && !labelsHidden && !cableDetected && outputs.length > 0 && (
          <div className="result">
            <span className="result-indicator" />
            <span>
              No VB-CABLE or Voicemeeter output was found among {outputs.length} playback devices. If you just
              installed it, reboot so Windows registers the driver, then click Refresh devices.
            </span>
          </div>
        )}

        <label className="checkbox">
          <input type="checkbox" checked={monitor} onChange={(event) => setMonitor(event.target.checked)} />
          <span>Monitor the converted voice in my headphones</span>
        </label>
        {monitor && (
          <label>
            Monitoring output
            <select value={monitorId} onChange={(event) => setMonitorId(event.target.value)}>
              <option value="">System default</option>
              {outputs
                .filter((device) => !looksLikeVirtualCable(device.label))
                .map((device) => (
                  <option key={device.id} value={device.id}>
                    {device.label}
                  </option>
                ))}
            </select>
          </label>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="step">2</span>
          <div>
            <h2>Voice and preset</h2>
            <p>{voices.length === 0 ? "Create a Seed-VC voice first." : "Zero-shot Seed-VC runs live without training."}</p>
          </div>
        </div>
        <label>
          Voice
          <select value={voiceId} onChange={(event) => setVoiceId(event.target.value)} disabled={voices.length === 0}>
            {voices.length === 0 && <option value="">No voices</option>}
            {voices.map((voice) => (
              <option key={voice.id} value={voice.id}>
                {voice.name}
              </option>
            ))}
          </select>
        </label>
        <div className="preset-grid">
          {presets.map((option) => (
            <button
              key={option.value}
              className={`preset ${preset === option.value ? "selected" : ""}`}
              onClick={() => setPreset(option.value)}
              disabled={live}
            >
              <strong>{option.label}</strong>
              <span>{option.note}</span>
            </button>
          ))}
        </div>
        <div className="actions">
          {live ? (
            <button className="secondary" onClick={() => void stop()}>
              Stop
            </button>
          ) : (
            <button className="primary" disabled={busy || !voiceId} onClick={() => void start()}>
              {busy ? "Preparing…" : "Go live"}
            </button>
          )}
        </div>
        {busy && (
          <div className="result">
            <span className="result-indicator" />
            <span>The worker loads the model on first use, which can take up to a minute.</span>
          </div>
        )}
      </section>

      {(error || live) && (
        <section className="panel">
          <div className="panel-head">
            <span className="step">3</span>
            <div>
              <h2>Performance</h2>
              <p>Measured on the GPU worker, not estimated from the hardware.</p>
            </div>
          </div>
          {error && (
            <div className="result bad">
              <span className="result-indicator" />
              <span>{error}</span>
            </div>
          )}
          {live && (
            <div className="stat-grid">
              <div className="stat">
                <span className="stat-label">Inference</span>
                <span className="stat-value">{stats?.inference_ms_mean ? `${stats.inference_ms_mean} ms` : "—"}</span>
                <span className="stat-note">mean per block · p95 {stats?.inference_ms_p95 ?? "—"} ms</span>
              </div>
              <div className="stat">
                <span className="stat-label">Block size</span>
                <span className="stat-value">{blockSeconds ? `${(blockSeconds * 1000).toFixed(0)} ms` : "—"}</span>
                <span className="stat-note">pipeline primes two blocks</span>
              </div>
              <div className="stat">
                <span className="stat-label">Estimated latency</span>
                <span className="stat-value">{estimatedLatency ? `${estimatedLatency} ms` : "—"}</span>
                <span className="stat-note">includes one network leg</span>
              </div>
              <div className="stat">
                <span className="stat-label">Stream health</span>
                <span className="stat-value">
                  {(stats?.blocks ?? 0) === 0
                    ? "Waiting for audio"
                    : stats?.dropped_frames === 0
                      ? "No drops"
                      : `${stats?.dropped_frames} drops`}
                </span>
                <span className="stat-note">
                  {stats?.blocks ?? 0} blocks · queue {stats?.queued_frames ?? 0} · link {linkState}
                </span>
              </div>
            </div>
          )}
        </section>
      )}

      <audio ref={virtualAudioRef} style={{ display: "none" }} />
      {monitor && <audio ref={monitorAudioRef} autoPlay style={{ display: "none" }} />}
    </div>
  );
}
