#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯字幕拼图：只从每帧抠出黑底字幕条，摞到纯白 3:4 画布上，不带任何板书/人物画面。

属于 video-subtitle-slicer skill。默认路径按**当前工作目录**（项目根）解析，
换期/换项目用 --subdir 传字幕帧目录（含 manifest.json）的绝对或相对路径。

用法：
  python pure_caption.py --probe                      # 只打印每条字幕条的检测结果
  python pure_caption.py --build                     # 出图（上期默认区间）
  python pure_caption.py --start 222 --end 234 --build \
      --subdir "素材/下期V2_字幕图_ocr" --prefix 下期_222-234_纯字幕 --no-dedupe
"""
import os
import sys
import json
import argparse
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                      # strip_stack.py 与本脚本同目录
from strip_stack import find_bar_box  # noqa: E402

PROJECT = os.getcwd()                         # 项目根（运行时的 cwd）
SUBDIR = os.path.join(PROJECT, "素材", "上期V2_字幕图_ocr")   # 默认上期，--subdir 可覆盖
W, H = 1080, 1440
BG = (255, 255, 255)
DARK = 70


def load_items():
    mj = os.path.join(SUBDIR, "manifest.json")
    if not os.path.exists(mj):
        sys.exit(f"找不到 {mj}\n请用 --subdir 指定字幕帧目录（含 manifest.json），"
                 f"或在项目根目录下运行本脚本。")
    with open(mj, encoding="utf-8") as f:
        return json.load(f)["images"]


def analyze(im, it):
    """返回 (gray, detect_box, bar_center, detect_h)；bar 中心是 OCR 字幕中心，最可靠。"""
    gray = np.asarray(im.convert("L"), dtype=np.float32)
    bar = it.get("bar") or []
    anchor = (bar[0] + bar[1]) / 2 if len(bar) == 2 and bar[1] > bar[0] else None
    box = find_bar_box(gray, anchor=anchor)
    det_h = (box[1] - box[0]) if box else 0
    center = ((bar[0] + bar[1]) / 2 * im.height) if anchor is not None else \
        ((box[0] + box[1]) / 2 if box else im.height * 0.92)
    return gray, box, center, det_h


def caption_strip(im, it, single_h, tight=False):
    """返回只含黑底白字字幕条的 PIL 图（裁掉两侧画面）。

    tight=True：人物帧专用。常规做法按「暗列 > 0.5」找黑条边界，室内人物帧里
    深色衣物/家具会被算进黑条，导致两侧带出画面碎块。改为用黑条上下纯黑边缘行
    定左右边界，几乎不向外扩，只留黑底本身。
    """
    gray, box, center, det_h = analyze(im, it)
    if det_h >= 90:
        y0, y1 = box                      # 正常（含双行）：用检测到的黑条边界
    else:
        # 短句帧 find_bar_box 会把黑条收窄到 35~79px；manifest 的 bar 是文字框
        # （约 55px，不含黑底上下留白），也不能直接裁。以 bar 中心为锚按
        # 区间内自动算出的单行标准条高向外扩。
        y0 = int(center - single_h / 2)
        y1 = int(center + single_h / 2)
    y0, y1 = max(0, y0), min(im.height, y1)
    band = gray[y0:y1]
    col_frac = (band < DARK).mean(axis=0)

    if tight:
        # 用黑条「上下边缘几行」定左右边界：这些行不含文字，黑底内部几乎全黑，
        # 画面区域则明显亮。直接用整条算会被大字打碎（最长连续黑列只剩几十像素）。
        edge_h = max(2, band.shape[0] // 6)
        edge = np.concatenate([band[:edge_h], band[-edge_h:]], axis=0)
        edge_frac = (edge < DARK).mean(axis=0)
        strong = np.where(edge_frac > 0.8)[0]
        runs = []
        if len(strong):
            s = prev = int(strong[0])
            for x in strong[1:]:
                x = int(x)
                if x != prev + 1:
                    runs.append((s, prev + 1))
                    s = x
                prev = x
            runs.append((s, prev + 1))
            x0, x1 = max(runs, key=lambda r: r[1] - r[0])
            x0, x1 = max(0, x0 - 2), min(im.width, x1 + 2)
        else:
            xs = np.where(col_frac > 0.5)[0]
            x0, x1 = (int(xs.min()), int(xs.max()) + 1) if len(xs) else (0, im.width)
        pad_x, pad_y = 0, 0
    else:
        xs = np.where(col_frac > 0.5)[0]
        if len(xs) == 0:
            x0, x1 = 0, im.width
        else:
            x0, x1 = int(xs.min()), int(xs.max()) + 1
        pad_x, pad_y = 6, 3

    x0 = max(0, x0 - pad_x)
    x1 = min(im.width, x1 + pad_x)
    y0 = max(0, y0 - pad_y)
    y1 = min(im.height, y1 + pad_y)
    return im.crop((x0, y0, x1, y1)), (y0, y1, x0, x1)


def strip_sig(strip):
    """字幕条缩略灰度签名，用于判定相邻帧是不是同一句字幕。"""
    g = strip.convert("L").resize((80, 20), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    return (a - a.mean()) / (a.std() + 1e-6)


def is_same_strip(a, b, thresh=0.93):
    if a.shape != b.shape:
        return False
    return float((a * b).mean()) >= thresh


def collect(start, end, tight=False, dedupe=False):
    items = load_items()
    raw = []
    for n in range(start, end + 1):
        it = items[n - 1]
        path = os.path.join(SUBDIR, f"{n:03d}.jpg")
        if not os.path.exists(path):
            print(f"#{n} 缺图 {path}", file=sys.stderr)
            continue
        im = Image.open(path).convert("RGB")
        _gray, _box, _center, det_h = analyze(im, it)
        raw.append((n, im, it, det_h))
    # 单行标准条高按区间内 det_h 中位数自适应：上期V2≈110px、下期V2 board≈99px。
    # 别写死，也别用 116px 兜底值套到别的片。
    singles = [det_h for _, _, _, det_h in raw if 90 <= det_h <= 150]
    single_h = int(np.median(singles)) if singles else 116
    doubles = [n for n, _, _, det_h in raw if det_h > 150]
    print(f"单行标准条高={single_h}px，双行条={doubles}")
    out = []
    prev_sig = None
    for n, im, it, _det_h in raw:
        strip, bbox = caption_strip(im, it, single_h, tight=tight)
        if dedupe:
            sig = strip_sig(strip)
            if prev_sig is not None and is_same_strip(prev_sig, sig):
                print(f"#{n} 与上一条字幕重复帧，跳过")
                continue
            prev_sig = sig
        out.append((n, strip, it.get("text", "").strip(), bbox))
    return out


def build(strips, outdir, prefix, per_page, gap):
    os.makedirs(outdir, exist_ok=True)
    max_w = max(s.width for _, s, _, _ in strips)
    scale = min(1.0, (W - 2 * 48) / max_w)
    if scale < 1.0:
        strips = [(n, s.resize((round(s.width * scale), round(s.height * scale)), Image.LANCZOS), t, b)
                  for n, s, t, b in strips]
    margin = 60
    usable = H - 2 * margin
    pages, cur, cur_h = [], [], 0
    for item in strips:
        h = item[1].height
        add = h + (gap if cur else 0)
        overflow = cur and (cur_h + add > usable or (per_page > 0 and len(cur) >= per_page))
        if overflow:
            pages.append(cur)
            cur, cur_h, add = [], 0, h
        cur.append(item)
        cur_h += add
    if cur:
        pages.append(cur)
    # 末页过短时自动均衡：按平均条数重排，但每页总高度必须仍放得下
    if per_page <= 0 and len(pages) >= 2:
        import math
        avg = len(strips) / len(pages)
        if len(pages[-1]) < avg * 0.6:
            k = math.ceil(len(strips) / len(pages))
            trial = [strips[i:i + k] for i in range(0, len(strips), k)]
            if all(sum(s.height for _, s, _, _ in pg) + gap * (len(pg) - 1) <= usable
                   for pg in trial):
                print(f"末页仅 {len(pages[-1])} 条 → 自动均衡为每页 {k} 条")
                pages = trial
    paths = []
    for pi, page in enumerate(pages, 1):
        canvas = Image.new("RGB", (W, H), BG)
        total = sum(s.height for _, s, _, _ in page) + gap * (len(page) - 1)
        y = (H - total) // 2
        for n, strip, _text, _box in page:
            canvas.paste(strip, ((W - strip.width) // 2, y))
            y += strip.height + gap
        out = os.path.join(outdir, f"{prefix}_{pi:02d}.jpg")
        canvas.save(out, quality=92)
        paths.append((out, page[0][0], page[-1][0], len(page)))
    return paths


def main():
    global SUBDIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=55)
    ap.add_argument("--end", type=int, default=106)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--outdir", default=os.path.join(PROJECT, "分页草稿", "成图_上期_纯字幕"))
    ap.add_argument("--prefix", default="上期_55-106_纯字幕")
    ap.add_argument("--per-page", type=int, default=0, help="0=按画布高度自动（含末页自动均衡）")
    ap.add_argument("--gap", type=int, default=22)
    ap.add_argument("--tight", action="store_true",
                    help="人物帧必开：只裁纯黑底核心，去掉两侧室内画面碎块")
    ap.add_argument("--dedupe", action="store_true",
                    help="跳过与上一条字幕相同的连续重复帧（人物帧用）")
    ap.add_argument("--subdir", default=SUBDIR,
                    help="字幕帧目录（含 manifest.json），换期用；默认上期V2")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="板书帧必开：不做形状签名去重，保留全部独立短句")
    args = ap.parse_args()

    SUBDIR = args.subdir
    strips = collect(args.start, args.end, tight=args.tight, dedupe=args.dedupe and not args.no_dedupe)
    print(f"共取到 {len(strips)} 条字幕条")
    hs = [s.height for _, s, _, _ in strips]
    ws = [s.width for _, s, _, _ in strips]
    print(f"条高 min/median/max = {min(hs)}/{int(np.median(hs))}/{max(hs)}")
    print(f"条宽 min/median/max = {min(ws)}/{int(np.median(ws))}/{max(ws)}")

    if args.probe:
        for n, s, t, b in strips[:8]:
            print(f"#{n:>3} strip={s.width}x{s.height} box={b} {t[:38]}")
        bad = [(n, s.width, s.height, t[:34]) for n, s, t, _ in strips if s.height < 80 or s.height > 190]
        print(f"\n异常条 {len(bad)} 条：")
        for n, w, h, t in bad:
            print(f"#{n:>3} {w}x{h} {t}")
        return
    if not args.build:
        return
    paths = build(strips, args.outdir, args.prefix, args.per_page, args.gap)
    print(f"共 {len(paths)} 页；白底、无板书、条按原尺寸、间距 {args.gap}px")
    for p, a, b, cnt in paths:
        print(f"  {os.path.basename(p)}  台词 {a}-{b}  {cnt} 条")


if __name__ == "__main__":
    main()
