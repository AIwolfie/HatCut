# Hatcut

**A non-linear video editor you can read in an afternoon.**

Hatcut is a timeline editor — tracks, clips, trims, splits, crossfades, captions,
animated GIFs, hardware encoding — built as roughly 7,100 lines of Python, HTML, CSS
and JavaScript (plus 800 of tests) with **zero runtime dependencies**. No `pip install`
of a media framework, no bundled binaries, no node_modules. The server is
`http.server`. The front end is three files. The encoder is whatever `ffmpeg` you
already have.

```console
$ git clone <this repo> && cd Hatcut
$ python -m hatcut doctor
  python    3.13.7  (win32)
  hatcut    0.1.0
  ffmpeg    C:\ffmpeg\bin\ffmpeg.EXE
  ffprobe   C:\ffmpeg\bin\ffprobe.EXE
  encoders  h264: h264_nvenc, h264_qsv, h264_amf, libx264
            hevc: hevc_nvenc, hevc_qsv, hevc_amf, libx265
  ready

$ python examples/make_demo.py
$ python -m hatcut serve examples/demo.hatcut
  Hatcut editor on http://127.0.0.1:8777/
    project: examples\demo.hatcut
    ctrl-c to stop
```

Or on Windows, simply double-click **`run_gui.bat`** (or drag and drop a `.hatcut` file onto it) to launch the GUI immediately!

That is the whole setup. If `doctor` says `ready`, you have an editor.

---

## Starting the GUI Editor

You can start the graphical editor in multiple ways:

### 1. Windows Shortcut (.bat)
Double-click `run_gui.bat` in the repository root.
- Automatically verifies your Python and FFmpeg setup.
- If no demo project exists, it generates one automatically and opens it in your default web browser.
- You can also **drag-and-drop** any `.hatcut` project file directly onto `run_gui.bat` to launch that project.

### 2. Command Line
```console
# Launch on an existing project (or opens demo if passed)
python -m hatcut serve examples/demo.hatcut

# Or launch on an empty/new project
python -m hatcut serve my_project.hatcut

# Custom port or headless (no auto-open browser)
python -m hatcut serve edit.hatcut --port 9000 --no-browser
```
The editor will be live at `http://127.0.0.1:8777/`.

---

## Why this exists

Every serious NLE is a GUI wrapping a filter graph. The interesting engineering is
never the buttons — it is the part where a 1280×720 30fps stereo clip has to be cut
against a 640×640 25fps mono one and come out as a single coherent stream. That
problem is real, it is hard, and it is almost always hidden behind an installer.

Hatcut puts it in a text file you can diff.

- **The project format is JSON**, human-readable and version-controlled. A cut is
  a reviewable change request.
- **The render is a command line.** `hatcut graph` prints the exact ffmpeg
  invocation before anything runs. Nothing is opaque.
- **The editor is a web page** served from localhost, so it works over SSH and
  has no install step.

---

## The three-minute tour

```console
# What is in this file, really?
$ python -m hatcut probe footage/interview.mov
  interview.mov
    path      footage/interview.mov
    format    mov
    duration  252.48s  (4:12.480)
    video     hevc 3840x2160 @ 23.976fps
    audio     aac 48000Hz 2ch
    size      1.42 GB

# Start a project and drop clips into it.
$ python -m hatcut new edit.hatcut --name "Launch film" --size 1920x1080 --fps 24
  created edit.hatcut  (1920x1080 @ 24fps)
$ python -m hatcut add edit.hatcut footage/interview.mov
  interview.mov -> 0.00s +252.48s (video+audio)
saved edit.hatcut  total 252.48s
$ python -m hatcut add edit.hatcut footage/broll.mp4 --at 12.5
  broll.mp4 -> 12.50s +8.00s (video+audio)
saved edit.hatcut  total 252.48s

# Look before you leap: this compiles the graph and shows you the command.
$ python -m hatcut graph edit.hatcut --preset web-1080p
$ python -m hatcut validate edit.hatcut
  edit.hatcut: no problems found

# Render it.
$ python -m hatcut render edit.hatcut --preset web-1080p -o launch.mp4
  rendering 252.48s of timeline -> launch.mp4 (Web 1080p (H.264))
  [############################] 100.0%  252.5s
  wrote launch.mp4  (184.2 MB, 252.5s, 4.12x realtime)
```

Then open the graphical editor on the same project:

