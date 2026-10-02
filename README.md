# Kasa Lights Sync

A [Music Assistant](https://music-assistant.io) plugin that syncs TP-Link Kasa
smart lights / light strips to music in real time — brightness pulses on the
beat, color drifts and jumps with the track, all driven by the same
real-time audio analysis pipeline that powers Music Assistant's built-in
[Hue Lights Sync](https://www.music-assistant.io/plugins/hue-entertainment/)
plugin.

It works with any source Music Assistant plays (Spotify, Tidal, local files,
etc.) because the analysis runs on the PCM audio MA has already decoded for
playback, not on the original file — see "How it works" below.

> **Status: experimental, not yet run against a live Music Assistant
> instance.** The audio analysis logic (`analyzer.py`) is unit tested and
> passing (see `tests/`). The Sendspin registration/plugin wiring
> (`bridge.py`, `provider.py`) is written against Music Assistant's actual
> source (not guessed — see "Built against" below) but hasn't yet been
> exercised against a running server. Expect to file/fix a few rough edges
> on first real deploy. Contributions and bug reports welcome.

## How it works

Music Assistant decodes every source (Spotify, Tidal, local files, whatever)
into the same PCM stream before it reaches any player. A real-time feature
extractor built into Music Assistant's `sendspin` provider (`aiosendspin`'s
`VisualizerFeatureExtractor`) turns that stream into spectrum/loudness
frames at a few Hz, plus a scheduled beat grid sourced from the optional
`smart_fades` analysis provider. This plugin registers as a **virtual
Sendspin player** (type `LIGHT`, exactly how Hue Lights Sync registers its
Entertainment Areas) that receives those frames, turns them into target
hue/saturation/brightness values, and sends them to your Kasa device(s) over
the local network via [`python-kasa`](https://github.com/python-kasa/python-kasa).

You group the resulting "Kasa: <name>" player with whatever real player is
actually playing to the room, the same way you'd group a Hue Lights Sync
player — see **Usage** below.

**Why Kasa doesn't just do this itself:** Kasa light strips (KL420L5,
KL400L5, etc.) do have a "Music" mode, but it only exists in the TP-Link
Kasa phone app, which uses your phone's own microphone — there's no local
API call for it, so nothing server-side (Home Assistant, Music Assistant, or
otherwise) can trigger it. This plugin gets real audio-reactive behavior
onto the same hardware a different way: by driving `set_hsv()`/transition
calls directly from Music Assistant's own analysis of what it's playing.

**Why a Kasa update doesn't look as fluid as Hue's:** Hue Entertainment
streams color over a continuous low-latency DTLS connection at 30Hz. Kasa's
local protocol is a plain request/response TCP call per command — there's no
streaming mode. This plugin renders at a much lower rate (`RENDER_RATE_HZ` in
`const.py`, default 4Hz) and leans on `set_hsv()`'s own `transition_ms`
parameter so the strip's firmware eases between points instead of visibly
stepping. It's a pulse-and-drift effect, not frame-accurate strobing.

## Built against

This was written by reading the actual Music Assistant source
(`music-assistant/server`, Apache-2.0) rather than guessing at its plugin
API, specifically:

- `music_assistant/providers/hue_entertainment/{provider,bridge,analyzer,constants}.py`
  — the direct model for this plugin's shape and the Sendspin
  registration/render-loop pattern (`bridge.py` here is a close structural
  mirror, with Hue's DTLS session swapped for direct `python-kasa` calls).
- `music_assistant/providers/milkdrop_visualizer/tap.py` — confirmed that
  MA's audio analysis is source-agnostic (reads the already-decoded playback
  buffer, so Spotify/Tidal/local all work identically).
- `music_assistant/providers/sendspin/bridge_role.py` — the
  `BridgeVisualizerRole` extension point this plugin registers against.
- `music_assistant/models/audio_analysis.py` and
  `music_assistant/models/audio_analysis_provider.py` — confirms analysis
  "do[es] not need to know which context [it is] running in" (live playback
  vs. background scan), source-agnostic by design.
- [`aiosendspin`](https://pypi.org/project/aiosendspin/)'s
  `server/roles/visualizer/features.py` (`VisualizerFeatureExtractor`,
  `ExtractedFrame`) — the actual FFT/spectrum/onset engine behind the
  real-time frames this plugin consumes.

## Why this is a separate repo, not a fork of `music-assistant/server`

[`music-assistant-plugin-manager`](https://github.com/TigreGotico/music-assistant-plugin-manager)
patches provider discovery at runtime so any pip-installable package
registered under the `music_assistant.provider` entrypoint group loads
automatically — **no modification to Music Assistant's own source tree
required.** Forking the whole server repo for one new provider directory
would mean dragging along its entire monorepo, CI, and release process to
track upstream forever, for a change that doesn't touch any of it.

## Requirements

- A Music Assistant server (2.9+) with the `sendspin` provider running (it's
  built in). The optional `smart_fades` provider adds real beat-grid data —
  without it, this plugin still reacts to loudness/spectrum, just without
  beat-locked flashes.
- One or more Kasa light strips/bulbs with a `Light` module (color or color
  temp) — tested conceptually against KL420L5/KL400L5; anything
  `python-kasa` supports should work.
- **A DHCP reservation for each Kasa device.** This plugin connects by IP,
  not by the Kasa app's device name.
- Python 3.12+ (Music Assistant's own requirement).

## Installation

### Self-hosted / Docker Music Assistant

```bash
pip install kasa-lights-sync music-assistant-plugin-manager
```

Then launch the server with the plugin manager's wrapper instead of the
normal entry point:

```bash
python -m music_assistant_plugin_manager
```

### Home Assistant "Music Assistant" Supervisor app (official pre-built app)

The official app is pre-built and auto-updating with no exposed hook for
custom plugins — there's no way to drop this in without changing how the
container starts. The lowest-friction option is a **minimal local add-on**
whose only diff from the upstream Dockerfile is installing this package (and
the plugin manager) and changing the launch command — it does **not** need
to vendor or track Music Assistant's own source, so it's a much thinner,
lower-drift fork than patching `music_assistant/providers/` directly would
be. Rough shape:

```dockerfile
FROM ghcr.io/music-assistant/server:2.10.4   # pin to the version you're running
RUN pip install kasa-lights-sync music-assistant-plugin-manager
CMD ["python", "-m", "music_assistant_plugin_manager"]
```

Install as a local add-on (`build: true`) the same way as any custom
Supervisor add-on. You'll still get Music Assistant's own updates by
bumping the pinned base image tag — you won't get Supervisor's one-click
auto-update for this app anymore, since the image is now locally built.

## Usage

1. Settings → Add-ons/Integrations → add **Kasa Lights Sync**.
2. Enter the Kasa device IP(s) for this light group (comma-separated), pick a
   visualization style, set a brightness ceiling.
3. A new player, "Kasa: <name>", appears. Group it with the real player
   that's actually playing to the room (same workflow as Hue Lights Sync).
4. Play music. The grouped Kasa lights should start reacting within a few
   seconds — give a track a moment on first play if beat data isn't cached
   yet (the beat-tracking model takes a few seconds on a track's first
   play; instant on repeat plays).

## Configuration

| Setting | Description |
|---|---|
| `device_hosts` | Comma-separated local IPs of the Kasa lights this group drives. |
| `color_mode` | `smooth` (gentle, beat-tinted color drift), `ambient` (slower, bass-reactive saturation), `flashing` (strong pulse every beat), `energetic` (big brightness swings, fast color rotation). |
| `brightness` | 1–100, the ceiling this group renders up to. |

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_analyzer.py` covers the pure audio→light logic without needing a
live Music Assistant server or real hardware. `bridge.py`/`provider.py`
integrate directly with Music Assistant/Sendspin internals and are best
exercised against a real running server.

## License

Apache License 2.0 (same as Music Assistant itself — see `LICENSE`). This
project depends on Music Assistant's internal APIs at runtime but contains
no code copied from it.

## Credits

Architecture modeled directly on Music Assistant's built-in **Hue Lights
Sync** and **MilkDrop Visualizer** plugins (music-assistant/server,
Apache-2.0). All credit for the underlying real-time audio analysis pipeline
goes to the Music Assistant project.
