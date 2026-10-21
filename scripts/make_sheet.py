#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「字幕图」目录里的 manifest + 图片，拼成「字幕总览」长图（每行一条字幕 + 序号时间）。

用法：
  python make_sheet.py <字幕图目录>            # 默认每页 18 行
  python make_sheet.py <字幕图目录> --per-page 24
  python make_sheet.py <字幕图目录> --band 0.700,0.800   # 只裁字幕行（比例）

输出：<目录>/总览_1.jpg, 总览_2.jpg ...
没装中文字体会退化为无标签（仅图片）。
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def load_font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def fmt_ts(t):
    t = int(round(t or 0))
    return f"{t // 60:02d}:{t % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description="字幕图 → 字幕总览长图")
    ap.add_argument("dir", help="字幕图目录（含 manifest.json）")
    ap.add_argument("--per-page", type=int, default=18, help="每页多少行字幕")
    ap.add_argument("--band", default="0.700,0.800", help="裁字幕行的纵向比例")
    ap.add_argument("--width", type=int, default=1080, help="总览图宽度")
    ap.add_argument("--quality", type=int, default=88)
    args = ap.parse_args()

    mpath = os.path.join(args.dir, "manifest.json")
    if not os.path.exists(mpath):
        sys.exit(f"找不到 manifest：{mpath}")
    items = json.load(open(mpath, encoding="utf-8"))["images"]
    if not items:
        sys.exit("manifest 里没有图片")

    y0, y1 = (float(x) for x in args.band.split(","))
    font = load_font(int(args.width * 0.032))
    label_w = int(args.width * 0.14)
    gap = 10

    pages = [items[i:i + args.per_page] for i in range(0, len(items), args.per_page)]
    for pi, batch in enumerate(pages, 1):
        rows = []
        for it in batch:
            p = os.path.join(args.dir, it["file"])
            if not os.path.exists(p):
                continue
            im = Image.open(p).convert("RGB")
            w, h = im.size
            band = im.crop((0, int(h * y0), w, int(h * y1)))
            tw = args.width - label_w
            th = max(1, int(band.height * tw / band.width))
            rows.append((it, band.resize((tw, th)), th))
        if not rows:
            continue
        total_h = sum(r[2] for r in rows) + gap * len(rows) + 70
        sheet = Image.new("RGB", (args.width, total_h), (245, 245, 245))
        d = ImageDraw.Draw(sheet)
        d.text((24, 18), f"字幕总览 {pi}/{len(pages)}（每行一条字幕=一张图）",
               font=font, fill=(30, 30, 30))
        y = 70
        for it, band, th in rows:
            d.text((14, y + th // 2 - int(args.width * 0.018)), fmt_ts(it.get("start", 0)),
                   font=font, fill=(160, 40, 40))
            sheet.paste(band, (label_w, y))
            d.line([(0, y + th + gap // 2), (args.width, y + th + gap // 2)],
                   fill=(220, 220, 220), width=1)
            y += th + gap
        out = os.path.join(args.dir, f"总览_{pi}.jpg")
        sheet.save(out, quality=args.quality)
        print(f"✓ {out}  {sheet.size[0]}x{sheet.size[1]}  {len(rows)} 行")

    print(f"\n共 {len(items)} 条字幕，分 {len(pages)} 页")


if __name__ == "__main__":
    main()
