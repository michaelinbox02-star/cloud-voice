import { useEffect, useRef, useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";

type Props = {
  path: string | null;
  height?: number;
};

/** Draws a static peak envelope for a local audio file. */
export function Waveform({ path, height = 56 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">("idle");

  useEffect(() => {
    let cancelled = false;
    const canvas = canvasRef.current;
    if (!canvas) return;

    const context = canvas.getContext("2d");
    if (!context) return;

    const clear = () => {
      canvas.width = canvas.clientWidth * window.devicePixelRatio;
      canvas.height = height * window.devicePixelRatio;
      context.setTransform(window.devicePixelRatio, 0, 0, window.devicePixelRatio, 0, 0);
      context.clearRect(0, 0, canvas.width, canvas.height);
    };
    clear();

    if (!path) {
      setState("idle");
      return;
    }

    setState("loading");
    (async () => {
      try {
        const response = await fetch(convertFileSrc(path));
        const buffer = await response.arrayBuffer();
        if (cancelled) return;
        const audio = new AudioContext();
        const decoded = await audio.decodeAudioData(buffer);
        await audio.close();
        if (cancelled) return;

        const samples = decoded.getChannelData(0);
        const columns = Math.max(1, Math.floor(canvas.clientWidth));
        const step = Math.max(1, Math.floor(samples.length / columns));
        const middle = height / 2;
        context.strokeStyle = "#c8a674";
        context.lineWidth = 1;
        context.beginPath();
        for (let column = 0; column < columns; column += 1) {
          let peak = 0;
          const start = column * step;
          for (let index = 0; index < step && start + index < samples.length; index += 1) {
            peak = Math.max(peak, Math.abs(samples[start + index]));
          }
          const extent = Math.max(1, peak * (height / 2 - 2));
          context.moveTo(column + 0.5, middle - extent);
          context.lineTo(column + 0.5, middle + extent);
        }
        context.stroke();
        setState("ready");
      } catch {
        if (!cancelled) setState("error");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [path, height]);

  return (
    <div className="waveform" style={{ height }}>
      <canvas ref={canvasRef} style={{ width: "100%", height }} />
      {state !== "ready" && (
        <span className="waveform-note">
          {state === "loading" ? "Rendering waveform…" : state === "error" ? "Waveform unavailable" : "No audio loaded"}
        </span>
      )}
    </div>
  );
}
