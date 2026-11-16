#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compose.py — 把字幕图拼成「拼台词」效果（小红书图文笔记用）

核心原则：每张原图保持 9:16 原比例，绝不拉伸/裁切画面。
支持两种版式：
  - vertical : 竖排长图（多张 9:16 原样上下堆叠，适合做「台词脚本」长图）
  - grid     : 宫格（按列数排布，每格内保持比例居中，适合「金句合集」封面）

用法：
  python compose.py <字幕图目录> --layout vertical --start 6 --count 12 --out 竖排.jpg
  python compose.py <字幕图目录> --layout grid --cols 2 --start 6 --count 12 --out 宫格.jpg

可选：
  --label       读取目录内 manifest.json 给每张打时间码
  --gap         间距(像素, 默认 24)
  --bg          背景色(默认 #f2f2f2)
  --quality     输出 jpg 质量(默认 86)
"""
import os, sys, json, argparse
from PIL import Image, ImageDraw, ImageFont
from _compat import find_font

def load_font(size):
    p = find_font()
    if p:
        try: return ImageFont.truetype(p, size, index=1 if p.endswith(".ttc") else 0)
        except Exception:
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return ImageFont.load_default()

def fmt(sec):
    sec = int(sec); m, s = divmod(sec, 60); return f"{m:02d}:{s:02d}"

def list_frames(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".jpg") and f[:3].isdigit())

def pick(frames, start, count):
    if start < 1: start = 1
    sel = frames[start-1:start-1+count]
    return sel

def compose_vertical(d, sel, label_map, gap, bg, quality):
    imgs = [Image.open(os.path.join(d, f)).convert("RGB") for f in sel]
    W = max(im.width for im in imgs)
    H = sum(im.height for im in imgs) + gap * (len(imgs) + 1)
    canvas = Image.new("RGB", (W, H), bg)
    y = gap
    fnt = load_font(max(22, int(W * 0.028)))
    for im, f in zip(imgs, sel):
        # 统一宽度，保持比例
        if im.width != W:
            h = int(im.height * W / im.width); im = im.resize((W, h))
        canvas.paste(im, (0, y))
        if label_map and f in label_map:
            t = label_map[f]
            tw = fnt.getlength(t) + 16
            chip = Image.new("RGBA", (int(tw), int(fnt.size + 12)), (20, 20, 20, 170))
            canvas.paste(chip, (12, y + 12), chip)
            dd = ImageDraw.Draw(canvas)
            dd.text((20, y + 14), t, font=fnt, fill=(255, 255, 255))
        y += im.height + gap
    return canvas

def compose_grid(d, sel, cols, label_map, gap, bg, quality, width=None):
    """方形宫格：cols x rows 个 9:16 画面。cols==rows 时输出整体仍是 9:16。"""
    imgs = [Image.open(os.path.join(d, f)).convert("RGB") for f in sel]
    n = len(imgs); rows = (n + cols - 1) // cols
    if width:
        cell_w = (width - gap * (cols - 1)) // cols
    else:
        cell_w = 540
    cell_h = int(round(cell_w * 16 / 9))  # 9:16 格子
    W = cols * cell_w + gap * (cols - 1)
    H = rows * cell_h + gap * (rows - 1)
    canvas = Image.new("RGB", (W, H), bg)
    fnt = load_font(max(16, int(cell_w * 0.052)))
    for idx, (im, f) in enumerate(zip(imgs, sel)):
        r, c = divmod(idx, cols)
        # 等比缩放到格子内（contain，留边）
        scale = min(cell_w / im.width, cell_h / im.height)
        nw, nh = int(round(im.width * scale)), int(round(im.height * scale))
        im = im.resize((nw, nh), Image.LANCZOS)
        x = c * (cell_w + gap) + (cell_w - nw) // 2
        y = r * (cell_h + gap) + (cell_h - nh) // 2
        canvas.paste(im, (x, y))
        if label_map and f in label_map:
            t = label_map[f]
            tw = fnt.getlength(t) + 12
            chip = Image.new("RGBA", (int(tw), int(fnt.size + 8)), (20, 20, 20, 150))
            canvas.paste(chip, (x + 6, y + 6), chip)
            dd = ImageDraw.Draw(canvas)
            dd.text((x + 12, y + 8), t, font=fnt, fill=(255, 255, 255))
    return canvas

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--layout", choices=["vertical", "grid"], default="vertical")
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--out", default="compose.jpg")
    ap.add_argument("--gap", type=int, default=24)
    ap.add_argument("--bg", default="#f2f2f2")
    ap.add_argument("--label", action="store_true", help="用 manifest 打时间码")
    ap.add_argument("--width", type=int, default=None, help="宫格模式下的输出总宽(默认1080)；cols=rows 时整体比例保持 9:16")
    ap.add_argument("--quality", type=int, default=86)
    args = ap.parse_args()

    frames = list_frames(args.src)
    sel = pick(frames, args.start, args.count)
    if not sel:
        print("没有选中任何图片"); sys.exit(1)

    label_map = {}
    if args.label:
        mj = os.path.join(args.src, "manifest.json")
        if os.path.exists(mj):
            man = json.load(open(mj, encoding="utf-8"))
            m = {it["file"]: it.get("start", 0) for it in man.get("images", [])}
            label_map = {f: fmt(m[f]) for f in sel if f in m}

    if args.layout == "vertical":
        canvas = compose_vertical(args.src, sel, label_map, args.gap, args.bg, args.quality)
    else:
        canvas = compose_grid(args.src, sel, args.cols, label_map, args.gap, args.bg, args.quality, args.width)

    canvas.save(args.out, quality=args.quality)
    print(f"已生成 {args.out}  尺寸 {canvas.size}  含 {len(sel)} 张 (start={args.start})")

if __name__ == "__main__":
    main()