```console
$ python -m hatcut serve edit.hatcut
```

---

## The editor

`hatcut serve` starts a single-threaded HTTP server on `127.0.0.1:8777` and opens
a browser. What you get:

| | |
|---|---|
| **Timeline** | Multi-track, drag to move, drag edges to trim, split at the playhead, ripple delete. |
| **Magnetic Snapping** | Magnetic clip and playhead snapping to clip edges, markers, and ruler ticks with a vertical guide line and one-click toggle. |
| **Timeline Markers** | Drop colored markers (`M` key) with timestamps and notes on the ruler for cueing beats and speech. |
| **Multi-Clip Selection** | Shift-click or rubberband drag-to-select to batch-move or batch-delete entire sections in lockstep. |
| **Undo / Redo** | Client-side command history stack with `Ctrl+Z` / `Ctrl+Y` shortcuts and topbar undo/redo buttons. |
| **Audio Waveforms** | High-precision amplitude peak extraction rendered directly onto timeline audio clips for beat-accurate cutting. |
| **Masking** | Shape masks (Rectangle, Ellipse/Circle, Linear Split) and Freeform Polygon masks with feathering, inversion, and interactive handles on the preview stage. |
| **Keyframe Animation** | Keyframable mask parameters as well as Transform parameters (scale, pan x/y, rotation, opacity) with piecewise interpolation. |
| **Chroma Key** | Green screen / blue screen background removal with tolerance, softness, and spill suppression. |
| **Blend Modes** | Track and clip overlay blend modes (`screen`, `multiply`, `overlay`, `addition`, `difference`, `darken`, `lighten`) via FFmpeg `blend`. |
| **Subtitles (SRT / VTT)** | Direct import of `.srt` and `.vtt` into dedicated timed caption tracks, and export back to `.srt`. |
| **Audio Auto-Ducking** | Sidechain compression on background music tracks that automatically dips music volume when voice/dialogue speaks. |
| **3D LUTs & Color Looks** | Full `.cube` 3D LUT grading support, plus one-click cinematic looks (Film Warm, Clean Cold, Cyberpunk, Vintage, Noir, Teal & Orange). |
| **Pitch & Retiming** | Pitch shift (-24 to +24 semitones) and natural pitch-preserving audio tempo retiming. |
| **Transitions** | 57 of them across 7 families — dissolves, wipes, slides, shapes, motion — plus audio crossfades. |
| **Preview** | Real compositing — stacked picture layers, CSS-transformed positions, live mask clip-paths, and playhead tracking. |
| **Scrubbing** | Bound to a 480p proxy generated on demand and cached, so a 4K HEVC source scrubs like a 480p H.264 one. |
| **Bin** | Browse the filesystem, probe on import, drag onto a track. |
| **Inspector** | Per-clip trim, speed, volume, position, scale, opacity, masks, keyframes, transitions. |
| **Captions** | Text clips with font, size, colour, expression-based positioning and an optional box. |
| **Portable Projects** | Media paths are automatically stored relative to the `.hatcut` project file so projects can be moved across drives or machines seamlessly. |
| **Export** | Preset picker, hardware/software encoder choice, live progress, cancel that actually stops ffmpeg. |

Everything the UI does goes through the same HTTP API documented below, so
anything you can click, you can script.

### On preview fidelity

The preview is **not** a rendered frame and does not pretend to be. It composites
`<video>` elements with CSS transforms, which means it is instant and honest about
geometry, timing, layering and captions — but it cannot show you an `xfade`, a
blur, or a GPU pixel shader. Positions given as ffmpeg expressions that the browser
cannot evaluate fall back to centred **and say so**, rather than silently lying:

> preview cannot evaluate the text position "if(gt(t,2),100,200)" — centring it;
> the render still uses it exactly

The render is the source of truth. The preview is a very good sketch of it.

---

## Architecture

