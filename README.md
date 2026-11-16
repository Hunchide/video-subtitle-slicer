# video-subtitle-slicer

![License](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-Windows%20%2F%20macOS%20%2F%20Linux-0078D4) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Offline](https://img.shields.io/badge/works-offline-OK-success) ![Agent](https://img.shields.io/badge/agent--agnostic-9cf)

> **Language:** English | [简体中文](README.zh-CN.md)

Turn a finished video (usually vertical, with burned-in subtitles) into **one image per subtitle line**, then compose the lines into 1080×1440 image-text notes for Xiaohongshu/RED, WeChat Official Accounts, and similar platforms.

**OCR-first**: it reads burned-in subtitle text frame by frame, and only treats two lines as the same when their characters match — no missed lines, no duplicates, text identical to the source, and copyable output. Alternatives include edge detection (fast but can miss lines), Whisper ASR (for footage without burned-in subtitles), and plain text cards.

Fully offline, free, and API-key-free. **Pure CLI, agent-agnostic**: any environment that can call Python (local terminal, CI, WorkBuddy, Claude, Codex, or any automation framework) can orchestrate it — no platform lock-in.

---

## What it does

1. **Slice subtitle images**: finished video → `001.jpg / 002.jpg …` + `manifest.json` (OCR text, timecodes, subtitle-bar position, shot type).
2. **Compose caption pages**: pick a line range (`12-17` = one page) → 1080×1440 vertical image with a cover + stacked subtitle bars; or use the "pure caption" route to keep only the subtitle bars.
3. **Build an overview sheet**: stitch dozens of images into one long contact sheet so you can read the whole transcript at a glance.
4. **Export a subtitle list**: a numbered list with timecodes and OCR text, so you can hand-pick the hook lines.

---

## Installation

Requires **Python 3.9+** and **ffmpeg / ffprobe**.

```bash
# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1

# 2. Python dependencies
pip install -r requirements.txt

# 3. Pick one transcription backend (only needed for the ASR route; the OCR route doesn't need it)
#    Apple Silicon : mlx-whisper (enabled by default in requirements, fast on GPU)
#    Windows/Linux : comment out mlx-whisper, uncomment openai-whisper, then pip install
```

### Install ffmpeg (per OS)

| OS | How |
|---|---|
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` (or `dnf` / `pacman`) |
| Windows | Download from [ffmpeg.org](https://ffmpeg.org), unzip, add `bin/` to PATH |

If ffmpeg isn't on PATH, use the `FFMPEG_BIN` / `FFPROBE_BIN` environment variables, or point the script's `--ffmpeg` argument at the executable (on Windows typically `C:\ffmpeg\bin\ffmpeg.exe`).

### Chinese fonts (auto-detected)

Via `_compat.py`, the scripts detect a CJK font per OS: **macOS** STHeiti / PingFang; **Windows** Microsoft YaHei / SimHei / SimSun; **Linux** Noto Sans CJK / WenQuanYi / DejaVu. Falls back to the default font if none is found. Force one with `--font` or the `CJK_FONT` environment variable.

---

## Quick start

```bash
PY=python3
S=./scripts

# 1) Recommended: OCR-scan the subtitles, not a line missed (4fps / 540px-wide frames by default; CPU is fine; ~6 min for a 7-min video)
$PY $S/scan_captions.py "<video>.mp4"
#    Preview the timeline only, no real images (fast)
$PY $S/scan_captions.py "<video>.mp4" --review
#    Cache OCR results so re-tuning params doesn't re-recognize
$PY $S/scan_captions.py "<video>.mp4" --json ocr_cache.json

# 2) Fallback (no OCR available): edge-change detection, fast but can miss lines
$PY $S/slice_subtitles.py "<video>.mp4"

# 3) Write a caption list → hand-write a pagination file → compose caption pages
$PY $S/make_script_list.py --subdir "<video-name>_subtitle-images" --prefix ep1
#    Hand-write pages.txt: one page per line, e.g. "4-7" / "8,9,10" / "12-17"
$PY $S/build_pages.py "<video>.mp4" --subdir "<video-name>_subtitle-images" \
    --pages pages.txt --outdir output --prefix ep1

# 4) Overview contact sheet
$PY $S/make_sheet.py "<video-name>_subtitle-images"
```

`scan_captions.py` writes to `<video-name>_subtitle-images/`, containing `001.jpg` / `002.jpg` … and `manifest.json`.

---

## Scripts

| Script | What it does |
|---|---|
| `scan_captions.py` | **Primary**: OCR reads burned-in subtitles frame by frame, merges by text, never misses a line; outputs subtitle images + manifest |
| `slice_subtitles.py` | Alternative / multi-mode: raw edge detection, hybrid/frame/sentence/textcard (ASR routes). `--backend auto\|mlx\|openai` |
| `make_script_list.py` | Generate a caption list (image strips + Markdown table with index / time / OCR text) |
| `build_pages.py` | Compose captions into 1080×1440 pages: cover + stacked subtitle bars, with large text cards, double-line bars, pagination |
| `pure_caption.py` | Pure-caption route: keep only subtitle bars on a white canvas; whiteboard content handled separately |
| `strip_stack.py` | Low-level horizontal-strip stacking (dependency of build_pages; rarely called directly) |
| `lineup.py` / `batch_lineup.py` | Legacy "background + subtitle bar" collage, 3 lines per image / batch version |
| `compose.py` | Assemble subtitle images into a vertical long image or a grid |
| `make_sheet.py` | Overview contact sheet (N rows per page + index + time) |
| `_compat.py` | **Cross-platform layer**: font detection, ffmpeg paths, Pillow compatibility, dual ASR backends |

> Full arguments for each command: `python <script>.py --help`. The layout decision tree, gotchas, and final tuned parameters are in [SKILL.md](SKILL.md).

---

## manifest.json schema (agent integration interface)

```json
{
  "video": "/abs/path/to/video.mp4",
  "mode": "ocr",
  "fps": 4,
  "count": 12,
  "images": [
    {
      "file": "001.jpg",
      "text": "Subtitle text as read by OCR",
      "kind": "caption",
      "start": 1.20,
      "end": 4.80,
      "bar": [0.70, 0.78],
      "ocr_score": 0.92,
      "scene": "person"
    }
  ]
}
```

Fields:
- `text`: burned-in subtitle text from OCR (ready to reuse as caption copy); `kind=caption` is a subtitle, `kind=slide` is a slide/large-text card with no subtitle, where `text` is only a layout summary.
- `bar`: vertical position of the subtitle bar (fraction of image height), used as an anchor when composing so the wrong strip isn't cut.
- `scene`: `person` (a real person on camera) or `board` (whiteboard/outline page). Cover images for normal caption pages must use a `person` frame.

Typical agent orchestration: ① call `scan_captions.py` for images + manifest → ② read the manifest for text/timecodes to decide copy and pagination → ③ call `build_pages.py` / `pure_caption.py` for final images → ④ call `make_sheet.py` for the overview → ⑤ return image paths to the host (upload / push / display).

---

## Key design decisions

- **Why OCR first**: the earlier "edge change + IoU" guess missed 108 seconds in a 7-minute video (look-alike lines merged, whole segments dropped when the bar jumped). Reading characters has none of these problems — the text is the identity.
- **Double-line subtitles**: two adjacent, horizontally centered OCR boxes with small line spacing are merged into one bar.
- **Representative frame**: pick the frame in the 50%–80% range of the group whose OCR text equals the group's text (not the highest-confidence frame — peak confidence usually lands right as a subtitle changes, biasing output half a line early).
- **Render-then-verify**: after slicing, OCR the image again and compare against the expected text; on mismatch, re-slice at another timestamp (up to 6 tries) to buffer half-frame timestamp error.
- **Backend auto-selection**: `_compat.resolve_asr_backend()` prefers mlx-whisper (Apple GPU), otherwise openai-whisper (CPU). Override with `--backend`.

---

## Known limitations

- OCR can misread occasional characters (rare words, English brands, stylized text). Body-text accuracy is high; for 100% fidelity, trust the image.
- Render-then-verify roughly doubles render time (one extra OCR per image); text-card pages are prone to false "mismatch" reports — inspect the image before judging.
- With rapid multi-speaker dialogue or subtitles flashing for <0.5s, 4fps may miss them; rescan with `--fps 6`.
- Footage without burned-in subtitles needs the ASR route (Whisper listens rather than reads, so output may differ from on-screen subtitles).

---

## License

MIT © Hunchide
