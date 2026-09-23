# Architecture

## Boundaries

The desktop is Windows-first. It captures a physical microphone, controls audio devices, handles WebRTC signaling/media, and sends converted audio to a selected virtual cable playback endpoint. Apps such as Discord select the corresponding virtual cable recording endpoint. A signed virtual audio driver is a separate OS component; Cloud Voice Studio detects it and guides installation when absent.

The GPU worker is disposable. Durable voice data lives in a worker volume and must be exported before teardown. A backup bundle can restore it on a fresh worker. No ML runtime or weights are installed on the desktop.

## Planned services

- Management API: authenticated health, profiles, jobs, telemetry, and short-lived realtime session grants.
- Seed-VC runtime: pinned `jiaheguo521/seed-vc-realtime` revision `58d0ca372cc76f60a798d9f84611f053f9029f3a` for realtime conversion; offline integration is a separate adapter.
- RVC runtime: pinned `RVC-Project/Retrieval-based-Voice-Conversion-WebUI` revision `81eed5e8f68b6bed1789f682fe78cdd324495afc` for inference and training.
- TTS runtime: Kokoro base speech, followed by Seed-VC or RVC conversion.

Each model environment is isolated in its own container. Model artifacts are downloaded directly on the GPU and checksum-verified. The release manifest pins image, upstream, and model revisions. The desktop uses an SSH connection for provisioning and a TLS management channel. Realtime media uses WebRTC/Opus with a bounded jitter buffer and session tokens.

## Performance gates

Benchmark RVC and Seed-VC separately on the actual GPU, including network RTT, jitter, packet loss, inference duration, buffer depth, and mic-to-virtual-mic latency. Presets are recommended from those measurements. The upstream Seed-VC fork reports hundreds of milliseconds of local latency, so no fixed latency target is presumed.

## Current milestone

GPU preflight and an authenticated management health service are implemented first. No conversion endpoint should claim success until actual inference and an audio quality smoke test pass on the GPU.