```
hatcut/
├── ffmpeg.py        locating ffmpeg/ffprobe, probing encoders and filters
├── cli.py           the 11 subcommands, progress bar, exit codes
├── core/            the model — no ffmpeg, no I/O beyond reading a project
│   ├── timeline.py    Track, Clip, TextStyle, split, ripple, overlap rules
│   ├── project.py     Project, save/load, media registration, validation
│   ├── media.py       ffprobe → MediaInfo (codecs, raster, rate, channels)
│   ├── effects.py     22 effects + the registry that builds them
│   └── errors.py      HatcutError and friends
├── render/          the model → ffmpeg
│   ├── graph.py       the filtergraph compiler  ← the interesting file
│   ├── filters.py     normalisation chains (scale, fps, sar, pix_fmt, audio)
│   ├── transitions.py xfade / acrossfade argument construction
│   └── pipeline.py    jobs, subprocess, progress parsing, cancellation
├── export/
│   ├── presets.py     12 output presets
│   └── encoder.py     hardware detection + per-encoder dialect handling
├── server/app.py    local HTTP API, proxy & thumbnail caches, Range serving
└── ui/              index.html, style.css, app.js — the entire front end
```

### How a timeline becomes a command

The central problem: `concat` and `xfade` both reject inputs whose size, frame
rate, pixel format or channel layout disagree — and a real edit is *nothing but*
disagreeing inputs. So the compiler works bottom-up:

```
  raw sources (1280x720@30 stereo)   (640x640@25 mono)
        │                                   │
        ├── per-clip normalisation ─────────┤   scale → pad → setsar → fps
        │                                   │   → format=yuv420p
        ▼                                   ▼
   [vseg1]                             [vseg2]
        └────────── join pairwise ──────────┘   concat for hard cuts,
                    │                            xfade for crossfades
                    ▼
               [vjoin4]  ← track V1
                    │
                    ├── overlaid by V2 in RGBA     (gaps stay transparent,
                    │                               not black)
                    ├── then V3, V4 …
                    ├── then text tracks burned last
                    ▼
                 [vflat]  → drawtext → scale to preset → encode
```

Two decisions carry most of the weight:

**Joining pairwise instead of all at once.** `concat=n=5` is one filter and much
simpler — but it cannot express a crossfade anywhere in the middle. Reducing
two streams to one, repeatedly, lets a track interleave hard cuts and crossfades
freely, because each step consumes exactly two streams and emits one. The cost is
a longer graph; the payoff is that transitions are not a special case.

**Overlay layers carry alpha.** The bottom video track is the opaque base. Every
track above it is composited in RGBA, so a gap in V2 shows V1 through it instead of
punching a black hole. This is also why letterbox padding is transparent rather
than black on overlay layers, and why the base track is the only one padded to the
full timeline length.

### Effects and transitions are registries, not switch statements

Each clip carries an ordered `effects: list[dict]`. A registry in `core/effects.py`
maps a name to a builder that emits ffmpeg filters, plus metadata — parameter
ranges, defaults, a category — that is enough to generate a UI without the UI
knowing anything about the effect. The transitions catalog works the same way and
is considerably larger than "crossfade": 57 entries across 7 families.

```console
$ python -c "from hatcut.core import effects; print(sorted(effects.REGISTRY))"
['audio_fade_in', 'audio_fade_out', 'blur', 'brightness', 'contrast', 'crop',
 'fade_in', 'fade_out', 'gain', 'gamma', 'grayscale', 'hue', 'invert', 'mirror',
 'opacity', 'rotate', 'saturation', 'sepia', 'sharpen', 'speed', 'vignette',
 'vignette_soft']
```

Five categories — `color`, `blur`, `stylise`, `transition`, `audio`. Effects
compile into the same per-clip chain as normalisation, and the compiler collapses
*runs of adjacent identical filters* before emitting — normalisation and the
effect pass can both ask for the same pixel format, and `format=yuv420p` twice in
a row is pure noise. It does not merge distinct effects: brightness and contrast
are two different filters and remain two passes.

### Audio is built the same way

Each audio track compiles exactly like a video track — pairwise join, transitions
honoured — then the tracks are mixed. Clip volume, speed, and `atrim`/`apad` are
applied per segment. The crossfade math is deliberately shared with the video path
so the two cannot drift apart.

### Encoder dialects are filtered, not translated

`-preset ultrafast` is an x264 word. `h264_nvenc` has presets too, but they are
called `p1`…`p7`, and `-tune hq` means something else again. Handing an x264
preset name to NVENC does not degrade gracefully — the render dies outright:

```
[h264_nvenc] Unable to parse "preset" option value "ultrafast"
```

So `encoder.py` distinguishes two kinds of ffmpeg flags:

