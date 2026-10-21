#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「成片视频」切成多张图片，每图一句完整字幕 —— 方便拼台词做图文笔记（如小红书）。

设计：
  成片视频（多为竖屏、烧录字幕）没有独立字幕流 → 用 mlx-whisper 听写音频拿短句，
  再按「句末标点」合并成「一句完整字幕」；对每句取时间中点截一帧，PIL 把这句字幕
  叠加到底部（半透明黑条 + 白字描边），输出 001.jpg / 002.jpg ... + manifest.json。

四种模式：
  raw      默认。**按烧录字幕「换一句」出图**：检测字幕条变化，每条字幕一张图，
           整套图连起来就是完整逐字稿（不重绘，保留画面里的原字幕）
  sentence 按 ASR 整句截中点帧（一句话一张，粒度粗，字幕可能只有半句）
  hybrid   视频画面 + 底部模糊底条叠「完整台词」（盖掉原碎片字幕/营销条）
  frame    视频帧 + 脚本叠加的字幕（半透明条+白字描边），适合原片无字幕
  textcard 纯文字卡（浅底 + 大字台词 + 时间码，不带画面，适合干净排版）

用法：
  python slice_subtitles.py <video.mp4>                    # raw（默认，按字幕换句出全套）
  python slice_subtitles.py <video.mp4> --mode sentence
  python slice_subtitles.py <video.mp4> --mode hybrid
  python slice_subtitles.py <video.mp4> --mode textcard
  python slice_subtitles.py <video.mp4> --sub-band 0.66,0.80 --detect-fps 3
  python slice_subtitles.py <video.mp4> --dry-run        # 只打印会出多少张图，不生成

依赖：mlx_whisper（Apple GPU）、opencc（简体）、Pillow、ffmpeg。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil

# ---------- 转写后端（复用 mlx-whisper，和 video-transcribe-cn 同源） ----------
try:
    import mlx_whisper
    HAVE_MLX = True
except Exception:
    HAVE_MLX = False

try:
    import opencc
    CC = opencc.OpenCC("t2s")
    HAVE_CC = True
except Exception:
    CC = None
    HAVE_CC = False

MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"   # macOS 华文黑体（清晰、有中文字形）
SENT_END = "。！？!?；;\n"   # 断句标点


# ======================================================================
# 1. 取字幕：有软字幕流就抽 srt，否则 ASR
# ======================================================================
def has_subtitle_stream(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type", "-of", "default=noprint_wrappers=1", video],
        capture_output=True, text=True)
    return "subtitle" in out.stdout


def extract_srt(video, tmp_srt):
    """抽软字幕流为 srt（取第一条 subtitle 流）。"""
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", video, "-map", "0:s:0",
         "-c:s", "srt", tmp_srt], capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(tmp_srt) and os.path.getsize(tmp_srt) > 0


def parse_srt(path):
    """srt -> [{start,end,text}]，srt 本身就是按句切好的，直接用。"""
    txt = open(path, encoding="utf-8", errors="ignore").read()
    segs = []
    for block in re.split(r"\n\s*\n", txt.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})", lines[1])
        if not m:
            continue
        ts = lambda s: sum(float(x) * f for x, f in zip(
            s.replace(",", ".").split(":"), (3600, 60, 1)))
        text = " ".join(lines[2:]).strip()
        if text:
            segs.append({"start": ts(m.group(1)), "end": ts(m.group(2)), "text": text})
    return segs


def asr_transcribe(video):
    """整段 ASR（短视频直接用，不必切 30 分钟段）。返回 mlx_whisper 的 segments。"""
    if not HAVE_MLX:
        sys.exit("未安装 mlx_whisper，请先：python -m pip install mlx-whisper opencc-python-reimplemented")
    audio = tempfile.mktemp(suffix=".wav")
    # 抽 16k 单声道给模型
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video,
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("抽音频失败：" + r.stderr[:300])
    res = mlx_whisper.transcribe(
        audio, path_or_hf_repo=MODEL, language="zh",
        initial_prompt="以下是一段普通话视频，含有出镜解说与字幕。请使用简体中文输出，并带上正常的中文标点。",
        condition_on_previous_text=False, word_timestamps=True, verbose=False)
    os.unlink(audio)
    return res.get("segments", [])


