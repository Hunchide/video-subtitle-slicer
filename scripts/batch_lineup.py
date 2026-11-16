#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_lineup.py — 批量「拼台词」：自动挑出口播段，每 N 句出一张 3:4 拼图。

做法：
  1) 读 <字幕图目录>/manifest.json，逐张判断是「口播」还是「幻灯片/文字密集画面」
     （画面上方大片纯白 → 幻灯片，拼台词不好看，自动跳过）
  2) 口播段内按 N 句一组切分（末尾只剩 1 句的并进前一组，避免单句图）
  3) 每组调 lineup.render_lineup：取一帧当底图 + 把这几句的原字幕条摞上去

用法：
  python batch_lineup.py <视频> --subdir <字幕图目录> --outdir <输出目录> --per 3

可选：
  --per 3            每图几句（默认 3）
  --min-group 2      少于该句数的组直接跳过（默认 2）
  --include-slide    连幻灯片段也拼（默认跳过）
  --white 0.30       幻灯片判定阈值：上方白像素占比
  --width 1080 / --gap 16 / --bottom-margin 56 / --quality 90
"""
import os, sys, json, argparse, subprocess, importlib.util
import numpy as np
from PIL import Image

import _compat

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lu = _load("lineup")


def is_slide(path, thresh=0.30):
    im = Image.open(path).convert("L").resize((108, 192))
    a = np.asarray(im, dtype=np.float32)
    top = a[int(192 * 0.03):int(192 * 0.55)]
    return float((top > 232).mean()) > thresh


def runs_of(flags):
    """把布尔序列切成连续段，返回 [(start_idx, end_idx, value)]"""
    segs, cur, s = [], flags[0], 0
    for i in range(1, len(flags)):
        if flags[i] != cur:
            segs.append((s, i - 1, cur)); cur = flags[i]; s = i
    segs.append((s, len(flags) - 1, cur))
    return segs


def chunk(items, per, min_group):
    """按 per 切组；末尾剩 1 句并进前一组"""
    out = []
    for i in range(0, len(items), per):
        out.append(items[i:i + per])
    if len(out) >= 2 and len(out[-1]) == 1:
        out[-2].extend(out[-1]); out.pop()
    return [g for g in out if len(g) >= min_group]


def fmt(t):
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--subdir", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--per", type=int, default=3)
    ap.add_argument("--min-group", type=int, default=2)
    ap.add_argument("--include-slide", action="store_true")
    ap.add_argument("--skip-tail", type=float, default=0.0,
                    help="跳过最后 N 秒的组（片尾关注引导卡拼台词会打架，建议 10）")
    ap.add_argument("--white", type=float, default=0.30)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--crop-top", type=float, default=0.20)
    ap.add_argument("--gap", type=int, default=16)
    ap.add_argument("--bottom-margin", type=int, default=56)
    ap.add_argument("--quality", type=int, default=90)
    args = ap.parse_args()

    man = json.load(open(os.path.join(args.subdir, "manifest.json"), encoding="utf-8"))
    imgs = man["images"]

    flags = [is_slide(os.path.join(args.subdir, it["file"]), args.white) for it in imgs]
    segs = runs_of(flags)
    talk = sum(1 for f in flags if not f)
    print(f"字幕图共 {len(imgs)} 张：口播 {talk}、幻灯片 {len(imgs)-talk}")

    groups = []
    for a, b, slide in segs:
        if slide and not args.include_slide:
            print(f"  跳过幻灯片 {imgs[a]['file']}–{imgs[b]['file']}（{b-a+1} 张）")
            continue
        for g in chunk(imgs[a:b + 1], args.per, args.min_group):
            groups.append(g)

    # 可选：丢掉片尾段（关注引导浮层卡）
    if args.skip_tail > 0:
        dur = float(subprocess.run(
            [_compat.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", args.video],
            capture_output=True, text=True).stdout.strip() or 0)
        if dur > 0:
            kept = [g for g in groups if g[-1]["end"] < dur - args.skip_tail]
            if len(kept) != len(groups):
                print(f"  跳过片尾 {args.skip_tail:.0f}s 内 {len(groups)-len(kept)} 组")
            groups = kept

    os.makedirs(args.outdir, exist_ok=True)
    index = []
    for n, g in enumerate(groups, 1):
        out = os.path.join(args.outdir, f"{n:02d}.jpg")
        size, btime = lu.render_lineup(args.video, args.subdir, g, out,
                                       width=args.width, crop_top=args.crop_top,
                                       gap=args.gap, bottom_margin=args.bottom_margin,
                                       quality=args.quality)
        index.append({
            "file": os.path.basename(out),
            "sources": [it["file"] for it in g],
            "start": round(g[0]["start"], 2),
            "end": round(g[-1]["end"], 2),
            "texts": [it.get("text", "") for it in g],
            "base_time": round(btime, 2),
        })
        print(f"  ✓ {n:02d}.jpg  {fmt(g[0]['start'])}–{fmt(g[-1]['end'])}  {len(g)} 句  {size[0]}x{size[1]}")

    json.dump({"video": os.path.abspath(args.video), "per": args.per,
               "count": len(index), "images": index},
              open(os.path.join(args.outdir, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n完成：{len(groups)} 张 → {args.outdir}")


if __name__ == "__main__":
    main()