| Kind | Examples | Handling |
|---|---|---|
| **Encoder-private vocabulary** | `-preset`, `-tune` | Stripped when the chosen encoder is hardware. Each encoder contributes its own instead. |
| **Shared flags** | `-pix_fmt`, `-movflags`, `-profile:v` | Passed through unchanged — these mean the same thing everywhere. |
| **Quality** | `-crf` (x264/x265), `-cq` (NVENC), `-global_quality` (QSV), `-q:v` (VideoToolbox) | Not shared: the flag *name* differs per family, so `presets.py` never sets one directly — `encoder.py` does. |

NVENC gets `-rc vbr -cq N -b:v 0 -preset p5 -tune hq`; QSV gets
`-global_quality N -look_ahead 1`; AMF gets `-rc cqp -qp_i N -qp_p N`; VideoToolbox
gets an inverted quality scale. Software falls back to `-crf N -preset medium`.

There is a regression test for precisely this, because the bug shipped once:

```python
def test_the_preview_preset_is_safe_on_every_encoder():
    extra = presets.get("preview").extra
    assert "-preset" in extra, "fixture drift: preview no longer sets a preset"
    for name in sorted(encoder.HARDWARE):
        kept = encoder.filter_extra(extra, choice(name))
        assert "-preset" not in kept
        assert "-tune" not in kept
```

---

## Project format

A `.hatcut` file is JSON, versioned, and diffable.

```json
{
  "format": "hatcut",
  "version": 1,
  "name": "Launch film",
  "settings": { "width": 1920, "height": 1080, "fps": 24.0, "sample_rate": 48000 },
  "tracks": [
    {
      "kind": "video", "name": "V1",
      "clips": [
        { "id": "clip_69a64ac0df", "source": "footage/interview.mov",
          "link_id": "link_a681cb758b",
          "start": 0.0, "duration": 12.5, "in_point": 3.25,
          "speed": 1.0, "volume": 1.0, "enabled": true,
          "transition": { "type": "fade", "duration": 0.75 } }
      ]
    },
    { "kind": "text", "name": "T1", "clips": [
        { "id": "clip_a462df2800", "source": "", "start": 14.0, "duration": 3.0,
          "text": { "content": "Chapter One", "font_size": 64,
                    "color": "white", "x": "(w-text_w)/2", "y": "(h-text_h)/2",
                    "box": true, "box_color": "black@0.5" } }
    ]}
  ],
  "media_cache": { "footage/interview.mov": { "duration": 252.48, "width": 3840, ... } }
}
```

Some notes that are not obvious from the shape:

- **`in_point` is in source time, `start` is in timeline time.** Trimming the left
  edge of a sped-up clip moves `in_point` by `offset × speed`, not `offset`.
- **`transition` belongs to the clip it enters**, describing the join *before* it.
  The first clip on a track has nothing to fade from, so its transition is ignored.
- **`media_cache` is a cache, not truth.** It is probed data kept for fast loads,
  keyed by the `source` string exactly as you wrote it. Delete it and Hatcut
  re-probes; nothing breaks.
- **`link_id` groups a picture clip with its sound.** Importing an A/V file creates
  two clips sharing one `link_id`. What the link actually propagates is narrower
  than the name suggests: **`set_speed` retimes both**, and setting a `transition`
  on one copies it to the other — because a picture that dissolves while its sound
  hard-cuts drifts apart. It does **not** propagate moves or trims; see the known
  issues.
- **A still is a picture with no intrinsic duration** (`is_image` is
  `has_video and duration <= 0.0`), so a still clip *must* carry an explicit
  `duration` — that is where its length comes from.
- **`source` paths are as you gave them.** Relative paths resolve against your
  working directory, so a project is portable only if you keep it that way.

---

## CLI reference

```
hatcut probe     describe media files
hatcut new       create an empty project
hatcut add       append media to a project
hatcut info      show a project's contents
hatcut validate  check a project for problems
hatcut presets   list export presets
hatcut encoders  show usable video encoders
hatcut doctor    check that ffmpeg is wired up correctly
hatcut graph     print the compiled filtergraph
hatcut render    render a project
hatcut serve     open the web editor
```

`hatcut graph` is the one worth knowing. It compiles everything, prints the exact
command ffmpeg would receive, and runs nothing — the fastest way to understand
what the editor is actually doing, and the fastest way to file a good bug report.

### Presets

