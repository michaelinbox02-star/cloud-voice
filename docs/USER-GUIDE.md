# Cloud Voice Studio — quick guide

## Before you start

You need two things:

1. A GPU server that the app can reach over SSH (yours is already set up).
2. **VB-CABLE** installed on your PC — it only matters for live voice. Without it,
   Discord and Zoom cannot see the converted voice as a microphone.

## 1. Connect to the GPU

Open **Server**.

| Field | Value |
| --- | --- |
| Host | your GPU server address |
| Port | `22` |
| Username | your SSH username |
| Private key path | the `.ssh` key file you use to log in |
| GitHub repository | the project repository URL |

Click **Test server**, then **Connect**. If the worker was never installed, click
**Install on GPU** instead; it takes several minutes the first time.

When you are connected, the panel shows the GPU, free disk space and which
engines are ready.

## 2. Make a voice

Open **Voices**.

**Seed-VC (no training)** — the fast option. Give it 5–30 seconds of clean
speech, no music or background noise. Pick **Seed-VC** as the engine, choose the
recording, name it, and create. It is ready immediately.

**RVC (trained)** — if you already have a `.pth` model, pick **RVC v2**, choose
the model and its `.index` file if you have one. Or train your own on the
**Training** page, which can add the result here in one click.

## 3. Convert a file

Open **Studio → Voice to Voice**.

1. Choose the audio file to convert.
2. Pick the voice you want it to become.
3. Click **Convert**.

You get the waveform, playback, and timing (how long the GPU took, and how much
faster or slower than real time). Use **Save a copy…** to export as WAV, FLAC or
MP3. Open *Advanced parameters* only if you want to trade quality against speed.

## 4. Talk live through the converted voice

Open **Realtime**.

1. **Microphone** — your real mic.
2. **Converted audio output** — pick `CABLE Input (VB-Audio Virtual Cable)`. This
   is the part people miss: the app *plays* into the cable, and Discord listens to
   the other end of it.
3. Tick **Monitor** if you want to hear yourself in your headphones.
4. Pick the voice and a preset:
   - **Low latency** — best for conversation.
   - **Balanced** — steadier, a bit more delay.
   - **Quality** — nicest sound, most delay.
5. Click **Go live**.

Then in Discord, Zoom or OBS, set your **microphone** to
`CABLE Output (VB-Audio Virtual Cable)`.

The first time you go live the GPU has to load the model — up to a minute. After
that it starts in seconds. The panel below shows live timing and whether any
audio is being dropped.

## 5. Type something and have it spoken

Open **Studio → Text to Speech**.

Pick a reader voice, type or paste your text, and choose a voice to convert
through if you want it to sound like someone specific — or leave it as **None**
for plain speech. Generate, listen, then save as WAV, FLAC or MP3.

## 6. Train an RVC voice

Open **Training**. It runs entirely on the GPU.

1. Put your recordings in a `.zip` and select it. 5–30 minutes of clean speech is
   a good target.
2. Give it a name.
3. Leave the defaults unless you know better: 200 epochs, batch size 8, 40 kHz.
   Tick **Pitch guidance** if you will sing with this voice.
4. Click **Start training**.

It takes roughly five minutes for a small dataset and much longer for a real one.
You can close the window; the GPU keeps working. When it finishes, click **Add to
voice library** and the voice appears on the Voices page.

## 7. Back up before you throw the server away

Open **Settings**.

- **Export backup…** writes one file containing every voice, every RVC model and
  the whole history.
- **Restore from backup…** puts it all back on a replacement worker.

Do this before destroying a rented GPU. It is the only copy of your voices.

## When something goes wrong

**The virtual cable is not listed.** Windows hides device names until the app has
microphone permission. Open the Realtime page, click **Refresh devices**, and if
the names are still blank allow microphone access for desktop apps in Windows
Settings → Privacy & security → Microphone. If you just installed VB-CABLE,
reboot once.

**Live voice feels delayed.** Try the **Low latency** preset. The worker is in
Los Angeles; distance adds delay that no setting can remove. A GPU server closer
to you is the real fix.

**The first conversion is slow.** Models are downloading or loading. Only the
first one pays that cost.

**The voice does not sound like the person.** For Seed-VC, use a cleaner and
longer reference clip. For RVC, train on more clean audio and more epochs.

**A job failed.** The error text is shown on the job itself and is written to be
actionable — it names the missing file or the rejected setting.
