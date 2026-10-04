# HA Lights Sync

A [Music Assistant](https://music-assistant.io) plugin that syncs **any
Home Assistant light** to music in real time — brightness pulses on the
beat, color drifts and jumps with the track, all driven by the same
real-time audio analysis pipeline that powers Music Assistant's built-in
[Hue Lights Sync](https://www.music-assistant.io/plugins/hue-entertainment/)
plugin.

If a light already works in Home Assistant — Kasa, Hue, LIFX, Zigbee,
whatever — it works here. No device IPs, no separate pairing, no new
integration to set up: the light picker in this plugin's config is
populated live from your existing Home Assistant entities.

It also works with any source Music Assistant plays (Spotify, Tidal, local
files, etc.), because the analysis runs on the PCM audio MA has already
decoded for playback, not on the original file — see "How it works" below.

> **Status: experimental.** The audio analysis logic (`analyzer.py`) is unit
> tested and passing (see `tests/`). The Sendspin registration/plugin wiring
> (`bridge.py`, `provider.py`) is written against Music Assistant's actual
> source (not guessed — see "Built against" below); against a real server
> it loads, registers its config, and correctly validates/rejects bad input
> (confirmed against a real Music Assistant instance with real Spotify and
> Chromecast players during development). Actually confirming a real light
> reacts end-to-end is the next step - not yet verified. Contributions and
> bug reports welcome.

## How it works

Music Assistant decodes every source (Spotify, Tidal, local files, whatever)
into the same PCM stream before it reaches any player. A real-time feature
extractor built into Music Assistant's `sendspin` provider (`aiosendspin`'s
`VisualizerFeatureExtractor`) turns that stream into spectrum/loudness
frames at a few Hz, plus a scheduled beat grid sourced from the optional
`smart_fades` analysis provider. This plugin registers as a **virtual
Sendspin player** (type `LIGHT`, exactly how Hue Lights Sync registers its
Entertainment Areas) that receives those frames, turns them into target
hue/saturation/brightness values, and sends them on via Home Assistant's own
**`light.turn_on` service** — reusing the already-authenticated connection
Music Assistant's built-in "Home Assistant" plugin maintains
(`mass.get_provider("hass").hass`, a `hass_client.HomeAssistantClient`).

That's the whole trick: this plugin never talks to a light's actual
protocol/brand at all. Home Assistant already knows how, for anything it
controls, so this plugin just asks HA to do it, with a transition timed to
the beat.

You group the resulting "HA Lights: &lt;name&gt;" player with whatever real
player is actually playing to the room, the same way you'd group a Hue
Lights Sync player — see **Usage** below.

**Why your lights don't just do this themselves:** some smart lights (e.g.
TP-Link Kasa strips) do have a "Music" mode, but it typically only exists in
the manufacturer's own phone app, using your phone's microphone — there's no
local API call for it, so nothing server-side (Home Assistant, Music
Assistant, or otherwise) can trigger it. This plugin gets real audio-reactive
behavior onto whatever hardware you already have a different way: by driving
`light.turn_on` calls directly from Music Assistant's own analysis of what
it's playing.

**Why it doesn't look as fluid as Hue Entertainment:** Hue Entertainment
streams color over a continuous low-latency DTLS connection at 30Hz. A
`light.turn_on` service call is a plain request/response round trip (through
HA's websocket API, then whatever HA does internally for that light's actual
integration) — there's no streaming mode. This plugin renders at a much
lower rate (`RENDER_RATE_HZ` in `const.py`, default 4Hz) and leans on
`transition` (seconds) so the light eases between points instead of visibly
stepping. It's a pulse-and-drift effect, not frame-accurate strobing, and how
fluid it feels further depends on how fast your specific light/integration
responds to HA's own service call.

## Built against

This was written by reading the actual Music Assistant source
(`music-assistant/server`, Apache-2.0) rather than guessing at its plugin
API, specifically:

