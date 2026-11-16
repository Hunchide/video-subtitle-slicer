#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lineup.py — 「拼台词」：一张干净底图 + 多行原字幕条竖着摞起来（小红书图文用）

不是宫格拼图！做法是：
  1) 从视频里取一帧「没有字幕」的干净画面当底图（裁成 3:4）
  2) 把连续 N 句字幕的黑条（含文字，原样像素）从字幕图里抠出来
  3) 居中对齐、自下而上摞在底图底部
输出固定 3:4（默认 1080×1440）。

用法：
  python lineup.py <视频> --subdir <字幕图目录> --start 12 --count 3 --out out.jpg
可选：
  --base-time 50.0   手动指定底图时间(秒)
  --width 1080       输出宽度（高度 = 4/3 宽 → 3:4）
  --crop-top 0.20    3:4 裁切起点（占原高比例）
  --gap 16           字幕条间距
  --debug            另存底图与每条裁剪到 <out>_debug/
"""
import os, sys, json, argparse, subprocess, tempfile, shutil
import numpy as np
from PIL import Image, ImageFilter

import _compat

DARK = 70          # 黑条像素阈值
ROW_DARK_FRAC = 0.20   # 行内暗像素占比（含白字行，实测最低 ~0.23）
MIN_BAR_FRAC = 0.015   # 条高至少占画面高度 1.5%
MAX_BAR_FRAC = 0.32    # 超过视为深色衣服等，非字幕


def find_bar_boxes(g, y_from=0.50, y_to=1.0):
    """找到画面下半部所有像字幕黑条的区间，返回 [(y0,y1,x0,x1)]（按 y 升序）。"""
    h, w = g.shape
    a0 = int(h * y_from); a1 = int(h * y_to)
    reg = g[a0:a1]
    if reg.size == 0:
        return []
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

    out = []
    for (ra, rb) in runs:
        hh = rb - ra
        if hh < h * MIN_BAR_FRAC or hh > h * MAX_BAR_FRAC:
            continue
        band = reg[ra:rb]
        mask = band.min(axis=0) < DARK
        xs, xe, cur = 0, 0, None
        for i, v in enumerate(mask):
            if v and cur is None:
                cur = i
            elif not v and cur is not None:
                if i - cur > xe - xs:
                    xs, xe = cur, i
                cur = None
        if cur is not None and len(mask) - cur > xe - xs:
            xs, xe = cur, len(mask)
        if xe - xs < w * 0.15:
            continue
        out.append((a0 + ra, a0 + rb, xs, xe))
    return out


def find_bar_box(g, y_from=0.50, y_to=1.0):
    """兼容旧接口：返回最高的那条字幕黑条 (y0,y1,x0,x1) 或 None。"""
    boxes = find_bar_boxes(g, y_from, y_to)
    if not boxes:
        return None
    return max(boxes, key=lambda b: b[1] - b[0])


def cover_bars(im):
    """把底图上所有字幕黑条抹成纯色（取条内背景色），避免新字幕盖不全留下半截旧字幕。
    返回 (im, boxes)。"""
    g = np.asarray(im.convert("L"), dtype=np.float32)
    boxes = find_bar_boxes(g)
    if not boxes:
        return im, []
    W, H = im.size
    arr = np.asarray(im, dtype=np.uint8).copy()
    for (y0, y1, x0, x1) in boxes:
        pad = max(3, int(H * 0.004))
        ya, yb = max(0, y0 - pad), min(H, y1 + pad)
        xa, xb = max(0, x0 - pad), min(W, x1 + pad)
        patch = arr[ya:yb, xa:xb].reshape(-1, 3)
        # 取最暗的一小撮像素的均值当条底色（字幕条是黑底，白字是少数）
        lum = patch.mean(axis=1)
        dark = patch[lum <= np.percentile(lum, 40)]
        color = dark.mean(axis=0) if len(dark) else np.array([0, 0, 0])
        arr[ya:yb, xa:xb] = color.astype(np.uint8)
    return Image.fromarray(arr), boxes


def get_gray(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.float32)


def extract_frame(video, t, width, out):
    subprocess.run([_compat.FFMPEG, "-y", "-nostdin", "-v", "error", "-ss", f"{t:.3f}",
                    "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-1",
                    "-q:v", "2", out], capture_output=True)
    return os.path.exists(out) and os.path.getsize(out) > 0


def inpaint_bar(im, box, pad=8, blur=14):
    """把底图上的原字幕条抹掉：区域内按上/下边界做竖向插值，再高斯模糊。"""
    W, H = im.size
    y0, y1, x0, x1 = box          # find_bar_box 返回 (y0,y1,x0,x1)
    x0 = max(0, x0 - pad); x1 = min(W, x1 + pad)
    y0 = max(0, y0 - pad); y1 = min(H, y1 + pad)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return im
    arr = np.asarray(im, dtype=np.float32).copy()
    top = arr[max(0, y0 - 10), x0:x1].copy()
    bot = arr[min(H - 1, y1 + 10), x0:x1].copy()
    hgt = y1 - y0
    for j in range(hgt):
        t = (j + 1) / (hgt + 1)
        arr[y0 + j, x0:x1] = top * (1 - t) + bot * t
    out = Image.fromarray(arr.clip(0, 255).astype(np.uint8))
    patch = out.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
    out.paste(patch, (x0, y0))
    return out


def make_base(video, tmpdir, width, t, crop_top, debug_dir=None, clean=False):
    """取 t 秒整帧 → 裁成 3:4 底图。
    默认不抹原字幕：新字幕条会直接贴在原字幕位置把它盖住（够用且快）。
    clean=True 时才做竖向插值抹除（残影兜底，一般不需要）。
    返回 (base, bar_top_canvas)：bar_top_canvas 是原字幕条在裁切后画布里的顶部 y（用于对齐）。"""
    f = os.path.join(tmpdir, "base.jpg")
    if not extract_frame(video, t, width, f):
        raise SystemExit("底图抽帧失败")
    im = Image.open(f).convert("RGB")
    box = find_bar_box(np.asarray(im.convert("L"), dtype=np.float32))
    if box is not None and clean:
        im = inpaint_bar(im, box)
    W, H = im.size
    out_h = int(round(W * 4 / 3))
    y0 = int(H * crop_top)
    if y0 + out_h > H:
        y0 = max(0, H - out_h)
    base = im.crop((0, y0, W, y0 + out_h))
    bar_top = None
    if box is not None:
        bar_top = max(0, box[0] - y0)
    if debug_dir:
        base.save(os.path.join(debug_dir, "base.jpg"))
    return base, bar_top


def crop_bar(img_path, debug_path=None):
    im = Image.open(img_path).convert("RGB")
    g = np.asarray(im.convert("L"), dtype=np.float32)
    box = find_bar_box(g, 0.50, 1.0)
    if box is None:                                  # 找不到条，退回整幅下半
        W, H = im.size
        bar = im.crop((0, int(H * 0.62), W, int(H * 0.80)))
    else:
        y0, y1, x0, x1 = box
        W, H = im.size
        pad = max(3, int(W * 0.004))
        bar = im.crop((max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)))
    if debug_path:
        bar.save(debug_path)
    return bar


def pick_base(video, tmpdir, width, cand_times, ref_bar_h, crop_top, clean=False, debug_dir=None):
    """从候选时间点里挑一张最像「正常口播」的帧当底图。
    打分逻辑：排除幻灯片/黑帧/过曝帧；优先选自带字幕条高度与新字幕条接近的帧
    （这样新字幕条贴上去正好盖住原字幕，不留残影）。
    返回 (base_image, bar_top_in_canvas)"""
    best = None                                   # (score, base, bar_top, t)
    for i, t in enumerate(cand_times):
        if t < 0:
            continue
        f = os.path.join(tmpdir, f"cand_{i}.jpg")
        if not extract_frame(video, t, width, f):
            continue
        im = Image.open(f).convert("RGB")
        W, H = im.size
        small = np.asarray(im.convert("L").resize((108, 192)), dtype=np.float32)
        top = small[int(192 * 0.03):int(192 * 0.55)]
        if float((top > 232).mean()) > 0.30:      # 幻灯片，跳过
            continue
        mean = float(small.mean())
        dark = float((small < 25).mean())
        if mean < 45 or mean > 228 or dark > 0.55:   # 黑帧 / 过曝
            continue
        g = np.asarray(im.convert("L"), dtype=np.float32)
        boxes = find_bar_boxes(g)
        if boxes:
            bh = max(b[1] - b[0] for b in boxes)
            err = abs(bh - ref_bar_h) / max(1.0, ref_bar_h)
            score = 1.0 - min(1.0, err)
            if len(boxes) > 1:                    # 多条字幕通常是过渡/花式画面，降权
                score -= 0.15
        else:
            score = 0.45                          # 无字幕也算可用，但次优
        score -= abs(mean - 140) / 1400.0          # 亮度越接近中间越好（轻微权重）
        if best is None or score > best[0]:
            im, boxes = cover_bars(im)             # 把底图自带字幕抹成纯色
            out_h = int(round(W * 4 / 3))
            y0 = int(H * crop_top)
            if y0 + out_h > H:
                y0 = max(0, H - out_h)
            base = im.crop((0, y0, W, y0 + out_h))
            bar_bottom = None if not boxes else max(0, max(b[1] for b in boxes) - y0)
            best = (score, base, bar_bottom, t)
    if best is None:                              # 全被排除，退回第一句
        t = cand_times[0]
        f = os.path.join(tmpdir, "base_fb.jpg")
        extract_frame(video, t, width, f)
        im = Image.open(f).convert("RGB")
        W, H = im.size
        im, boxes = cover_bars(im)
        out_h = int(round(W * 4 / 3))
        y0 = int(H * crop_top)
        if y0 + out_h > H:
            y0 = max(0, H - out_h)
        base = im.crop((0, y0, W, y0 + out_h))
        best = (0, base, None if not boxes else max(0, max(b[1] for b in boxes) - y0), t)
    if debug_dir:
        best[1].save(os.path.join(debug_dir, "base.jpg"))
    return best[1], best[2], best[3]


def render_lineup(video, subdir, sel, out, width=1080, crop_top=0.20, gap=16,
                  bottom_margin=56, base_time=None, clean=False, quality=90, debug_dir=None):
    """核心渲染：base 帧 + 把 sel（manifest 里的条目列表）的字幕条摞上去。
    返回 (canvas_size, base_time)。"""
    tmp = tempfile.mkdtemp(prefix="lineup_")
    try:
        bars = []
        for it in sel:
            dp = os.path.join(debug_dir, f"bar_{it['file']}") if debug_dir else None
            bars.append(crop_bar(os.path.join(subdir, it["file"]), dp))

        if base_time is not None:
            cand = [base_time]
        else:
            cand = []
            for it in sel:
                cand += [(it["start"] + it["end"]) / 2]
                cand += [it["start"] + 0.15, max(it["start"], it["end"] - 0.15)]
            cand = cand[:12]
        ref_h = max(b.height for b in bars)
        base, anchor, btime = pick_base(video, tmp, width, cand, ref_h, crop_top, clean, debug_dir)
        W, H = base.size
        canvas = base.copy()

        maxw = int(W * 0.94)
        norm = []
        for b in bars:
            if b.width > maxw:
                nh = int(round(b.height * maxw / b.width))
                b = b.resize((maxw, nh), Image.LANCZOS)
            norm.append(b)

        avail = int(H * 0.55)
        total = sum(b.height for b in norm) + gap * (len(norm) - 1)
        if total > avail:
            k = avail / total
            norm = [b.resize((max(1, int(b.width * k)), max(1, int(b.height * k))), Image.LANCZOS)
                    for b in norm]
            total = sum(b.height for b in norm) + gap * (len(norm) - 1)

        # 对齐：字幕条整摞的「底部」对齐原字幕区底部，往上长 → 原字幕全被盖住；
        # 底图没有字幕时贴底。
        bottom_limit = H - bottom_margin
        end = anchor if anchor is not None else bottom_limit
        end = min(max(end, 0), H - 8)
        y = end - total
        if y < 8:                                  # 放不下就整摞缩一点
            k = max(0.4, (end - 8) / total) if total > 0 else 1
            norm = [b.resize((max(1, int(b.width * k)), max(1, int(b.height * k))), Image.LANCZOS)
                    for b in norm]
            total = sum(b.height for b in norm) + gap * (len(norm) - 1)
            y = max(0, end - total)

        for b in norm:
            canvas.paste(b, ((W - b.width) // 2, y))
            y += b.height + gap

        canvas.save(out, quality=quality)
        return canvas.size, btime
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--subdir", required=True)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--out", default="lineup.jpg")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--crop-top", type=float, default=0.20)
    ap.add_argument("--gap", type=int, default=16)
    ap.add_argument("--bottom-margin", type=int, default=56)
    ap.add_argument("--base-time", type=float, default=None)
    ap.add_argument("--clean-base", action="store_true",
                    help="先把底图自带字幕抹掉（默认不抹，直接由新字幕条盖住）")
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    man = json.load(open(os.path.join(args.subdir, "manifest.json"), encoding="utf-8"))
    imgs = man["images"]
    sel = imgs[args.start - 1: args.start - 1 + args.count]
    if not sel:
        raise SystemExit("选中的字幕为空")

    dbg = None
    if args.debug:
        dbg = os.path.splitext(args.out)[0] + "_debug"
        os.makedirs(dbg, exist_ok=True)

    size, btime = render_lineup(args.video, args.subdir, sel, args.out,
                                width=args.width, crop_top=args.crop_top, gap=args.gap,
                                bottom_margin=args.bottom_margin, base_time=args.base_time,
                                clean=args.clean_base, quality=args.quality, debug_dir=dbg)
    print(f"✓ {args.out}  {size}  ratio={size[0]/size[1]:.4f}  "
          f"句数={len(sel)}  底图t={btime:.2f}s")


if __name__ == "__main__":
    main()
