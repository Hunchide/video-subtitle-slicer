#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_script_list.py — 生成「台词清单」：把每条烧录字幕原样裁出来，带上序号和时间码
用途：人工挑句子、规划「每张图哪几句 + 最后一句留钩子」，再交给 build_pages.py 出图。

用法：
  python make_script_list.py --subdir <字幕图目录> --prefix 上期 --outdir <目录>
"""
import os, re, sys, argparse, importlib.util
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from _compat import find_font

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("strip_stack", os.path.join(HERE, "strip_stack.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def load_font(size):
    p = find_font()
    if p:
        try:
            return ImageFont.truetype(p, size, index=1)
        except Exception:
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def fmt(t):
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subdir", required=True)
    ap.add_argument("--prefix", default="清单")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--rows", type=int, default=18, help="每页多少条")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=0)
    args = ap.parse_args()

    items = ss.load_items(args.subdir)
    end = args.end or len(items)
    outdir = args.outdir or args.subdir
    os.makedirs(outdir, exist_ok=True)

    cells = []
    for i in range(args.start, min(end, len(items)) + 1):
        it = items[i - 1]
        f = os.path.join(args.subdir, it["file"])
        im = Image.open(f).convert("RGB")
        g = np.asarray(im.convert("L"), dtype=np.float32)
        bar = it.get("bar") or []
        if it.get("kind") == "slide" or not bar:
            if it.get("kind") == "slide":        # 幻灯片：整帧缩略，看得见版面
                crop = im.crop((0, int(im.height * 0.10), im.width, int(im.height * 0.95)))
            else:                                # 没黑条的帧：取底部固定高度
                crop = im.crop((0, im.height - 170, im.width, im.height))
            if bar:
                y0 = int(max(0, bar[0] * im.height) - 4)
                y1 = int(min(im.height, bar[1] * im.height) + 4)
                if y1 - y0 >= 10:
                    crop = im.crop((0, y0, im.width, y1))
        else:                                    # 有 OCR 传下来的字幕条位置：直接用
            y0 = int(max(0, bar[0] * im.height) - 6)
            y1 = int(min(im.height, bar[1] * im.height) + 6)
            if y1 - y0 < 10:
                box = ss.find_bar_box(g)
                y0, y1 = (box[0] - 4, box[1] + 4) if box else (im.height - 170, im.height)
            y0, y1 = max(0, y0), min(im.height, y1)
            if y1 - y0 > 300:
                y0 = y1 - 300
            crop = im.crop((0, y0, im.width, y1))
        cells.append((i, it["start"], crop, it.get("kind") == "slide"))

    gutter = 112
    strip_w = args.width - gutter
    font = load_font(26)
    slide_h = 300
    pages = [cells[i:i + args.rows] for i in range(0, len(cells), args.rows)]
    for pg, chunk in enumerate(pages, 1):
        row_hs = [slide_h if c[3] else int(c[2].height * strip_w / c[2].width) for c in chunk]
        row_h = max(row_hs) + 14
        sheet = Image.new("RGB", (args.width, row_h * len(chunk)), (255, 255, 255))
        d = ImageDraw.Draw(sheet)
        for k, (i, t, crop, is_slide) in enumerate(chunk):
            y = k * row_h
            if is_slide:                        # 幻灯片按高度贴，居中，不拉伸
                h = min(slide_h, row_h - 14)
                w = int(crop.width * h / crop.height)
                sheet.paste(crop.resize((w, h), Image.LANCZOS),
                            (gutter + max(0, (strip_w - w) // 2), y + 7))
            else:
                h = int(crop.height * strip_w / crop.width)
                sheet.paste(crop.resize((strip_w, h), Image.LANCZOS), (gutter, y + 7))
            d.line([(0, y), (args.width, y)], fill=(228, 228, 228), width=1)
            d.text((10, y + 8), f"{i:03d}", font=font, fill=(190, 0, 0))
            d.text((10, y + 40), fmt(t), font=font, fill=(90, 90, 90))
            if is_slide:
                d.text((10, y + 72), "幻灯", font=font, fill=(150, 150, 150))
        out = os.path.join(outdir, f"{args.prefix}_台词清单_{pg}.jpg")
        sheet.save(out, quality=88)
        print(f"✓ {out}  {sheet.size[0]}×{sheet.size[1]}  第 {chunk[0][0]}–{chunk[-1][0]} 条")

    # 同时落一份文本清单（ASR 参考文本，便于按关键词搜）
    md = os.path.join(outdir, f"{args.prefix}_台词清单.md")
    has_ocr = any(it.get("kind") for it in items)
    with open(md, "w", encoding="utf-8") as fp:
        fp.write(f"# {args.prefix} 台词清单（序号 / 时间 / 字幕文本）\n\n")
        fp.write("> " + ("文本为 OCR 读出的烧录字幕原文；幻灯片段标 [幻灯片]，文字只是版面摘要。"
                         if has_ocr else "文本为 ASR 参考，可能有错字；**以图片里烧录字幕为准**。")
                 + "\n\n")
        fp.write("| # | 时间 | 字幕文本 |\n|---|---|---|\n")
        for i, t, _c, _s in cells:
            txt = items[i - 1].get("text", "").strip().replace("|", "／")
            if items[i - 1].get("kind") == "slide":
                txt = f"[幻灯片] {txt}"
            fp.write(f"| {i} | {fmt(t)} | {txt} |\n")
    print(f"✓ {md}")


if __name__ == "__main__":
    main()