| | Output |
|---|---|
| `web-1080p` | mp4, 1920×1080, crf 20 — the safe default |
| `web-720p` / `web-4k` | mp4, H.264 |
| `vertical-1080` | mp4, 1080×1920 — TikTok / Reels / Shorts, crops to fill |
| `square-1080` | mp4, 1080×1080 — feed posts |
| `hevc-1080p` | mp4, H.265, crf 23 — about half the size, slower |
| `webm-vp9` | webm, royalty-free |
| `gif` | animated GIF, palette-quantised, no audio |
| `audio-m4a` / `audio-mp3` | audio only, video stream dropped |
| `prores` | mov, ProRes 422 mezzanine |
| `preview` | mp4, 854×480, crf 30, 24fps, `ultrafast` |

### Environment

| Variable | Effect |
|---|---|
| `HATCUT_FFMPEG` | Path to a specific `ffmpeg` binary. Overrides PATH — useful for pinning a build per project. |
| `HATCUT_FFPROBE` | Same, for `ffprobe`. |
| `HATCUT_CACHE` | Where proxies and thumbnails live. Default `~/.hatcut/cache`. |

If `ffmpeg` is not on PATH, Hatcut also searches `C:\ffmpeg` and
`C:\Program Files\ffmpeg` on Windows, because that is where it usually ends up.

---

## HTTP API

The editor is a client of this API, not a privileged part of it. Every route is
plain JSON over localhost.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/state` | Everything the editor needs: the `timeline` (settings + tracks + clips), `media_cache`, `presets`, `encoders`, the `transitions` and `effects` catalogs, `duration`, `problems`, `missing_media`, `renders` |
| `GET` | `/api/presets` | Available export presets |
| `GET` | `/api/encoders` | Detected encoders and which are hardware |
| `GET` | `/api/project` | The raw project file |
| `POST` | `/api/project/save` | Write the project to disk |
| `POST` | `/api/settings` | Raster, frame rate, sample rate |
| `GET` | `/api/browse?path=` | List a directory |
| `GET` | `/api/probe?path=` | Probe one file |
| `POST` | `/api/import` | Register media and place it |
| `GET` | `/api/proxy?path=` | Serve a 480p proxy (Range-capable) |
| `GET` | `/api/thumb?path=&t=` | Serve a thumbnail |
| `POST`/`DELETE` | `/api/track` | Add / remove a track |
| `POST` | `/api/text` | Add a text clip |
| `POST`/`DELETE` | `/api/clip/<id>` | Edit / remove a clip |
| `POST` | `/api/clip/<id>/split` | Split a clip at a timeline position |
| `POST` | `/api/render` | Start a render |
| `GET` | `/api/render` | Poll render progress |
| `GET`/`DELETE` | `/api/render/<id>` | Status / cancel |

Proxies and thumbnails are content-keyed caches under `HATCUT_CACHE` — the key
hashes the source path, its mtime and size, and the target geometry, so editing a
file on disk invalidates its proxy automatically. Generation takes a single-flight
lock per key, so two browser tabs asking for the same proxy produce one ffmpeg run
rather than two. `/api/proxy` implements HTTP Range requests (`206`,
`Content-Range`, `Accept-Ranges: bytes`) — without that a `<video>` element cannot
seek, which is exactly the kind of thing that makes a "working" editor feel broken.

---

## Testing

```console
$ python -m pytest -q
73 passed, 1 xfailed in 4.51s

$ python -m pytest -m "not slow"      # skip real ffmpeg renders
$ python -m pytest -m slow            # only real renders
```

The fixture is synthetic and deliberately hostile: `conftest.py` builds two clips
with **nothing in common** — different raster, frame rate, sample rate and channel
count — because agreement is the case that already works.

```python
CLIP_SPECS = {
    "wide": dict(size="1280x720", rate=30, sample_rate=44100, channels=1, freq=440),
    "tall": dict(size="640x640",  rate=25, sample_rate=48000, channels=2, freq=660),
}
```

The `slow` tests are the ones that matter most: they run ffmpeg for real and probe
the output, because a filtergraph that compiles is not a filtergraph that works.
They assert things like "a 3s still renders as 3s of video" and "cancelling a
render leaves no partial file" — a truncated mp4 is worse than no file, because it
looks finished and plays as garbage.

---

## Known issues

**A transition leaves a black tail.** This is the one real open bug, and it is
pinned by a `strict=True` xfail rather than papered over.

A 0.5s crossfade between two 2s clips should land at 3.5s, as it does in Premiere
and Resolve. Hatcut renders 4.0s: the `xfade` correctly overlaps the two clips and
produces 3.5s, but the base layer is then padded back out to `Timeline.duration`
with genuine `color=c=black` and the audio is `apad`-ed to match. The result is
correctly in sync — no drift — but the second clip sits 0.5s earlier than the
timeline says and half a second of black is appended.

The fixture that catches it:

```python
@pytest.mark.xfail(strict=True, reason="…")
def test_a_crossfade_does_not_leave_a_black_tail(self, tmp_path, project, wide, tall):
    …
    assert probe(output).duration == pytest.approx(3.5, abs=0.4)