- `music_assistant/providers/hue_entertainment/{provider,bridge,analyzer,constants}.py`
  — the direct model for this plugin's shape and the Sendspin
  registration/render-loop pattern (`bridge.py` here is a close structural
  mirror, with Hue's DTLS session swapped for a `light.turn_on` service call).
- `music_assistant/providers/hass/__init__.py` — confirms the "Home
  Assistant" plugin exposes its connected client as the public attribute
  `self.hass: HomeAssistantClient`, reachable from any other plugin via
  `mass.get_provider("hass").hass` with zero extra config, when running as
  a Home Assistant add-on (`CONF_URL` defaults to the internal supervisor
  API, token auto-retrieved).
- [`hass-client`](https://github.com/music-assistant/python-hass-client)'s
  `HomeAssistantClient.call_service()`/`get_states()` — the actual calls this
  plugin makes (`light.turn_on` with `hs_color`/`brightness_pct`/`transition`,
  and a `light.*` entity sweep to populate the config picker).
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
  built in) and the **"Home Assistant" plugin** added and connected
  (Settings → Add Provider → Home Assistant). If you're running Music
  Assistant as a Home Assistant add-on, that plugin auto-connects with no
  extra config.
- The optional `smart_fades` analysis provider adds real beat-grid data —
  without it, this plugin still reacts to loudness/spectrum, just without
  beat-locked flashes.
- Any light(s) already working in Home Assistant, of any brand/integration.
- Python 3.12+ (Music Assistant's own requirement).

## Installation

### Self-hosted / Docker Music Assistant

```bash
pip install ha-lights-sync music-assistant-plugin-manager
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
be. Rough shape (see this plugin's test-deploy repo,
[`ha-music-assistant-kasa-addon`](https://github.com/zachfeldman/ha-music-assistant-kasa-addon),
for a working example):

```dockerfile
FROM ghcr.io/music-assistant/server:2.10.4   # pin to the version you're running

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --python matters: a bare `pip install` targets the base image's system
# Python, not $VIRTUAL_ENV (the venv Music Assistant actually runs from) -
# that venv has no pip binary of its own (built via uv, not pip). Caught on
# the first real deploy: build succeeded, container started, then failed
# with "No module named music_assistant_plugin_manager".
RUN pip --python "$VIRTUAL_ENV/bin/python" install --no-cache-dir \
    "ha-lights-sync @ git+https://github.com/zachfeldman/ha-lights-sync.git" \
    music-assistant-plugin-manager

RUN printf '#!/bin/sh\n\
for path in /usr/lib/*/libjemalloc.so.2; do\n\
    [ -f "$path" ] && export LD_PRELOAD="$path" MALLOC_CONF="background_thread:true,dirty_decay_ms:5000,muzzy_decay_ms:5000" && break\n\
done\n\
exec python -m music_assistant_plugin_manager "$@"\n' > /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
```

Install as a local add-on (`build: true`) the same way as any custom
Supervisor add-on. You'll still get Music Assistant's own updates by
bumping the pinned base image tag — you won't get Supervisor's one-click
auto-update for this app anymore, since the image is now locally built.
**This also means a fresh add-on starts with its own empty `/data`** - no
shared state (library, streaming service logins, paired players) with an
existing Music Assistant app install. Plan for that (e.g. test on a second
instance before replacing your main one) rather than assuming it inherits
your existing setup.

## Usage

1. Make sure Music Assistant's **Home Assistant** plugin is added and
   connected first (Settings → Add Provider → Home Assistant) — this
   plugin's light picker is empty until that's in place.
2. Settings → Add Provider → **HA Lights Sync**.
3. Pick the light(s) this group should drive (multi-select, straight from
   your existing Home Assistant entities), a visualization style, and a
   brightness ceiling.
4. A new player, "HA Lights: &lt;name&gt;", appears. Group it with the real
   player that's actually playing to the room (same workflow as Hue Lights
   Sync).
5. Play music. The grouped light(s) should start reacting within a few
   seconds — give a track a moment on first play if beat data isn't cached
   yet (the beat-tracking model takes a few seconds on a track's first
   play; instant on repeat plays).

### Changing which lights are in the group later

`color_mode`/`brightness`/`beat_multiplier`/`transition_style`/`sensitivity`
are regular settings, editable any time from the provider's own settings
screen. Which
*lights* are in the group is different - that's collected by the
interactive setup flow (see `setup_flow.py`), not a regular setting, so
editing it goes through Music Assistant's generic **Reconfigure** action
instead (exactly the same picker form as initial setup, prefilled with your
current selection) - not something built specifically for this plugin.

**If you don't see a "Reconfigure" button on the provider's settings page**,
that's a real gap in `music-assistant-plugin-manager` (confirmed, not
user error): Music Assistant's own provider loader auto-detects
`has_setup_flow` by checking whether a `setup_flow.py` file physically
exists under `music_assistant/providers/<domain>/` on disk. Plugin-manager-
loaded providers don't live there - they're redirected via an import hook
from wherever pip actually installed them - so that file-presence check
never finds it, and `has_setup_flow` silently defaults to `False` even
though the setup flow itself works perfectly (confirmed: it's invoked via
its own independent import-and-catch-`ModuleNotFoundError` check in
`music_assistant/controllers/config/flows.py`'s `_get_setup_flow_module`,
which never consults the `has_setup_flow` flag at all). The frontend's
Reconfigure button, however, *does* gate on that flag
(`EditProvider`'s visibility condition checks
`enabled && has_setup_flow && status !== INCOMPATIBLE`) - so the button
silently never renders, for a reason that has nothing to do with whether
reconfigure would actually work.

This plugin works around it by declaring `"has_setup_flow": true` directly
in its own `manifest.json`, since `music-assistant-plugin-manager`'s patch
builds the `ProviderManifest` straight from that file's contents rather
than computing it - any other plugin-manager-loaded provider that ships a
`setup_flow.py` will need the same one-line fix to get a working
Reconfigure button.

## Known limitations

**The "HA Lights: &lt;name&gt;" player may be hidden by default.** If you
don't see it in Music Assistant's main player list/switcher after setup,
check Settings → Players → HA Lights: &lt;name&gt; → "Hide this player in
the user interface" - uncheck it. It still exists and works either way;
this only affects whether the picker surfaces it.

**Grouping with a Chromecast/Google Home speaker can break that speaker's
playback.** This isn't a bug in this plugin - it's a real limitation of
Music Assistant's own Chromecast integration. Grouping *any* Sendspin-based
player (this plugin, or the built-in Hue Lights Sync) with a Cast speaker
requires switching that speaker from native Chromecast casting to MA's
**"Sendspin over Cast"** output protocol, which MA itself ships
experimental and off by default (Settings → Players → &lt;speaker&gt; →
"Show advanced settings" → enable Sendspin there first, or grouping will
silently do nothing). Confirmed during development: even with it enabled,
grouping sometimes causes Music Assistant to immediately log
`"<speaker> reported a media playback error"` and playback stops outright -
MA switches the speaker's output protocol the instant you group, and the
Cast device doesn't always pick up the new stream cleanly. If this happens
to you:
- It's worth testing with a non-Cast player (AirPlay, WiiM/LinkPlay, Sonos)
  to confirm whether it's Cast-specific - AirPlay-based Sendspin bridging
  appeared markedly more stable during development.
- This plugin does guard against the *consequence* that showed up alongside
  this for us (a wedged `light.turn_on` call permanently blocking that one
  light's future updates - see `CALL_SERVICE_TIMEOUT_S` in `const.py`), but
  it can't fix Music Assistant's own Cast/Sendspin bridge reliability.
- File this upstream against `music-assistant/server`'s Chromecast provider
  if you hit it, not this repo - there's nothing in `ha-lights-sync` that
  can work around it.

## Configuration

| Setting | Description |
|---|---|
| `light_entities` | The Home Assistant `light.*` entities this group drives. Multi-select, populated live from Home Assistant - any light already set up there is selectable. |
| `color_mode` | `smooth` (gentle, beat-tinted color drift), `ambient` (slower, bass-reactive saturation), `flashing` (strong pulse every beat), `energetic` (big brightness swings, fast color rotation), `pulse` (brightness continuously tracks the music's overall loudness, no beat flash - see below). |
| `brightness` | 1–100, the ceiling this group renders up to. |
| `beat_multiplier` ("Speed") | `1x`, `2x`, or `4x` pulses per beat (see below). |
| `transition_style` ("Transition style") | `fade` (default, eases between colors/brightness) or `instant` (hard, un-eased cut on every update - see below). |
| `sensitivity` ("Sensitivity") | 25–400%, default 100%. Gain on the detected audio signal itself - see below. Distinct from Speed, which only changes pulse *frequency*, not signal *strength*. |

All six settings apply to the running bridge immediately on save - no
restart, no re-grouping needed.

### Pulse mode

Every other `color_mode` is built around a beat flash on top of a resting
brightness. `pulse` drops the beat flash entirely
(`flash_strength=0`/`hue_beat_jump_deg=0` in `analyzer.py`'s `_PRESETS`) and
instead ties brightness directly and continuously to the track's overall
loudness across the *whole* spectrum, not just the bass band the other
modes watch (`_ModePreset.use_overall_level` - see `apply_spectrum`'s
`self._overall`, a full-bin average alongside the existing bass/treble
split). The result reads like a classic VU meter: brightness rises and
falls smoothly with how loud the music currently is, with a slow hue drift
so it doesn't look static, rather than pulsing on individual beats.

### Sensitivity

The analyzer has no idea how loud your room actually is - Sendspin hands it
spectrum magnitudes computed from whatever level the track happens to be
mixed/mastered at and wherever your volume happens to be set, so quieter
listening can leave every bin sitting well under 1.0, muting every mode's
swell/flash contrast even though beats are still being tracked correctly.
`sensitivity` is a plain gain multiplier applied to the raw spectrum
magnitude *before* it's clamped into the smoothed bass/treble/overall
levels (`apply_spectrum` in `analyzer.py`) - raise it if the lights look
washed-out/barely reactive at your normal listening volume. It has no effect
on beat timing at all: beat timestamps come from Sendspin's own tempo
tracking, a separate signal from the spectrum magnitude this setting scales
(see `push_beats`) - that's what makes it a genuinely different knob from
Speed (`beat_multiplier`), which only changes how often pulses fire.

### Speed (`beat_multiplier`)

The beat schedule Sendspin delivers is the track's real, tracked tempo - 1x
*is* the song's actual BPM, not an approximation. 2x and 4x aren't a
different analysis; they're synthesized by inserting 1 (2x) or 3 (4x)
evenly-spaced pulses between each pair of real beats, pulsing softer than
the real beat so the actual downbeat still reads as the strongest hit
(`_SUBBEAT_STRENGTH` in `analyzer.py`).

How crisp this looks in practice has a real ceiling, and it's not this
plugin's render loop (`RENDER_RATE_HZ` in `const.py` is 8Hz/125ms,
specifically sized to resolve 2x/4x subdivisions) - it's the round trip
through Home Assistant to your actual light. Measured against real TP-Link
Kasa strips during development: 13-220ms, mostly under 100ms. At 4x on a
fast track, sub-beats can land closer together than that round trip allows,
and `bridge.py` deliberately **skips** a render rather than queuing it (so
commands never arrive out of order) - logged as a `WARNING`
("Skipped render for ... - previous light.turn_on still in flight"). A few
of these on a very fast 4x track is normal; constant skipping means your
light/network is the bottleneck, not a setting to tune away here - try 2x
instead, or a less chatty light integration.

**Mixed-speed groups are handled automatically.** If one light in a group is
consistently slower to respond than the others (a laggy Wi-Fi strip grouped
with a fast Zigbee bulb, say), `bridge.py` tracks each entity's own recent
round-trip time independently and paces that specific entity to whatever it
can actually sustain, rather than either holding the whole group back to
match it or hammering it at the full shared render rate and constantly
logging skips for it (`_entity_latency_ms`/`ENTITY_LATENCY_SAFETY_FACTOR` in
`const.py`/`bridge.py`). It still reacts to every beat - just at its own
realistic pace - while faster lights in the same group keep rendering at the
full rate. This is automatic and needs no configuration; there's no "light
class" setting to pick, it adapts from real measured timing.

### Transition style (`transition_style`)

Every render sends a `transition` to `light.turn_on` alongside the target
color/brightness. `fade` (the default) sets it to the render period
(125ms at the default `RENDER_RATE_HZ`), so the light eases into each new
value - necessary for the continuous parts of the effect (hue drift,
bass-driven brightness swell) to look smooth rather than stepped, given how
infrequently a service call can realistically be sent compared to Hue
Entertainment's true continuous streaming.

`instant` sets `transition: 0` instead: no easing at all, just a hard cut on
every single update. The beat flash itself reads punchier and more
percussive this way - a "solid change in tempo" rather than a fade - but the
tradeoff is that the non-beat drift most color modes also do (hue rotation,
bass-reactive brightness) becomes visibly stepped too, since there's no
longer anything smoothing the gaps between renders. If that stepping bothers
you more than the fade softened the beat hits, `flashing` mode's own design
(floor near zero, almost no hue drift between beats) pairs especially well
with `instant`, since there's little continuous motion for the stepping to
show up in.

**Known hardware caveat:** TP-Link Kasa KL-series light strips (confirmed:
KL420L5) **ignore the `transition` parameter entirely and always snap
instantly**, regardless of what's sent - see
[python-kasa#389](https://github.com/python-kasa/python-kasa/issues/389) and
[home-assistant/core#57329](https://github.com/home-assistant/core/issues/57329),
both still open/unresolved as of this writing. On these specific lights,
`fade` vs `instant` makes **no visible difference** - the device was already
behaving like `instant` the whole time. If a light you've grouped doesn't
seem to respond to this setting, check whether its own integration has a
similar known limitation before assuming this plugin is broken; other light
brands/integrations (Hue, many Zigbee lights) do respect `transition`
properly.

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_analyzer.py` covers the pure audio→light logic without needing a
live Music Assistant server or real hardware. `bridge.py`/`provider.py`
integrate directly with Music Assistant/Sendspin/Home Assistant internals
and are best exercised against a real running server.

## License

Apache License 2.0 (same as Music Assistant itself — see `LICENSE`). This
project depends on Music Assistant's internal APIs at runtime but contains
no code copied from it.

## Credits

Architecture modeled directly on Music Assistant's built-in **Hue Lights
Sync** and **MilkDrop Visualizer** plugins, and on its built-in **Home
Assistant** plugin for the light-control connection
(music-assistant/server, Apache-2.0). All credit for the underlying
real-time audio analysis pipeline goes to the Music Assistant project.
