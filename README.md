# video-subtitle-slicer

![License](https://img.shields.io/badge/license-MIT-green) ![Platform](https://img.shields.io/badge/platform-macOS%20%2F%20Apple%20Silicon-000) ![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![Offline](https://img.shields.io/badge/works-offline-OK-success) ![WorkBuddy](https://img.shields.io/badge/WorkBuddy-skill-9cf)

把「成片视频」（多为竖屏、已烧录字幕）切成多张图片，**每图一句完整字幕**，方便拼台词做成图文笔记（小红书 / 公众号等）。

- **默认 `raw` 模式**：不靠语音识别、不需要 OCR，直接用「字幕行边缘变化检测」逐条抓画面里烧录的字幕 —— 一套图按时间顺序连起来就是**完整逐字稿**，文字与原片 100% 一致。
- 其余模式用 `mlx-whisper` 词级时间戳听写切句，适合无烧录字幕的纯口播原片，或需要叠「完整台词」的场合。

离线、免费、不需要任何 API Key。Apple Silicon 上走 GPU 听写。

---

## 五种模式

| 模式 | 产出 | 什么时候用 |
|---|---|---|
| `raw`（默认） | **检测烧录字幕换行，每条字幕一张图**；画面原样，不重绘 | 成片有烧录字幕（绝大多数情况）；要完整逐字稿 |
| `hybrid` | 上半视频画面 + 底部模糊底条叠 **ASR 完整台词** | 想要「一句完整话」压在一张图上，不介意叠字 |
| `frame` | 视频帧 + 脚本叠加字幕（半透明条 + 白字描边） | 原片**没有**烧录字幕（纯口播） |
| `sentence` | 按 ASR 整句截中点帧（一句话一张，粒度粗） | 只想每句话一张、不要逐条字幕 |
| `textcard` | 纯文字卡（浅底 + 大字台词 + 时间码），不带画面 | 想要最干净、零歧义的排版 |

---

## 安装依赖

需要 Python 3.9+ 与 `ffmpeg`（Homebrew 装即可）。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg        # 如未安装
```

> **听写后端**：脚本默认用 `mlx-whisper`（仅 Apple Silicon 走 GPU）。
> 非 Apple Silicon 机器请改用 `openai-whisper`：把脚本顶部
> `MODEL = "mlx-community/whisper-large-v3-turbo"` 与 `import mlx_whisper`
> 替换为对应的 `openai-whisper` 调用即可（逻辑不变）。

中文字体：脚本默认尝试 macOS 系统字体（华文黑体 / 苹方）。**非 macOS**
请通过 `--font` 指向本机中文字体（如 `Noto Sans CJK SC`），否则会退化为无字幕样式。

---

## 用法

```bash
PY=python3                      # 或激活 venv 后直接 python
S=./scripts

# 默认 raw：检测烧录字幕换行，每条字幕一张图（整套 = 完整逐字稿）
$PY $S/slice_subtitles.py "<成片>.mp4"

# 字幕行位置不准时先校准（纵向比例，逗号分隔）
$PY $S/slice_subtitles.py "<成片>.mp4" --sub-band 0.705,0.795

# 切太碎 / 太粗时调灵敏度（边缘图差值，越大越钝；默认 9.5）
$PY $S/slice_subtitles.py "<成片>.mp4" --diff-thresh 9.5 --detect-fps 3

# 画面 + 完整台词（ASR 叠字）
$PY $S/slice_subtitles.py "<成片>.mp4" --mode hybrid

# 纯文字卡（干净排版）
$PY $S/slice_subtitles.py "<成片>.mp4" --mode textcard

# 先看会出多少张图、不生成（raw 也会先跑一遍检测）
$PY $S/slice_subtitles.py "<成片>.mp4" --dry-run

# 自定义：输出目录 / 截帧宽度 / 单句最大字数
$PY $S/slice_subtitles.py "<成片>.mp4" --out-dir <dir> --scale-w 1080 --max-chars 48
```

输出目录默认在视频同目录 `<成片名>_字幕图/`，内含：

- `001.jpg` / `002.jpg` … 按时间顺序，**raw 每张就是一条烧录字幕**
- `manifest.json`：`{video, mode, count, images:[{file, text, start, end}]}`
  - raw 模式下 `text` 是**对应时间段的 ASR 参考文本**（一句 ASR 常覆盖多张图，仅作检索参考；真实字幕以图片为准）

### 字幕总览长图

图多时别逐张看。用 `make_sheet.py` 一键生成「字幕总览」长图（每页 18 行 + 序号 + 时间），一眼读完整个逐字稿：

```bash
$PY $S/make_sheet.py "<成片名>_字幕图" --per-page 18 --band 0.700,0.800
```

输出 `总览_1.jpg`、`总览_2.jpg` … 在目录下。

> 提示：图片按时间顺序编号，可直接拖进小红书图文编辑器。小红书单篇最多 18 图，几十张需自行挑选合并。

---

## 关键设计

### raw：烧录字幕逐条检测（默认，不需 OCR）

1. 按 `--detect-fps`（默认 3）抽帧 → 裁**字幕行**（`--sub-band`，默认 0.705–0.795）→ 转**边缘图**（`FIND_EDGES`）缩成 176×36 签名。
2. **为什么用边缘不用灰度**：卡拉OK逐词高亮只改字的颜色、不改字形轮廓，边缘图对高亮免疫。实测同一句不同高亮边缘差 ≈5.6，不同句 ≈14，区分度约 2.5×。
3. 相邻帧边缘差 > `--diff-thresh`（默认 9.5）即认为**换了一条字幕** → 分组。
4. 过滤：时长 < 0.45s、或该组平均边缘强度 < 12（空镜 / 纯背景）→ 丢弃。
5. 每条字幕取组**中段帧**做代表（此时整行字幕已完整显示），并跳过与上一条几乎相同（差 < 6）的重复。
6. 结果：一套图 = 完整逐字稿，文字与原片 100% 一致。

### 其它模式（ASR 路线）

1. **词级时间戳**：`word_timestamps=True`，句子 start/end 取真实首词 / 末词时间（按片段平均分配会导致截到下一句）。
2. **断句**：句末标点必断；≥ `--max-chars` 且落在逗号处则断；超长硬断兜底。
3. **取帧**：`(start+end)/2` 中点帧。
4. 有软字幕流（srt/ass）优先 `ffmpeg` 抽 srt；无则 ASR。whisper 默认繁体 → `opencc` `t2s`。

---

## 已知限制

- raw 靠「画面变化」定字幕边界，**不识别文字**：文字本身来自原片烧录字幕（100% 一致），但边界依赖字幕是否真的换了画面。花字 / 贴纸 / 录屏在字幕行内变化会误判。
- ASR 路线不认画面文字（whisper 是听不是看），听写与原字幕可能有出入（如 AdTech 听成 iTech），以 `hybrid` / `textcard` 展示的听写为准。
- 本机无中文 OCR 时无法把 raw 的图转成文本；若同时要「图文 + 可复制文字」，raw 出图 + 复用转写流程的逐字稿最省。
- 多人快速对话时，一句可能被切得过碎或过粗 —— 用 `--max-chars` 和 `--dry-run` 先调，再正式出图。

---

## 作为 WorkBuddy Skill 使用

本仓库即一个 WorkBuddy 技能，目录结构符合 `SKILL.md` 规范。可直接把 `SKILL.md` + `scripts/` 放进
`~/.workbuddy/skills/video-subtitle-slicer/` 启用。

## License

MIT © Hunchide