def _split_clauses(text):
    """把一段 ASR 文本按句末标点 + 逗号切成子句（标点保留在子句尾部），作为自然断句边界。"""
    parts = re.split(r"(?<=[。！？!?；;，,：:])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def to_sents_from_asr(segments, max_chars=48):
    """ASR 短片段 → 合并成「一句完整字幕」：按子句累积，句末或超长即断。
    同一 whisper 片段里的多句按出现顺序均匀分配时间码，保证每张图截到不同画面。"""
    sents = []
    buf = ""
    cur_start = cur_end = None
    seg_sents = []  # 当前片段内已切出的句子文本

    def flush():
        nonlocal buf
        if buf.strip():
            seg_sents.append(buf.strip())
        buf = ""

    def close_seg(s_start, s_end):
        nonlocal seg_sents
        n = len(seg_sents)
        if n == 0:
            return
        for k, txt in enumerate(seg_sents):
            if n == 1:
                st, en = s_start, s_end
            else:
                st = s_start + (s_end - s_start) * (k / n)
                en = s_start + (s_end - s_start) * ((k + 1) / n)
            sents.append({"start": st, "end": en, "text": txt})
        seg_sents = []

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        s_start = seg.get("start") or 0.0
        s_end = seg.get("end") or s_start
        if cur_start is None:
            cur_start, cur_end = s_start, s_end
        else:
            close_seg(cur_start, cur_end)  # 收掉上一个片段
            cur_start, cur_end = s_start, s_end
        for cl in _split_clauses(text):
            # 再加这一子句会超长 → 先把前面整句断出去
            if buf and len(buf) + len(cl) > max_chars and len(buf) >= 4:
                flush()
            buf += cl
            if cl[-1] in SENT_END and len(buf) >= 4:
                flush()
            elif len(buf) >= max_chars:
                flush()
    close_seg(cur_start, cur_end)
    return sents


def to_sents_from_words(words, max_chars=48):
    """用词级时间戳精确切句：句末/逗号/超长即断，start/end 取真实词时间。"""
    sents, buf, w_start, w_end = [], "", None, None
    soft = max_chars
    hard = int(max_chars * 1.45)
    for w in words:
        txt = w.get("word") or ""
        if txt == "":
            continue
        if w_start is None:
            w_start = w.get("start") or 0.0
        w_end = w.get("end") or w_start
        buf += txt
        stripped = buf.strip()
        if not stripped:
            continue
        last = stripped[-1]
        hit_end = last in SENT_END and len(stripped) >= 4
        hit_soft = len(stripped) >= soft and last in "，,：:、"
        hit_hard = len(stripped) >= hard
        if hit_end or hit_soft or hit_hard:
            sents.append({"start": w_start, "end": w_end, "text": stripped})
            buf, w_start, w_end = "", None, None
    if buf.strip():
        sents.append({"start": w_start or 0.0, "end": w_end or 0.0, "text": buf.strip()})
    return sents


def get_sentences(video, max_chars=48):
    tmp = tempfile.mktemp(suffix=".srt")
    if has_subtitle_stream(video) and extract_srt(video, tmp):
        print("  ✓ 检测到软字幕流，直接提取")
        segs = parse_srt(tmp)
        os.unlink(tmp)
        if segs:
            return segs
        print("  （软字幕为空，回退 ASR）")
    print("  → ASR 听写中（mlx-whisper, Apple GPU，词级时间戳）...")
    segs = asr_transcribe(video)
    words = [w for s in segs for w in (s.get("words") or []) if (w.get("word") or "").strip()]
    if words:
        sents = to_sents_from_words(words, max_chars)
    else:
        print("  （无词级时间戳，回退片段级切句）")
        sents = to_sents_from_asr(segs, max_chars)
    if HAVE_CC:
        for s in sents:
            s["text"] = CC.convert(s["text"])
    return sents


# ======================================================================
# 2. 图片生成
# ======================================================================
def load_font(path, size):
    if not path or not os.path.exists(path):
        return None
    for idx in (1, 0):
        try:
            from PIL import ImageFont
            return ImageFont.truetype(path, size, index=idx)
        except Exception:
            continue
    return None


def wrap_text(text, font, max_width):
    from PIL import ImageFont
    if font is None:
        return [text[i:i + 12] for i in range(0, len(text), 12)]
    lines, cur = [], ""
    for ch in text:
        test = cur + ch
        try:
            w = font.getlength(test)
        except Exception:
            w = len(test) * size_of(font)
        if w > max_width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def size_of(font):
    try:
        return font.size
    except Exception:
        return 40


def draw_subtitle(img, text, font_path):
    """在底部画半透明黑条 + 白字描边字幕。"""
    from PIL import Image, ImageDraw
    w, h = img.size
    fs = max(30, w // 22)
    font = load_font(font_path, fs)
    lines = wrap_text(text, font, int(w * 0.92))
    lh = int(fs * 1.35)
    pad = int(fs * 0.55)
    block_h = lh * len(lines) + pad * 2
    block_h = min(block_h, int(h * 0.55))  # 最多占一半高

    bar = Image.new("RGBA", (w, block_h), (0, 0, 0, 155))
    img = img.convert("RGBA")
    img.paste(bar, (0, h - block_h), bar)

    d = ImageDraw.Draw(img)
    y = h - block_h + pad + lh / 2
    sw = max(1, fs // 18)
    for line in lines:
        d.text((w / 2, y), line, font=font, fill=(255, 255, 255),
               anchor="mm", align="center",
               stroke_width=sw, stroke_fill=(0, 0, 0))
        y += lh
    return img.convert("RGB")


def make_frame_card(video, sent, idx, out_dir, scale_w, font_path, quality):
    from PIL import Image
    mid = (sent["start"] + sent["end"]) / 2.0
    tmp = os.path.join(out_dir, f"_f{idx:03d}.jpg")
    vf = f"scale={scale_w}:-1" if scale_w else "null"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{mid:.3f}",
                        "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "2", tmp],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tmp):
        print(f"  ! 截帧失败 #{idx}: {r.stderr[:120]}")
        return None
    img = Image.open(tmp).convert("RGB")
    os.unlink(tmp)
    img = draw_subtitle(img, sent["text"], font_path)
    out = os.path.join(out_dir, f"{idx:03d}.jpg")
    img.save(out, quality=quality)
    return out


def extract_frame(video, t, scale_w, tmp):
    """在 t 秒处截一帧到 tmp（不重绘，保留画面原烧录字幕）。"""
    vf = f"scale={scale_w}:-1" if scale_w else "null"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}",
                        "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "3", tmp],
                       capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(tmp)


def make_raw_card(video, sent, idx, out_dir, scale_w, quality):
    """不重绘字幕：取该句真实时间的「中点帧」，原烧录字幕自然入镜。
    词级时间戳保证中点落在这句台词正在显示的时刻。"""
    from PIL import Image
    t0, t1 = sent["start"], sent["end"]
    mid = (t0 + t1) / 2.0 if (t1 and t1 > t0) else (t0 or 0.0)
    tmp = os.path.join(out_dir, f"_r{idx:03d}.jpg")
    if not extract_frame(video, mid, scale_w, tmp):
        print(f"  ! 截帧失败 #{idx} @ {mid:.2f}s")
        return None
    out = os.path.join(out_dir, f"{idx:03d}.jpg")
    Image.open(tmp).convert("RGB").save(out, quality=quality)
    if os.path.exists(tmp):
        os.unlink(tmp)
    return out


def make_hybrid_card(video, sent, idx, out_dir, scale_w, font_path, quality):
    """保留视频画面，但用模糊底盖掉底部碎片字幕/营销条，改叠「这句完整台词」。
    适合烧录字幕是卡拉OK短词碎片的成片：一图一句完整台词 + 画面感。"""
    from PIL import Image, ImageDraw, ImageFilter
    t0, t1 = sent["start"], sent["end"]
    mid = (t0 + t1) / 2.0 if (t1 and t1 > t0) else (t0 or 0.0)
    tmp = os.path.join(out_dir, f"_h{idx:03d}.jpg")
    if not extract_frame(video, mid, scale_w, tmp):
        print(f"  ! 截帧失败 #{idx} @ {mid:.2f}s")
        return None
    img = Image.open(tmp).convert("RGB")
    w, h = img.size
    band_top = int(h * 0.72)                      # 底部 28% 覆盖区
    # 用该区域自身模糊 + 压暗做底，去除原碎片字幕与营销条
    band = img.crop((0, band_top, w, h)).filter(ImageFilter.GaussianBlur(28))
    ov = Image.new("RGBA", band.size, (0, 0, 0, 140))
    band = Image.alpha_composite(band.convert("RGBA"), ov).convert("RGB")
    img.paste(band, (0, band_top))
    band_h = h - band_top
    # 自适应字号，保证完整台词塞进覆盖区
    fs = int(w * 0.052)
    lines, font = [], None
    while fs > 20:
        font = load_font(font_path, fs)
        lines = wrap_text(sent["text"], font, int(w * 0.88))
        if int(fs * 1.35) * len(lines) <= band_h * 0.86:
            break
        fs -= 4
    d = ImageDraw.Draw(img)
    lh = int(fs * 1.35)
    y = band_top + (band_h - lh * len(lines)) / 2 + lh / 2
    sw = max(1, fs // 18)
    for line in lines:
        d.text((w / 2, y), line, font=font, fill=(255, 255, 255), anchor="mm",
               align="center", stroke_width=sw, stroke_fill=(0, 0, 0))
        y += lh
    out = os.path.join(out_dir, f"{idx:03d}.jpg")
    img.save(out, quality=quality)
    if os.path.exists(tmp):
        os.unlink(tmp)
    return out


def make_text_card(sent, idx, out_dir, font_path, quality):
    """纯文字卡：浅底 + 大字台词 + 底部时间码。竖屏 4:5。"""
    from PIL import Image, ImageDraw
    W, H = 1080, 1350
    img = Image.new("RGB", (W, H), (244, 244, 247))
    d = ImageDraw.Draw(img)
    # 顶部装饰线
    d.rectangle([W * 0.12, 70, W * 0.88, 78], fill=(30, 30, 35))
    fs = W // 15
    font = load_font(font_path, fs)
    lines = wrap_text(sent["text"], font, int(W * 0.84))
    lh = int(fs * 1.4)
    total = lh * len(lines)
    y = (H - total) / 2
    sw = max(1, fs // 20)
    for line in lines:
        d.text((W / 2, y + lh / 2), line, font=font, fill=(25, 25, 30),
               anchor="mm", align="center",
               stroke_width=sw, stroke_fill=(244, 244, 247))
        y += lh
    # 底部时间码
    tc = f"{fmt_ts(sent['start'])} – {fmt_ts(sent['end'])}"
    d.text((W / 2, H - 70), tc, font=load_font(font_path, 34),
           fill=(140, 140, 150), anchor="mm")
    out = os.path.join(out_dir, f"{idx:03d}.jpg")
    img.save(out, quality=quality)
    return out


def fmt_ts(sec):
    sec = int(round(sec or 0))
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


# ======================================================================
# 烧录字幕短语检测（raw 模式：按「字幕换一句」出图，拼起来就是完整逐字稿）
# ======================================================================
def detect_subtitle_phrases(video, scale_w=1080, band=(0.705, 0.795), fps=3.0,
                            diff_thresh=9.5, min_dur=0.45, merge_thresh=6.0,
                            min_edge=12.0):
    """采样帧，靠字幕行的**边缘图**变化检测「每换一条烧录字幕」的边界。

    用边缘而非灰度：卡拉OK逐词高亮只改颜色、不改字形轮廓，边缘图对高亮免疫，
    因此同一句的高亮抖动不会被误判成换句。

    返回 (phrases, tmpdir)：
      phrases = [{start, end, rep, path}]，path 是该短语中段（字幕完整显示）的代表帧
    """
    from PIL import Image, ImageFilter

    tmpdir = tempfile.mkdtemp(prefix="subdet_")
    pat = os.path.join(tmpdir, "f_%05d.jpg")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", video,
           "-vf", f"fps={fps},scale={scale_w}:-1", "-q:v", "3", pat]
    subprocess.run(cmd, capture_output=True)
    files = sorted(os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.startswith("f_"))
    if not files:
        return [], tmpdir

    W, H = 176, 36
    sigs, edges = [], []
    for p in files:
        try:
            im = Image.open(p).convert("L")
            w, h = im.size
            crop = im.crop((int(w * 0.08), int(h * band[0]), int(w * 0.92), int(h * band[1])))
            e = crop.filter(ImageFilter.FIND_EDGES)
            sig = list(e.resize((W, H)).get_flattened_data())
            sigs.append(sig)
            edges.append(sum(sig) / len(sig))
        except Exception:
            sigs.append(None)
            edges.append(0.0)

    n = W * H

    def mad(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / n

    change = [True] * len(sigs)
    for i in range(1, len(sigs)):
        if sigs[i] and sigs[i - 1]:
            change[i] = mad(sigs[i], sigs[i - 1]) > diff_thresh

    groups, start = [], 0
    for i in range(1, len(sigs) + 1):
        if i == len(sigs) or change[i]:
            groups.append((start, i - 1))
            start = i

    dt = 1.0 / fps
    phrases, prev_sig = [], None
    for (g0, g1) in groups:
        idxs = [i for i in range(g0, g1 + 1) if sigs[i]]
        if not idxs:
            continue
        dur = (g1 - g0 + 1) * dt
        if dur < min_dur:
            continue
        # 该组平均边缘强度够高才认为真的有字幕（排除空镜/纯背景）
        if sum(edges[i] for i in idxs) / len(idxs) < min_edge:
            continue
        rep = idxs[len(idxs) // 2]
        sig = sigs[rep]
        # 与上一条短语几乎相同（同一句的高亮抖动）→ 跳过
        if prev_sig is not None and mad(sig, prev_sig) < merge_thresh:
            continue
        prev_sig = sig
        phrases.append({"start": g0 * dt, "end": (g1 + 1) * dt,
                        "rep": rep * dt, "path": files[rep]})
    return phrases, tmpdir


# ======================================================================
# main
# ======================================================================
def main():
    ap = argparse.ArgumentParser(description="成片视频 → 每句字幕一张图")
    ap.add_argument("video")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--mode", choices=["raw", "sentence", "hybrid", "frame", "textcard"], default="raw",
                     help="raw=按烧录字幕换句出图(默认,整套拼起来=完整逐字稿)；sentence=按ASR整句截中点帧；hybrid=画面+叠完整台词；frame=叠字幕(无字幕原片)；textcard=纯文字卡")
    ap.add_argument("--scale-w", type=int, default=1080, help="截帧宽度（raw/frame 模式）")
    ap.add_argument("--max-chars", type=int, default=48, help="单句最大字数（超长兜底断）")
    ap.add_argument("--sub-band", default="0.705,0.795", help="raw 字幕行纵向范围（占画面高比例，逗号分隔）")
    ap.add_argument("--detect-fps", type=float, default=3.0, help="raw 字幕检测采样帧率")
    ap.add_argument("--diff-thresh", type=float, default=9.5, help="raw 换句判定阈值（越大越钝，边缘图差值）")
    ap.add_argument("--font", default=DEFAULT_FONT)
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--dry-run", action="store_true", help="只打印会出多少图，不生成")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        sys.exit(f"找不到视频：{args.video}")

    out_dir = args.out_dir or (
        os.path.join(os.path.dirname(args.video),
                     os.path.splitext(os.path.basename(args.video))[0] + "_字幕图"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n视频：{args.video}")
    print(f"输出：{out_dir}")
    sents = get_sentences(args.video, args.max_chars)
    print(f"→ ASR 切出 {len(sents)} 句")

    def asr_text_at(t):
        for s in sents:
            if s["start"] - 0.3 <= t <= s["end"] + 0.3:
                return s["text"]
        return sents[0]["text"] if sents else ""

    # ---- raw：按烧录字幕「换一句」出图（整套 = 完整逐字稿）----
    if args.mode == "raw":
        band = tuple(float(x) for x in args.sub_band.split(","))
        phrases, tmp = detect_subtitle_phrases(
            args.video, args.scale_w, band=band,
            fps=args.detect_fps, diff_thresh=args.diff_thresh)
        try:
            if len(phrases) >= 3:
                print(f"→ 检测到 {len(phrases)} 条烧录字幕短语（每条一张图）")
                if args.dry_run:
                    for i, p in enumerate(phrases, 1):
                        print(f"  {i:02d}. [{fmt_ts(p['start'])}-{fmt_ts(p['end'])}]")
                    return
                manifest = {"video": os.path.abspath(args.video), "mode": "raw",
                            "count": len(phrases), "images": []}
                for i, p in enumerate(phrases, 1):
                    out = os.path.join(out_dir, f"{i:03d}.jpg")
                    shutil.copyfile(p["path"], out)
                    txt = asr_text_at(p["rep"])
                    manifest["images"].append({
                        "file": os.path.basename(out), "text": txt,
                        "start": round(p["start"], 2), "end": round(p["end"], 2)})
                    print(f"  ✓ {os.path.basename(out)}  [{fmt_ts(p['start'])}] {txt[:26]}")
                with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                print(f"\n✓ 完成：{len(manifest['images'])} 张图 + manifest.json → {out_dir}")
                return
            print("→ 未检测到烧录字幕，回退按 ASR 整句截中点帧")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- 其它模式（以及 raw 兜底）：按 ASR 句子 ----
    if args.dry_run:
        for i, s in enumerate(sents, 1):
            print(f"  {i:02d}. [{fmt_ts(s['start'])}] {s['text']}")
        return

    manifest = {"video": os.path.abspath(args.video),
                "mode": args.mode, "count": len(sents), "images": []}
    for i, s in enumerate(sents, 1):
        if args.mode == "hybrid":
            out = make_hybrid_card(args.video, s, i, out_dir, args.scale_w, args.font, args.quality)
        elif args.mode in ("raw", "sentence"):
            out = make_raw_card(args.video, s, i, out_dir, args.scale_w, args.quality)
        elif args.mode == "frame":
            out = make_frame_card(args.video, s, i, out_dir, args.scale_w, args.font, args.quality)
        else:
            out = make_text_card(s, i, out_dir, args.font, args.quality)
        if out:
            manifest["images"].append({
                "file": os.path.basename(out),
                "text": s["text"],
                "start": round(s["start"], 2),
                "end": round(s["end"], 2),
            })
            print(f"  ✓ {os.path.basename(out)}  [{fmt_ts(s['start'])}] {s['text'][:24]}")

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 完成：{len(manifest['images'])} 张图 + manifest.json → {out_dir}")


if __name__ == "__main__":
    main()