```

The fix is a product decision, not a code decision: **should a transition shorten
the timeline?** Premiere and Resolve say yes — the overlap consumes time from both
neighbours. The other coherent answer is that a transition preserves the timeline's
length and the clips are re-timed under it. Both are defensible; they are
incompatible; the existing `xfade` offset math already implements the first.
`Timeline.duration` is the thing that has to change either way, which is why the
fix ripples into `next_free_start`, overlap validation, `ripple_delete` and
`split_clip`. Right now Hatcut renders 4.0s, which is *a* consistent answer, just
not the standard one.

Other known gaps, stated plainly:

- **The preview cannot show filters it cannot express in CSS.** Crossfades, blurs
  and colour work show as hard cuts in the browser. Render to see them.
- **No undo, no redo.** The workspace autosaves the project on mutation; there is
  no history stack, so an accidental ripple delete is gone.
- **One render at a time.** `RenderManager` runs jobs on background threads but
  serialises them, so two exports queue rather than share the machine.
- **Source paths are not made relative to the project.** A project written with
  absolute paths stays absolute — see the format notes above.
- **Effects are JSON-only.** All 22 are implemented and reach the graph, and
  `/api/state` already ships the full parameter metadata for every one — but no
  surface *sets* them yet: not the inspector, not the CLI, not `/api/clip/<id>`
  (which patches `start`, `duration`, `in_point`, `speed`, `volume`, `enabled`
  and `name`). Today you add effects by editing `clip.effects` in the project
  file. Wiring the controls is mostly UI work; the hard part is done.
- **Linked clips are half-linked.** `link_id` propagates speed and transitions but
  not position, so `move_clip` on the picture leaves the sound where it was and the
  two silently desync. Trimming is the same. Making moves propagate is a small fix;
  it is not done, so move both halves deliberately.

---

## Design constraints

These were choices, not omissions.

**Zero dependencies.** `pip install hatcut` pulls an empty list, and the reason is
that the interesting part of a video editor is the filtergraph, not the dependency
resolution. It also means the whole thing runs from a checkout with no build step,
and will still run in five years.

**The preview is a sketch; the render is the truth.** Rather than reimplement
ffmpeg's pixel pipeline in JavaScript — and get it subtly wrong forever — the
preview composites correctly at the level the DOM can be honest about, and warns
when it cannot. Geometry, timing, layering and captions are trustworthy. Filters
are not.

**`ffmpeg` is a hard dependency and is not bundled.** Bundling it would add
hundreds of megabytes and a licence surface to a project whose entire pitch is
that you can read it in an afternoon. If you have ffmpeg, you have Hatcut; if you
don't, `doctor` tells you in one line.

**Text goes through a file, not through `drawtext`'s escaping rules.** A caption
containing a colon, a percent sign, or any non-Latin script will break an inline
`text=` argument in ways that are genuinely hard to get right. Hatcut writes a
content-addressed `.txt` into the system temp directory and passes `textfile=`.
This is also why the file is not written to the current directory.

---

## Provenance

Hatcut was built from scratch, after a static analysis of a proprietary mobile
editor's APK. That analysis is where the problem statement came from — raster
mismatch, transition semantics, encoder dialects, proxy caches — but **no code from
that application, and no attempt to modify, patch, or circumvent it, is part of
this project.** Hatcut is an independent implementation against `ffmpeg`. It
contains no reverse-engineered logic, no bypassed licensing, and no integrity
controls to defeat. It is new code under the MIT licence.

If you were looking for something that unlocks someone else's paid features: that
is not here, and it is not what this is.

---

## Licence

MIT. See [LICENSE](LICENSE).

`ffmpeg` is licensed separately by its own terms — LGPL or GPL depending on the
build — and Hatcut neither bundles nor redistributes it.
