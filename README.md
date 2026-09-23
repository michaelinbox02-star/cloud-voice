# Cloud Voice Studio

Cloud Voice Studio is a Windows desktop client for voice conversion backed by a disposable Linux NVIDIA GPU worker. The desktop owns audio devices, the UI, and transport. Model inference and training run only on the worker.

## Status

The repository is under active development. The current runnable milestone covers GPU preflight, SSH deployment from the Windows desktop app, and an authenticated worker health service. Voice conversion and live streaming are not yet available.

## Layout

- `desktop/`: Tauri 2, React, TypeScript desktop client.
- `worker/`: GPU worker containers and management API.
- `scripts/`: idempotent server bootstrap and deployment.
- `docs/`: architecture and operational notes.

## GPU host prerequisites

Ubuntu 24.04 with an NVIDIA driver, an SSH account with passwordless sudo, outbound access to GitHub and model registries, and enough disk for model weights. The setup wizard will validate these and install Docker/NVIDIA Container Toolkit when needed.

Never commit credentials, private keys, recordings, datasets, or model weights. The `.gitignore` covers common forms; review every commit before pushing.

## Current development server

The initial GPU preflight passed on Ubuntu 24.04.1, Tesla V100-SXM3-32GB, driver 580.178.04. NVIDIA containers run successfully. This does not establish voice inference or latency performance.
