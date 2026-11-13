<div align="center">

# 🎬 HATCUT PRO

### **The Zero-Dependency, Programmable Non-Linear Video Editor (NLE)**

*A powerful, git-diffable timeline editor you can read and master in an afternoon.*

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Stdlib%20Only)-success?style=for-the-badge)](https://github.com/AIwolfie/HatCut)
[![Tests](https://img.shields.io/badge/Tests-103%2F103%20Passing-brightgreen?style=for-the-badge)](https://github.com/AIwolfie/HatCut)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-black?style=for-the-badge)](https://github.com/AIwolfie/HatCut)
[![Engine](https://img.shields.io/badge/Engine-FFmpeg%20Accelerated-orange?style=for-the-badge&logo=ffmpeg&logoColor=white)](https://ffmpeg.org)

<br/>

[![Support](https://img.shields.io/badge/☕%20Support-AIwolfie-ff813f?style=for-the-badge&labelColor=0d1117)](https://aiwolfie.gumroad.com/coffee)
[![PayPal](https://img.shields.io/badge/PayPal-AIwolfie-00457c?style=for-the-badge&logo=paypal&logoColor=white&labelColor=0d1117)](https://paypal.me/AIwolfie)

<br/>

[**🚀 Quick Start**](#-quick-start) •
[**✨ Features**](#-pro-editing-features) •
[**⌨️ Shortcuts**](#-keyboard-shortcuts) •
[**📐 Architecture**](#-architecture) •
[**🐍 Python SDK**](#-python-api) •
[**📖 CLI Reference**](#-cli-reference) •
[**🤝 Credits**](CREDITS.md)

---

</div>

<br/>

## 💡 What is Hatcut?

**Hatcut** is a full-featured, multi-track non-linear video editor built with **zero external Python or JavaScript runtime dependencies**. 

No heavy `pip install`, no bundled Chromium, no `node_modules`. The backend is Python's standard `http.server`, the frontend is three clean native files (HTML, CSS, JS), and the rendering engine uses whatever system `ffmpeg` you already have installed.

Whether you need a **visual studio GUI** on localhost, a **scriptable Python API** for automated video generation, or a **clean CLI** for batch rendering pipelines, Hatcut provides all three on top of a single, human-readable JSON project format.

---

## 🚀 Quick Start

### 1. Verification
Ensure you have Python 3.10+ and FFmpeg on your system:
```console
$ git clone https://github.com/AIwolfie/HatCut.git
$ cd HatCut
$ python -m hatcut doctor
  python    3.13.7  (win32)
  hatcut    0.1.0
  ffmpeg    C:\ffmpeg\bin\ffmpeg.EXE
  ffprobe   C:\ffmpeg\bin\ffprobe.EXE
  encoders  h264: h264_nvenc, h264_qsv, h264_amf, libx264
            hevc: hevc_nvenc, hevc_qsv, hevc_amf, libx265
  ready
```

### 2. Launch the Studio GUI

* **Windows (1-Click)**: Simply double-click **`run_gui.bat`** (or drag & drop any `.hatcut` project file directly onto it).
* **Command Line**:
```console
# Generate demo media & project
$ python examples/make_demo.py

# Launch the dark studio web editor on localhost:8777
$ python -m hatcut serve examples/demo.hatcut
```

The editor opens automatically in your browser at `http://127.0.0.1:8777/`.

---

## ✨ Pro Editing Features

### 🎞 Multi-Track Timeline & Editing Workflow
* **🧲 Magnetic Timeline & Snapping**: Clips and the playhead automatically snap to adjacent clip boundaries, ruler tick marks, and timeline markers within a 10px threshold, accompanied by an active cyan laser guide line.
* **🚩 Timeline Markers (`M` Key)**: Drop colored cue pins (Amber, Green, Blue, Red, Purple, Cyan) with descriptive labels and jump to beats or dialogue cues with a single click.
* **🔲 Multi-Clip Selection & Marquee**: Drag a rubberband selection box or `Shift+Click` multiple clips to move or delete sections together in lockstep.
* **↺ Full Undo / Redo Stack**: Comprehensive history tracking with `Ctrl+Z` and `Ctrl+Y` shortcuts prevents accidental edits.
* **🌊 Live Audio Waveforms**: High-precision amplitude peak extraction renders genuine audio waveforms directly onto timeline audio clips for frame-accurate beat matching.
* **✂ Ripple Deletion & Split**: Split clips instantly at the playhead (`C` or `S`), trim edges with ew-resize handles, and close gaps seamlessly.

---

### 🎭 Motion, Masking & Visual FX
* **📐 Precision Shape & Freeform Masking**:
  * **Presets**: Rectangle, Ellipse / Circle, and Two-Point Gradient Linear Split.
  * **Freeform Polygon Mask**: Click to plot arbitrary polygon masks with live interactive SVG control points directly on the preview monitor.
  * **Controls**: Live feathering, mask inversion, and position shifting.
* **⏱ Transform & Mask Keyframing**:
  * Animate scale, position ($X, Y$), rotation, and opacity smoothly over time using piecewise linear interpolation.
  * Keyframe mask handles over time for moving subjects.
* **🟩 Chroma Key (Green / Blue Screen Removal)**:
  * Remove solid backdrops using FFmpeg's `colorkey` and `despill` filters.
  * Fine-tune color selection, similarity tolerance, edge softness, and spill suppression.
* **🎨 3D LUTs & Preset Color Looks**:
  * Apply industry-standard `.cube` 3D LUT files via `lut3d`.
  * One-click cinematic grading presets: **Film Warm**, **Clean Cold**, **Cyberpunk Neon**, **Vintage Film**, **Noir**, **Teal & Orange**, and **Bleach Bypass**.
* **✨ Multi-Layer Blend Modes**:
  * Layer compositing modes: `screen`, `multiply`, `overlay`, `addition`, `difference`, `darken`, and `lighten`.

---

### 🎵 Audio Engineering & Subtitles
* **🦆 Smart Auto-Ducking**: Sidechain audio compression (`sidechaincompress`) that automatically attenuates background music whenever dialogue or voiceover occurs on designated speech tracks.
* **⚡ Pitch Correction & Retiming**: Shift pitch (-24 to +24 semitones) and preserve natural vocal pitch across speed ramps and tempo retiming.
* **💬 SRT & VTT Subtitle Integration**: Bidirectional subtitle parsing: import `.srt` or `.vtt` caption files directly into timed text tracks, edit them visually, and export back to `.srt`.

---

### 🚀 High-Performance Rendering & Hardware Acceleration
* **GPU Hardware Encoding**: Automatic detection and selection of **NVIDIA NVENC** (`h264_nvenc`, `hevc_nvenc`), **Intel QuickSync (QSV)**, **AMD AMF**, or **Apple VideoToolbox**, falling back to CPU `libx264`/`libx265`.
* **Direct Browser Download & Explorer Reveal**: When export finishes, immediately download the `.mp4` directly in your browser or click **📂 Show in Explorer** to reveal the file on your local drive.
* **Robust Dialect Translation**: Translates and escapes all filter graph expressions, color codes (`0xRRGGBBAA`), and path names to prevent FFmpeg crashes.

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|:---|:---|
| <kbd>Space</kbd> | Play / Pause transport |
| <kbd>Ctrl</kbd> + <kbd>Z</kbd> | Undo timeline mutation |
| <kbd>Ctrl</kbd> + <kbd>Y</kbd> / <kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>Z</kbd> | Redo timeline mutation |
| <kbd>M</kbd> | Add Marker at current playhead |
| <kbd>S</kbd> | Toggle Magnetic Snapping / Split clip |
| <kbd>Delete</kbd> / <kbd>Backspace</kbd> | Delete selected clip(s) |
| <kbd>←</kbd> / <kbd>→</kbd> | Step backward / forward 1 frame (<kbd>Shift</kbd> for 1s) |
| <kbd>Home</kbd> / <kbd>End</kbd> | Jump to start / end of timeline |
| <kbd>Ctrl</kbd> + <kbd>S</kbd> | Save project to disk |

---

## 📐 Architecture

```
HatCut/
├── hatcut/
│   ├── core/              The timeline data model (zero I/O, diffable JSON)
│   │   ├── timeline.py      Tracks, Clips, Markers, TextStyle, Transform, Keyframes
│   │   ├── project.py       Project serialisation, validation, relative media paths
│   │   ├── effects.py       25+ effects registry, chroma key, 3D LUTs, color presets
│   │   ├── subtitles.py     SRT and VTT parsers and exporters
│   │   └── media.py         ffprobe metadata inspection (rasters, codecs, audio)
│   ├── render/            Compiles high-level timeline into an FFmpeg filtergraph
│   │   ├── graph.py         Filtergraph compiler, layer compositing, sidechain ducking
│   │   ├── filters.py       Normalisation chains, piecewise keyframe expressions
│   │   ├── transitions.py   57 xfade transitions across 7 families
│   │   └── pipeline.py      Subprocess execution, progress parsing, cancellation
│   ├── export/            Presets and hardware encoder management
│   │   ├── presets.py       Output configurations (1080p, 4K, vertical 9:16, ProRes, GIF)
│   │   └── encoder.py       NVENC, QSV, AMF, VideoToolbox hardware dialect isolation
│   ├── server/app.py      Standard-library HTTP server, proxy cache, Range streaming
│   └── ui/                The complete dark studio frontend (HTML5 / ES6 / CSS3)
└── tests/                 Comprehensive test suite (103 passing tests)
```

### The Rendering Pipeline

```
  Raw Clip 1 (1080p @ 30fps)             Raw Clip 2 (720p @ 25fps)
          │                                        │
          ├── Per-Clip Normalisation ──────────────┤  (scale → pad → setsar → fps → format=yuv420p)
          ▼                                        ▼
       [vseg1]                                  [vseg2]
          └───────────── Pairwise Join ────────────┘  (xfade for dissolves/wipes, concat for cuts)
                            │
                            ▼
                         [vjoin]  ← Track V1 (Opaque Base)
                            │
                            ├── Overlaid by V2 in RGBA (alpha transparency preserved)
                            ├── Blended via Overlay Blend Modes (screen/multiply/etc.)
                            ├── Dynamic Transform & Mask Keyframing (zoompan / geq)
                            ├── Burned Subtitles & Title Captions (drawtext)
                            ▼
                         [vflat]  → Scale to target preset → Hardware GPU Encode
```

---

## 🐍 Python API

Everything you can do in the GUI can be scripted in Python with no external dependencies:

```python
from hatcut import Project, Clip, TextStyle

# 1. Initialize a 1080p 24fps project
project = Project.new("Cinematic Edit", width=1920, height=1080, fps=24.0)

# 2. Add media to the timeline
v_track = project.timeline.tracks_of("video")[0]
v_clip1, a_clip1 = project.add_media("footage/shot1.mp4", start=0.0)
v_clip2, a_clip2 = project.add_media("footage/shot2.mp4", start=4.0)

# 3. Add a crossfade transition between clips
v_clip2.transition = {"type": "fade", "duration": 0.8}

# 4. Add a title caption
t_track = project.timeline.add_track("text", "Titles")
t_track.clips.append(Clip(
    source="", start=1.0, duration=3.0,
    text=TextStyle(content="SCENE 1", font_size=72, color="#ffffff", box=True)
))

# 5. Save diffable JSON & render
project.save("film.hatcut")
project.timeline.render("web-1080p", "film.mp4")
```

---

## 📖 CLI Reference

Hatcut includes 11 dedicated CLI commands:

```console
$ python -m hatcut --help
  probe     Describe codecs, raster, and audio tracks of any media file
  new       Create a new .hatcut project file
  add       Append video, audio, or image media to tracks
  info      Display structured summary of a project's timeline
  validate  Validate project integrity (missing media, overlaps, invalid settings)
  presets   List all available export presets
  encoders  List detected hardware and software video encoders
  doctor    Verify system environment, Python version, and FFmpeg binaries
  graph     Compile and print the exact FFmpeg command line without rendering
  render    Execute render job with real-time CLI progress bar
  serve     Launch the graphical localhost web editor
```

---

## 📦 Export Presets

| Preset | Format | Resolution | Description |
|:---|:---|:---|:---|
| `web-1080p` | MP4 (H.264) | 1920×1080 | **Default**. Broadly compatible high-profile H.264. |
| `web-720p` | MP4 (H.264) | 1280×720 | Lightweight, fast web export. |
| `web-4k` | MP4 (H.264) | 3840×2160 | Ultra HD 2160p master. |
| `vertical-1080` | MP4 (H.264) | 1080×1920 | 9:16 framing for **TikTok, Reels & Shorts** (auto-cropped). |
| `square-1080` | MP4 (H.264) | 1080×1080 | 1:1 square framing for social feeds. |
| `hevc-1080p` | MP4 (H.265) | 1920×1080 | High efficiency: ~50% smaller file size at equivalent quality. |
| `webm-vp9` | WebM (VP9) | Project Native | Open, royalty-free web video format. |
| `prores` | MOV (ProRes 422) | Project Native | Uncompressed intermediate for DaVinci / Premiere roundtripping. |
| `gif` | Animated GIF | 480p Auto | Two-pass palette-quantized animated GIF. |
| `audio-m4a` / `audio-mp3` | M4A / MP3 | Audio Only | Drops video stream; exports clean master audio. |

---

## 🧪 Testing

Hatcut includes an automated test suite verifying everything from track math and proxy caching to actual FFmpeg filter execution:

```console
# Run the complete test suite
$ python -m pytest
============================ 103 passed in 30.74s =============================

# Fast unit tests (skipping real renders)
$ python -m pytest -m "not slow"
```

---

## ☕ Support the Project

If you find Hatcut useful for your edits, workflows, or pipelines, consider supporting ongoing development:

<div align="center">

[![Support](https://img.shields.io/badge/☕%20Support-AIwolfie-ff813f?style=for-the-badge&labelColor=0d1117)](https://aiwolfie.gumroad.com/coffee)
&nbsp;&nbsp;
[![PayPal](https://img.shields.io/badge/PayPal-AIwolfie-00457c?style=for-the-badge&logo=paypal&logoColor=white&labelColor=0d1117)](https://paypal.me/AIwolfie)

</div>

---

## 📜 License

Hatcut is released under the **[MIT License](LICENSE)**.

*FFmpeg is licensed separately by its own terms (LGPL/GPL) and is neither bundled nor redistributed with this software.*

<div align="center">
<sub>Crafted for engineers, editors, and creators who value simplicity and precision.</sub>
</div>
