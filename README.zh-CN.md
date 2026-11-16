# video-subtitle-slicer

> **Language:** [English](README.md) | 简体中文

![License](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-Windows%20%2F%20macOS%20%2F%20Linux-0078D4) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Offline](https://img.shields.io/badge/works-offline-OK-success) ![Agent](https://img.shields.io/badge/agent--agnostic-9cf)

把「成片视频」（多为竖屏、已烧录字幕）切成**每条字幕一张图**，再拼成 1080×1440 图文笔记（小红书 / 公众号等）。

**首选 OCR 路线**：逐帧读取画面里的烧录字幕文字，**同字才算同一句** —— 不漏句、不重复、文字与原片一致，且能拿到可复制的文本。另有边缘检测（快但会漏）、whisper ASR（无字幕原片）、纯文字卡等备选路线。

离线、免费、不需要任何 API Key。**纯命令行、agent 无关**：任何能调用 Python 的环境（本地终端 / CI / WorkBuddy / Claude / Codex / 任意自动化框架）都能直接编排，不绑定任何特定平台。

---

## 能做什么

1. **切字幕图**：成片 → 每条字幕一张 `001.jpg / 002.jpg …` + `manifest.json`（含 OCR 原文、时间码、字幕条位置、镜头类型）。
2. **拼台词**：挑台词区间（`12-17` 一行 = 一页）→ 出 1080×1440 竖图，头图 + 字幕条竖堆；或「纯字幕」路线只留字幕条。
3. **出总览**：几十张图拼成「字幕总览」长图，一眼读完整个逐字稿。
4. **字幕清单**：导出带序号 / 时间码 / OCR 原文的清单，人工挑句留钩子。

---

## 安装

需要 **Python 3.9+** 与 **ffmpeg / ffprobe**。

```bash
# 1. 建虚拟环境
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1

# 2. Python 依赖
pip install -r requirements.txt

# 3. 听写后端二选一（只有走 ASR 路线才需要；OCR 路线不需要）
#    Apple Silicon : mlx-whisper（requirements 默认启用，GPU 快）
#    Windows/Linux : 注释掉 mlx-whisper，放开 openai-whisper 那行，再 pip install
```

### 安装 ffmpeg（按系统）

| 系统 | 方式 |
|---|---|
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg`（或 `dnf` / `pacman`） |
| Windows | [ffmpeg.org](https://ffmpeg.org) 下载解压，`bin/` 加入 PATH |

不在 PATH 时，用环境变量 `FFMPEG_BIN` / `FFPROBE_BIN`，或脚本 `--ffmpeg` 参数指向可执行文件（Windows 常见 `C:\ffmpeg\bin\ffmpeg.exe`）。

### 中文字体（自动探测）

脚本通过 `_compat.py` 按系统自动找中文字体：**macOS** 华文黑体 / 苹方；**Windows** 微软雅黑 / 黑体 / 宋体；**Linux** Noto Sans CJK / 文泉驿 / DejaVu。找不到时退化为默认字体。可用 `--font` 或环境变量 `CJK_FONT` 强制指定。

---

## 快速开始

```bash
PY=python3
S=./scripts

# ① 首选：OCR 扫字幕，一句不漏（默认 4fps / 540 宽抽帧，CPU 即可；7 分钟片约 6 分钟）
$PY $S/scan_captions.py "<成片>.mp4"
#    先只看时间轴、不写真图（很快）
$PY $S/scan_captions.py "<成片>.mp4" --review
#    缓存 OCR 结果，调参重出图不用重识别
$PY $S/scan_captions.py "<成片>.mp4" --json ocr_cache.json

# ② 备选（无 OCR 环境）：边缘变化检测，快但会漏句
$PY $S/slice_subtitles.py "<成片>.mp4"

# ③ 写台词清单 → 人工写分页文件 → 拼台词出图
$PY $S/make_script_list.py --subdir "<成片名>_字幕图" --prefix 上期
#    手写 pages.txt：一行一页，如 "4-7" / "8,9,10" / "12-17"
$PY $S/build_pages.py "<成片>.mp4" --subdir "<成片名>_字幕图" \
    --pages pages.txt --outdir 成图 --prefix 上期

# ④ 字幕总览长图
$PY $S/make_sheet.py "<成片名>_字幕图"
```

`scan_captions.py` 输出 `<成片名>_字幕图/`，内含 `001.jpg` / `002.jpg` … 和 `manifest.json`。

---

## 脚本一览

| 脚本 | 作用 |
|---|---|
| `scan_captions.py` | **首选**：OCR 读烧录字幕，逐帧识别、按文字归并，一句不漏，产出字幕图 + manifest |
| `slice_subtitles.py` | 备选/多模式：raw 边缘检测、hybrid/frame/sentence/textcard（ASR 路线）。`--backend auto\|mlx\|openai` |
| `make_script_list.py` | 生成台词清单（图片条 + Markdown 表格，带序号 / 时间 / OCR 原文） |
| `build_pages.py` | 拼台词成 1080×1440 页：头图 + 字幕条竖堆，支持大字卡、双行条、分页 |
| `pure_caption.py` | 纯字幕路线：只抠字幕条贴纯白画布，板书另做 |
| `strip_stack.py` | 底层横带摞图（build_pages 依赖，一般不强调用） |
| `lineup.py` / `batch_lineup.py` | 旧版每 3 句一张「底图+字幕条」拼图 / 批量版 |
| `compose.py` | 把字幕图拼成竖排长图或宫格 |
| `make_sheet.py` | 字幕总览长图（每页 N 行 + 序号 + 时间） |
| `_compat.py` | **跨平台兼容层**：字体探测、ffmpeg 路径、Pillow 兼容、ASR 双后端 |

> 每条命令的完整参数：`python <脚本>.py --help`。拼台词的版式决策树、踩坑与定稿参数见 [SKILL.md](SKILL.md)。

---

## manifest.json 结构（agent 集成接口）

```json
{
  "video": "/abs/path/to/成片.mp4",
  "mode": "ocr",
  "fps": 4,
  "count": 12,
  "images": [
    {
      "file": "001.jpg",
      "text": "OCR 读出的字幕原文",
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

字段说明：
- `text`：OCR 读出的烧录字幕原文（拼台词文案可直接用）；`kind=caption` 是字幕，`kind=slide` 是无字幕的幻灯片/大字卡，`text` 只是版面摘要。
- `bar`：字幕条纵向位置（占高比例），拼图时作锚点，避免切错横带。
- `scene`：`person`（真人出镜）或 `board`（板书/提纲页）。普通拼台词头图须选 `person` 帧。

Agent 典型编排：① 调 `scan_captions.py` 出图 + manifest → ② 读 manifest 拿文本/时间码决定文案与分页 → ③ 调 `build_pages.py` / `pure_caption.py` 出成图 → ④ 调 `make_sheet.py` 出总览 → ⑤ 把图片路径回传宿主（上传 / 推送 / 展示）。

---

## 关键设计

- **为什么 OCR 优先**：早先靠「边缘变化 + IoU」猜换句，实测 7 分钟片漏 108 秒（字形相近句被并、字幕条位置跳变整段漏）。读字就没有这些问题——文字即身份。
- **双行字幕**：相邻、水平居中、行距小的两个 OCR 框合并成一条。
- **代表帧**：取组内 50%~80% 区间、且该帧 OCR 文字等于整组的那一帧（不选置信度最高帧——最高分常在字幕刚切换时，出图系统性偏早半句）。
- **出图回读校验**：截完把图再 OCR 一遍与期望文字比对，不匹配换时间点重截（最多 6 次），兜底时间戳半帧误差。
- **后端自适应**：`_compat.resolve_asr_backend()` 优先 mlx-whisper（Apple GPU），否则 openai-whisper（CPU）。`--backend` 可强制。

---

## 已知限制

- OCR 会读错个别字（生僻词、英文品牌、花字）；中文正文准确率很高，要 100% 原文以图片为准。
- 回读校验让出图变慢约一倍（每张多一次 OCR）；图文卡页易被误报「对不上」，先看图再判断。
- 多人快速对话 / 字幕 <0.5s 一闪而过时 4fps 可能采不到，可 `--fps 6` 重扫。
- 无烧录字幕的原片需走 ASR 路线（whisper 是听不是看，与原字幕可能有出入）。

---

## License

MIT © Hunchide
