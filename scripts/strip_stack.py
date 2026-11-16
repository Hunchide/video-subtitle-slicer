#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strip_stack.py — 「台词条」拼图：每句 = 该帧的一条横带（自带烧录字幕），竖着摞成一长图

参考版式：
    ┌──────────────────────────┐
    │  人物画面（完整人头）        │  ← 第 1 句：从头顶切到字幕底
    │        台词 1              │
    ├──────────────────────────┤
    │        台词 2              │  ← 其余：窄横带
    ├──────────────────────────┤
    │        台词 3              │
    └──────────────────────────┘  默认输出精确 1080×1440（3:4）

命令行：
  python strip_stack.py <视频> --subdir <字幕图目录> --start 4 --count 4 --out out.jpg

作为库用（build_pages.py 就是这么调的）：
  from strip_stack import load_items, grab_frames, compose_page
"""
import os, sys, json, argparse, subprocess, tempfile, shutil, re
import numpy as np
from PIL import Image

import _compat

DARK = 70
ROW_DARK_FRAC = 0.20
MIN_BAR_FRAC = 0.015
MAX_BAR_FRAC = 0.32


# ---------- 字幕条检测 ----------

def find_bar_box(g, y_from=0.45, y_to=1.0, anchor=None):
    """找画面下半部的字幕黑条，返回 (y0, y1) 或 None。

    anchor = OCR 给出的字幕中心（整幅高度比例）。给了就选**中心离它最近**的暗条——
    画面底部常有深色衣物/阴影比字幕条还大，不锚定会整段切错。"""
    h = g.shape[0]
    a0, a1 = int(h * y_from), int(h * y_to)
    reg = g[a0:a1]
    if reg.size == 0:
        return None
    frac = (reg < DARK).mean(axis=1)
    rows = frac >= ROW_DARK_FRAC
    runs, s = [], None
    for i, v in enumerate(rows):
        if v and s is None:
            s = i
        elif not v and s is not None:
            runs.append((s, i)); s = None
    if s is not None:
        runs.append((s, len(rows)))
    cands = [(ra, rb) for (ra, rb) in runs
             if h * MIN_BAR_FRAC <= rb - ra <= h * MAX_BAR_FRAC]
    if not cands:
        return None
    if anchor is not None:
        ac = anchor * h - a0                       # 锚点换到 reg 坐标
        best = min(cands, key=lambda c: abs((c[0] + c[1]) / 2 - ac))
    else:
        best = max(cands, key=lambda c: c[1] - c[0])
    return a0 + best[0], a0 + best[1]


def text_sig(im, box, size=(200, 28)):
    """字幕条的「文字形状」签名：亮像素二值掩膜，压缩到固定尺寸。"""
    if box is None:
        return None
    y0, y1 = box
    crop = im.crop((0, max(0, y0 - 4), im.width, min(im.height, y1 + 4)))
    g = np.asarray(crop.convert("L"), dtype=np.float32)
    m = (g > 150).astype(np.float32)
    return np.asarray(Image.fromarray(m).resize(size, Image.BILINEAR)) > 0.4


def same_text(a, b, thresh=0.82):
    if a is None or b is None:
        return False
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return union > 0 and inter / union >= thresh


# ---------- 取帧 ----------

def extract_frame(video, t, width, out):
    subprocess.run([_compat.FFMPEG, "-y", "-nostdin", "-v", "error", "-ss", f"{t:.3f}",
                    "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-1",
                    "-q:v", "2", out], capture_output=True)
    return os.path.exists(out) and os.path.getsize(out) > 0


def load_items(subdir):
    man = json.load(open(os.path.join(subdir, "manifest.json"), encoding="utf-8"))
    return man["images"]


def grab_frames(video, items, indices, width, tmp, dedupe=True, tag="g",
                skip_kinds=("slide",)):
    """按 1-based 序号抽帧；dedupe=True 时跳过与上一张字幕文字相同的帧。
    skip_kinds：这些 kind 的条目不出图（默认跳过幻灯片——整页塞不进横带）。"""
    frames, sigs, used, bars = [], [], [], []
    for i in indices:
        if i < 1 or i > len(items):
            continue
        it = items[i - 1]
        if skip_kinds and it.get("kind") in skip_kinds:
            continue
        # manifest 里带 t 就用它（那是出图时回读校验过的点，字幕肯定对得上）
        t = it["t"] if it.get("t") is not None else (it["start"] + it["end"]) / 2
        f = os.path.join(tmp, f"{tag}{i}.jpg")
        if not extract_frame(video, t, width, f):
            continue
        im = Image.open(f).convert("RGB")
        g = np.asarray(im.convert("L"), dtype=np.float32)
        bar = it.get("bar") or []
        anchor = (bar[0] + bar[1]) / 2 if len(bar) == 2 and bar[1] > bar[0] else None
        box = find_bar_box(g, anchor=anchor)
        sig = text_sig(im, box)
        if dedupe and sigs and same_text(sigs[-1], sig):
            continue
        frames.append(im)
        sigs.append(sig)
        used.append(i)
        bars.append(bar)
    return frames, used, bars


# ---------- 切带 ----------

def detect_head_top(g):
    """从上往下在画面中央找「深色头发」的第一行（避开左上角水印），返回像素 y。"""
    h, w = g.shape
    x0, x1 = int(w * 0.20), int(w * 0.80)
    lim = int(h * 0.60)
    for y in range(0, max(1, lim - 8)):
        if (g[y, x0:x1] < 92).mean() > 0.10:
            if all((g[y + k, x0:x1] < 100).mean() > 0.08 for k in range(1, 8)):
                return y
    return None


def make_header(im, top_frac=-1.0, pad_bottom=24, max_frac=0.80, headroom=0.05,
                anchor=None):
    """第一行：从画面顶部（或自动头顶 / top_frac 处）切到字幕条底部，保住完整人头。
    headroom = 头顶上方额外留白比例（放不下时可压到 0）。"""
    W, H = im.size
    g = np.asarray(im.convert("L"), dtype=np.float32)
    box = find_bar_box(g, anchor=anchor)
    if box is None and anchor is not None:
        # 短句帧（如「第五环」）黑底条很窄，find_bar_box 可能找不到；
        # 用 manifest 的 bar 中心锚点估算，避免 bottom 被当成整帧高、
        # 进而触发 max_frac 把板书顶部大标题裁掉。
        box = (max(0, int(H * (anchor - 0.030))), min(H, int(H * (anchor + 0.030))))
    bottom = min(H, box[1] + pad_bottom) if box is not None else H
    if top_frac is None or top_frac < 0:      # 自动：贴着头顶，上留 headroom
        ht = detect_head_top(g)
        top = 0 if ht is None else max(0, ht - int(H * headroom))
    else:
        top = int(H * top_frac)
    top = max(top, bottom - int(H * max_frac))     # 太高就裁掉一点顶部背景
    top = max(0, min(top, bottom - 40))
    return im.crop((0, top, W, bottom))


def make_header_parts(im, top_frac=-1.0, pad_bottom=24, max_frac=0.80, headroom=0.05,
                      anchor=None):
    """头图拆成「人物区」+「字幕条」两块返回。
    字幕条**保持原始像素高度**（不参与缩放），这样每一条字幕的字号都跟原片一致——
    整体等比缩小头图会把字幕一起缩小，跟下面横带的字号对不上。"""
    W, H = im.size
    g = np.asarray(im.convert("L"), dtype=np.float32)
    box = find_bar_box(g, anchor=anchor)
    if box is None and anchor is not None:
        # 同 make_header：短句帧找不到黑条时按锚点估算，别退回「整帧 + bar=None」
        # （后者会让 compose_page 走整体等比缩 + 两侧模糊兜底）
        box = (max(0, int(H * (anchor - 0.030))), min(H, int(H * (anchor + 0.030))))
    if box is None:
        return im.crop((0, 0, W, H)), None
    y0, y1 = box[0], box[1]
    bar = im.crop((0, y0, W, min(H, y1 + pad_bottom)))
    if top_frac is None or top_frac < 0:
        ht = detect_head_top(g)
        top = 0 if ht is None else max(0, ht - int(H * headroom))
    else:
        top = int(H * top_frac)
    top = max(top, y0 - int(H * max_frac))
    top = max(0, min(top, y0 - 40))
    return im.crop((0, top, W, y0)), bar


def fit_body(body, avail, width):
    """人物区放不下时只压这一块：等比缩到 avail 高，两侧用同帧模糊填充。"""
    from PIL import ImageFilter
    avail = max(60, int(avail))
    nw = max(2, int(round(body.width * avail / body.height)))
    small = body.resize((nw, avail), Image.LANCZOS)
    pad = body.resize((width, avail), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    pad.paste(small, ((width - nw) // 2, 0))
    return pad


def make_strip(im, band_h, pad_bottom=0, anchor=None):
    """从一帧里切出「含字幕」的横带：字幕条在带子里上下居中（pad_bottom>0 时用该值）。
    pad_bottom<0 = 字幕条**贴着带子底边**（下方不留白，用来跟下面的大字卡挨紧）。"""
    W, H = im.size
    g = np.asarray(im.convert("L"), dtype=np.float32)
    box = find_bar_box(g, anchor=anchor)
    if box is None and anchor is not None:
        # 白字无底条的口播片（如 转行不降薪.mp4）：find_bar_box 靠「暗条」检测必然 None，
        # 原来的「贴画面底部往上切」兜底会切到 y≈0.92~1.0，而字幕在 y≈0.78 → 横带里没有字。
        # 改用 manifest 的 bar 中心锚点估算字幕位置（与 make_header 的短句帧兜底一致）。
        # 只在原本就找不到的帧生效；有暗条的历史片子行为不变。
        box = (max(0, int(H * (anchor - 0.030))), min(H, int(H * (anchor + 0.030))))
    if box is not None:
        bar_h = box[1] - box[0]
        if pad_bottom < 0:
            pad = 0
        elif pad_bottom > 0:
            pad = pad_bottom
        else:
            pad = max(8, (band_h - bar_h) // 2)
        bottom = min(H, box[1] + pad)
    else:                                    # 兜底：贴画面底部往上切
        bottom = H - int(H * 0.02)
    bottom = max(1, min(H, bottom))
    top = max(0, bottom - band_h)
    strip = im.crop((0, top, W, bottom))
    if strip.height < band_h:                # 顶部不够高就补边（极少数情况）
        pad = Image.new("RGB", (W, band_h), (0, 0, 0))
        pad.paste(strip, (0, band_h - strip.height))
        strip = pad
    return strip.resize((W, band_h), Image.LANCZOS) if strip.height != band_h else strip


# ---------- 组版 ----------

def measure(frames, bars=None):
    hs = []
    for k, im in enumerate(frames):
        bar = bars[k] if bars else None
        anchor = (bar[0] + bar[1]) / 2 if bar and len(bar) == 2 and bar[1] > bar[0] else None
        b = find_bar_box(np.asarray(im.convert("L"), dtype=np.float32), anchor=anchor)
        if b is not None:
            hs.append(b[1] - b[0])
    return hs


def max_rows(H_head, min_strip, gap, target_h):
    """在精确 target_h 下，最多能摞几行（含 header）。"""
    if not target_h:
        return 99
    return 1 + max(0, int((target_h - H_head) // (min_strip + gap)))


_OCR = None
_WATERMARK = re.compile(r"Frank|小茶|@|biji|茶蛋|Speaking", re.I)
_BB_CACHE = {}


def slide_band_h(im, width):
    """大字卡行「铺满整宽」所需的高度（用于自适应行高，避免两侧留白）。"""
    bb = text_bbox_of(im)
    if bb is None:
        return 0
    cw = (bb[2] - bb[0]) * im.width
    ch = (bb[3] - bb[1]) * im.height
    if cw <= 0 or ch <= 0:
        return 0
    return int(round(width * ch / cw))


def text_bbox_of(im, min_w_frac=0.45):
    """大字卡：用 OCR 求出「文字区域」的 bbox（比例坐标），好把字单独裁出来。
    找不到就返回 None（调用方回退到整页缩略）。"""
    key = id(im)
    if key in _BB_CACHE:
        return _BB_CACHE[key]
    global _OCR
    try:
        if _OCR is None:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        res, _ = _OCR(np.array(im.convert("RGB")))
    except Exception:
        return None
    H, W = im.height, im.width
    xs0, ys0, xs1, ys1 = [], [], [], []
    for box, txt, score in (res or []):
        if float(score) < 0.5 or _WATERMARK.search(txt or ""):
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        xs0.append(min(xs)); xs1.append(max(xs))
        ys0.append(min(ys)); ys1.append(max(ys))
    if not ys0:
        _BB_CACHE[key] = None
        return None
    x0, x1 = min(xs0), max(xs1)
    y0, y1 = min(ys0), max(ys1)
    if (x1 - x0) < W * min_w_frac:        # 字太窄：左右各扩一点，别贴边
        pad = (W * min_w_frac - (x1 - x0)) / 2
        x0 = max(0, x0 - pad); x1 = min(W, x1 + pad)
    m = 0.02
    bb = (max(0, x0 - m * W) / W, max(0, y0 - m * H) / H,
          min(W, x1 + m * W) / W, min(H, y1 + m * H) / H)
    _BB_CACHE[key] = bb
    return bb


def make_slide_text_band(im, band_h):
    """只留大字：按 OCR 文字区域裁出来，再等比塞满横带（两侧模糊填充）。"""
    from PIL import ImageFilter
    W, H = im.size
    band_h = max(60, int(band_h))
    bb = text_bbox_of(im)
    if bb is None:
        return make_slide_block(im, band_h)
    crop = im.crop((int(bb[0] * W), int(bb[1] * H), int(bb[2] * W), int(bb[3] * H)))
    cw, ch = crop.size
    nh = band_h
    nw = int(round(nh * cw / ch))
    if nw > W:                            # 太宽：改成按宽度缩放，上下居中
        nw = W
        nh = max(60, int(round(W * ch / cw)))
        small = crop.resize((nw, min(nh, band_h)), Image.LANCZOS)
        canvas = crop.resize((W, band_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(24))
        canvas.paste(small, (0, (band_h - small.height) // 2))
        return canvas
    small = crop.resize((nw, nh), Image.LANCZOS)
    canvas = crop.resize((W, band_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(24))
    canvas.paste(small, ((W - nw) // 2, 0))
    return canvas


def make_slide_raw_band(im, band_h=None, pad_bottom=0, pad_top=0):
    """原尺寸裁大字：1:1 从原帧截出来，**不放大也不缩小**——保住原片字号。

    宽度用整幅宽（大字本来就是烧在画面上的，两侧是该帧真实画面，不是黑边）；
    高度取 OCR 文字区域的原始像素高。
    pad_bottom = 下边界再往下多带多少画面（字不动 → 字偏上、底部有呼吸）；
    pad_top    = 上边界再往上多带多少画面，**传负数=上边界往下切**，字就跟上一句贴得更近。
    band_h 只作兜底（OCR 失败时）。"""
    W, H = im.size
    bb = text_bbox_of(im)
    if bb is None:
        return make_slide_block(im, band_h or int(H * 0.3))
    x0, y0 = int(bb[0] * W), int(bb[1] * H)
    x1, y1 = int(bb[2] * W), int(bb[3] * H)
    x1 = max(x0 + 2, min(W, x1))
    y1 = max(y0 + 2, min(H, y1 + int(pad_bottom)))
    y0 = max(0, min(y0 + _cut_top(pad_top), y1 - 60))   # 负 = 上边界往下切（不许切到字）
    return im.crop((x0, y0, x1, y1))


def _cut_top(pad_top, cap=40):
    """往上切多少像素。OCR 框顶到字顶通常只剩 30~40px，切多了会切到字，所以封顶。"""
    return min(max(0, -int(pad_top)), cap)


def slide_raw_h(im, pad_bottom=0, pad_top=0):
    """raw 模式下这一行的高度 = 原帧裁出来的像素高（不参与任何缩放）。"""
    bb = text_bbox_of(im)
    if bb is None:
        return int(im.height * 0.3)
    H = im.height
    y0, y1 = int(bb[1] * H), int(bb[3] * H)
    y1 = min(H, y1 + int(pad_bottom))
    y0 = max(0, min(y0 + _cut_top(pad_top), y1 - 60))
    return max(60, y1 - y0)


def make_slide_block(im, band_h):
    """整页卡片（大字卡/提纲页）不裁成横带，而是等比缩到 band_h 高、两侧模糊填充。"""
    from PIL import ImageFilter
    W, H = im.size
    band_h = max(60, int(band_h))
    nw = int(round(band_h * W / H))
    if nw > W:                       # 极扁的图：改成按宽度 contain，上下留模糊
        nw = W
        band_h2 = int(round(W * H / W))
        small = im.resize((W, min(band_h, band_h2)), Image.LANCZOS)
        canvas = im.resize((W, band_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
        canvas.paste(small, (0, (band_h - small.height) // 2))
        return canvas
    small = im.resize((nw, band_h), Image.LANCZOS)
    canvas = im.resize((W, band_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(28))
    canvas.paste(small, ((W - nw) // 2, 0))
    return canvas


def compose_page(frames, width=1080, ratio=0.75, strip_ratio=1.55, min_pad=18,
                 gap=0, pad_bottom=0, header_top=-1.0, quality=90, strip_h=None,
                 header_mode="head", header_h=0, bars=None, slide_rows=(),
                 slide_frac=0.34, slide_mode="text", slide_pad_bottom=0, slide_pad_top=0,
                 headroom=0.05, header_fit="contain", tight_slide=False):
    """把给定帧摞成一张图；ratio>0 时**强制**该比例（默认 0.75 = 1080×1440）。
    header_fit: contain=人物区等比缩小+两侧模糊填充；cover=**不缩放**，从人物区顶部
    往下直接按原像素裁（保住头顶和头顶上方背景，两侧不糊，代价是人物下部被切）。
    返回 (canvas, info)。info 里含是否精确命中比例、放不下时的最大行数建议。"""
    n = len(frames)
    if n == 0:
        raise SystemExit("没有可用帧")
    # 每句的字幕条高度可能不同（单行 112 / 双行 177），行高必须逐行算
    anchors = []
    for k in range(n):
        bar = bars[k] if bars and k < len(bars) else None
        anchors.append((bar[0] + bar[1]) / 2
                       if bar and len(bar) == 2 and bar[1] > bar[0] else None)
    boxes = [find_bar_box(np.asarray(im.convert("L"), dtype=np.float32), anchor=a)
             for im, a in zip(frames, anchors)]
    bar_hs = [b[1] - b[0] if b else int(frames[0].height * 0.06) for b in boxes]
    ref = int(np.percentile(bar_hs, 80))
    min_strip = ref + 2 * min_pad
    # 统一基准条高：单行字幕一律等高（同片字幕条高度本就一致，逐帧测量会有几像素抖动）
    slide_rows = set(slide_rows or ())
    cap = [bar_hs[k] for k in range(1, n) if k not in slide_rows]
    ref_row = int(np.median(cap)) if cap else ref
    row_hs = [bar_hs[k] if (k in slide_rows or bar_hs[k] > ref_row * 1.35)
              else ref_row for k in range(1, n)]

    head_pad = pad_bottom if pad_bottom > 0 else 24
    for _pass in range(3):          # 两遍：第二遍让头图底部留白跟横带一致
        if header_mode == "band":                # 旧版：第一行只是条更厚的带子
            hh = header_h if header_h > 0 else int(max(width * 0.13, ref * strip_ratio + 20) * 2.8)
            header = make_strip(frames[0], hh, pad_bottom)
        else:                                    # 默认：按人头切
            header = make_header(frames[0], header_top, head_pad,
                                 headroom=headroom, anchor=anchors[0])
        H_head = header.height
        target_h = int(round(width / ratio)) if ratio and ratio > 0 else None

        # raw 模式：大字卡行按原帧像素高度「钉死」，不做任何缩放，其余行去凑 3:4
        fixed = ({k: slide_raw_h(frames[k], slide_pad_bottom, slide_pad_top)
                  for k in slide_rows} if slide_mode == "raw" else {})
        fixed_total = sum(fixed.values())

        # 行高塞不下时，先把头顶留白收窄（不切人头）再试一次
        if header_mode != "band" and target_h and n > 1:
            need = sum(row_hs[k - 1] + 2 * min_pad for k in range(1, n)
                       if k not in slide_rows) + fixed_total
            if H_head + need > target_h - gap * (n - 1):
                h2 = make_header(frames[0], header_top,
                                 head_pad, headroom=headroom * 0.5,
                                 anchor=anchors[0])
                if h2.height < H_head:
                    header, H_head = h2, h2.height

        heights = []
        for k, h in enumerate(bar_hs[1:], 1):
            if k in slide_rows:
                if k in fixed:
                    heights.append(fixed[k])
                elif slide_mode == "text" and target_h:
                    # 高度按「大字铺满整宽」自适应，这样两侧不留白
                    fh = slide_band_h(frames[k], width)
                    heights.append(int(min(fh or target_h * slide_frac,
                                           target_h * 0.62)))
                else:
                    heights.append(int(target_h * slide_frac) if target_h else int(width * 0.45))
            else:
                heights.append(row_hs[k - 1] + 2 * min_pad)
        exact = False
        header_scaled = False
        free = [k for k in range(1, n) if k not in slide_rows]   # 可自由缩放的行
        if target_h and n > 1:
            remaining = target_h - H_head - fixed_total - gap * (n - 1)
            base = sum(heights[k - 1] for k in free)
            if base > 0 and base <= remaining:    # 放得下：按比例把富余空间分给可缩放的行
                k = remaining / base
                for i in free:
                    heights[i - 1] = int(round(heights[i - 1] * k))
            else:                                 # 挤一挤：把留白压到 8px
                tight = {i: row_hs[i - 1] + 16 for i in free}
                if sum(tight.values()) <= remaining:
                    k = remaining / sum(tight.values())
                    for i in free:
                        heights[i - 1] = int(round(tight[i] * k))
                else:                             # 还是放不下：第一行大图等比缩小，两侧留黑
                    need = sum(tight.values()) + fixed_total
                    avail = target_h - need - gap * (n - 1)
                    avail = max(int(target_h * 0.08), avail)   # 下限只留 8% 画高，先保住横带
                    if avail < H_head:
                        # 关键：只压「人物区」，字幕条保持原始像素高度——
                        # 整体等比缩首图会把字幕一起缩小，跟下面横带的字号、条高都对不上
                        body, bar = make_header_parts(frames[0], header_top, 0, headroom=headroom,
                                                      anchor=anchors[0])
                        if bar is not None and avail - bar.height > 80:
                            room = avail - bar.height - head_pad
                            if header_fit == "cover" and body.height > room:
                                # 不缩放：从顶部往下按原像素裁，头顶和上方背景都在，两侧不糊
                                body = body.crop((0, 0, width, room))
                            else:
                                body = fit_body(body, room, width)
                            hdr = Image.new("RGB", (width, avail), (0, 0, 0))
                            hdr.paste(body, (0, 0))
                            hdr.paste(bar, (0, body.height))
                            # 字幕条下方补回 head_pad 的画面，否则头图↔第一条的空隙
                            # 会比横带之间的空隙窄一半，看着不齐
                            tail_h = avail - body.height - bar.height
                            if tail_h > 0:
                                # 取「字幕条正下方」那段原画面 1:1 补进来，衔接最自然
                                src = frames[0]
                                yb = int(bars[0][1] * src.height) if (
                                    bars and bars[0] and len(bars[0]) == 2) else src.height
                                yb = min(max(0, yb), max(0, src.height - tail_h))
                                band = src.crop((0, yb, src.width, yb + tail_h))
                                hdr.paste(band.resize((width, tail_h), Image.LANCZOS),
                                          (0, body.height + bar.height))
                            header, H_head = hdr, avail
                            for i in free:
                                heights[i - 1] = tight[i]
                            header_scaled = True
                        else:                     # 兜底：真的挤到放不下才整体缩
                            scale = avail / H_head
                            nw = max(2, int(round(header.width * scale)))
                            hdr = header.resize((nw, avail), Image.LANCZOS)
                            wid = frames[0].width
                            # 两侧不补黑边：用同帧放大模糊做底，看起来是「画面溢出」不是「贴黑条」
                            from PIL import ImageFilter
                            pad = header.resize((wid, avail), Image.LANCZOS) \
                                        .filter(ImageFilter.GaussianBlur(28))
                            pad.paste(hdr, ((wid - nw) // 2, 0))
                            header, H_head = pad, avail
                            for i in free:
                                heights[i - 1] = tight[i]
                            header_scaled = True
            # 吸收取整误差：逐行 +1px 轮着摊（肉眼看不出来），别全塞给最后一行——
            # 那会让最后一条横带比前面明显高一截，两条单行字幕看起来就不等高了
            diff = target_h - (H_head + sum(heights) + gap * (n - 1))
            idxs = [i for i in range(1, n) if i not in slide_rows] or list(range(1, n))
            step = 1 if diff > 0 else -1
            guard = 0
            while diff != 0 and guard < 200:
                for i in idxs:
                    if diff == 0:
                        break
                    if step < 0 and heights[i - 1] <= 60:
                        continue
                    heights[i - 1] += step
                    diff -= step
                guard += 1
            # 保底：任何一条横带都不许压到切字幕（这是最难看的失败形态），宁可裁首图底部
            # 用统一基准条高而不是该帧自己的条高——渐入帧会测出「33px 的小黑条」，按它会切字幕
            for i, hh in enumerate(heights):
                if i + 1 in slide_rows:
                    continue
                floor_h = row_hs[i] + 20
                if hh < floor_h:
                    heights[i] = floor_h
            over = (H_head + sum(heights) + gap * (n - 1)) - target_h
            if over > 0 and H_head - over > int(target_h * 0.10):
                header = header.crop((0, 0, header.width, H_head - over))
                H_head -= over
        if header_mode == "band" or n <= 1 or pad_bottom > 0:
            break
        sp = None
        for k in range(1, n):       # 取第一条普通横带的实际留白
            if k not in slide_rows:
                sp = max(8, (heights[k - 1] - row_hs[k - 1]) // 2)
                break
        if sp is None or sp == head_pad:
            break
        head_pad = sp               # 头图底留白 = 横带留白 → 所有空隙等高

    strips = [header]
    for k, (im, hh) in enumerate(zip(frames[1:], heights), 1):
        if k in slide_rows:
            if slide_mode == "raw":
                band = make_slide_raw_band(im, hh, slide_pad_bottom, slide_pad_top)
                strips.append(band if band.width == width
                              else band.resize((width, band.height), Image.LANCZOS))
            else:
                strips.append(make_slide_text_band(im, hh) if slide_mode == "text"
                              else make_slide_block(im, hh))
        else:
            # 下一行就是原尺寸大字卡时，这条横带底部不留白，让两句挨得更紧
            # 默认不开：这条一贴底，它的字幕条就比别的条高 10px，三处空隙看着不等高
            pb = -1 if (tight_slide and slide_mode == "raw"
                        and (k + 1) in slide_rows) else pad_bottom
            strips.append(make_strip(im, hh, pb, anchor=anchors[k]))

    # 渲染后仍不一致时（极少），用最后一条**可缩放**的横带吸收（大字卡行不参与）
    W = strips[0].width
    total = sum(s.height for s in strips) + gap * (len(strips) - 1)
    if target_h and total != target_h and abs(target_h - total) < int(target_h * 0.12):
        j = max(free) if free else (n - 1)
        new_h = max(60, strips[j].height + (target_h - total))
        if new_h != strips[j].height:
            strips[j] = make_strip(frames[j], new_h, pad_bottom, anchor=anchors[j])
        total = sum(s.height for s in strips) + gap * (len(strips) - 1)
    exact = (total == target_h) if target_h else True

    canvas = Image.new("RGB", (W, total), (0, 0, 0))
    y = 0
    for s in strips:
        canvas.paste(s, (0, y))
        y += s.height + gap

    info = {
        "rows": n, "size": canvas.size, "header_h": H_head, "head_pad": head_pad,
        "strip_h": heights[-1] if heights else 0,
        "ratio": canvas.size[0] / canvas.size[1],
        "exact": exact,
        "target_h": target_h,
        "max_rows": max_rows(H_head, min_strip, gap, target_h),
        "min_strip": min_strip,
        "header_scaled": header_scaled,
        "heights": heights, "bar_hs": bar_hs,
    }
    return canvas, info


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--subdir", required=True)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--out", default="strip_stack.jpg")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--ratio", type=float, default=0.75,
                    help="强制输出宽高比，0.75=1080×1440；0=不强制")
    ap.add_argument("--header-mode", choices=["head", "band"], default="head",
                    help="head=第一行从头顶切到字幕底；band=按高度切横带")
    ap.add_argument("--header-top", type=float, default=-1.0,
                    help="-1=自动贴头顶，0=画面最顶部，>0=按比例")
    ap.add_argument("--header-h", type=int, default=0,
                    help="band 模式第一行高度；0=自动，-1=与其它行等高")
    ap.add_argument("--strip-h", type=int, default=0, help="每行高度；0=自动")
    ap.add_argument("--strip-ratio", type=float, default=1.55,
                    help="自动带高 = 字幕条高 × 该倍数")
    ap.add_argument("--min-pad", type=int, default=18, help="每行字幕条最少留白")
    ap.add_argument("--pad-bottom", type=int, default=0)
    ap.add_argument("--gap", type=int, default=0)
    ap.add_argument("--quality", type=int, default=90)
    args = ap.parse_args()

    items = load_items(args.subdir)
    idx = list(range(args.start, args.start + args.count))
    tmp = tempfile.mkdtemp(prefix="strip_")
    try:
        frames, used, bars = grab_frames(args.video, items, idx, args.width, tmp,
                                         dedupe=True, tag="c")
        # 去重后行数不够就继续往后补
        nxt = args.start + args.count
        while len(frames) < args.count and nxt <= len(items):
            frames2, used2, bars2 = grab_frames(args.video, items, [nxt] * 1, args.width,
                                                tmp, dedupe=False, tag="x")
            if frames2:
                g = np.asarray(frames2[0].convert("L"), dtype=np.float32)
                if not same_text(text_sig(frames[-1], find_bar_box(
                        np.asarray(frames[-1].convert("L"), dtype=np.float32))),
                        text_sig(frames2[0], find_bar_box(g))):
                    frames.append(frames2[0]); used.append(nxt); bars.append(bars2[0])
            nxt += 1

        if not frames:
            raise SystemExit("抽帧失败")
        canvas, info = compose_page(
            frames, width=args.width, ratio=args.ratio,
            strip_ratio=args.strip_ratio, min_pad=args.min_pad, gap=args.gap,
            pad_bottom=args.pad_bottom, header_top=args.header_top,
            quality=args.quality, header_mode=args.header_mode, header_h=args.header_h,
            bars=bars)
        canvas.save(args.out, quality=args.quality)
        print(f"✓ {args.out}  {canvas.size[0]}×{canvas.size[1]}  ratio={info['ratio']:.4f}  "
              f"行数={info['rows']}  (header {info['header_h']}px + {info['rows']-1}×{info['strip_h']}px)")
        print(f"  句子序号：{used}")
        if not info["exact"]:
            print(f"  ⚠ 没能精确命中 {info['target_h']}px；该片头图下最多 {info['max_rows']} 行"
                  f"（当前 {info['rows']} 行）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
