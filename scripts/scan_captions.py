#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_captions.py — 用 OCR 扫烧录字幕，得到「一句不漏」的字幕时间轴

为什么要有这个脚本：
  早先靠「边缘图变化 + IoU 去重」猜换句，会漏 —— 两条字幕字形相近（如「想转出海」/「想进外企」）
  被判成同一句；字幕条位置跳动时检测带跑偏，整段漏掉。漏了以后拼台词就上下句连不上。
  改成 OCR 读字：读到的文字直接当身份，**同字才算同一句**，不靠相似度猜。

怎么判断「这是字幕」而不是幻灯片里的字：
  真字幕永远压在一层**深色底条**上（黑底白字）。所以看 OCR 框内的背景亮度中位数，
  背景暗 = 字幕；背景亮（米色卡、截图）= 幻灯片正文，不算字幕。
  没有字幕的时段（幻灯片讲解）单独记成 kind=slide，避免整段内容丢失。

流程：
  1) ffmpeg 按 fps 抽帧（默认 4fps，短句也能抓到）
  2) 逐帧 OCR（RapidOCR，CPU 就够快）
  3) 过滤：顶部水印、过小的截图文字
  4) 选「字幕行」：深色底 + 水平居中；双行字幕合并成一条
  5) 按文字归并连续帧 → 一条字幕 = 一个 item
  6) 没有字幕的连续时段（≥1.2s、画面有内容）→ 一条 kind=slide
  7) 落 manifest.json（和旧版同结构，build_pages / make_script_list 直接可用）

用法：
  python scan_captions.py <video> --review          # 只看时间轴/空档，不出图
  python scan_captions.py <video>                   # 出图 + manifest.json
  python scan_captions.py <video> --json /tmp/ocr.json   # 缓存 OCR，反复调参不用重跑
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

import _compat

WATERMARK = re.compile(r"^@|Speaking|抖音|小红书号|剪映", re.I)
PUNCT = " \u3000.,，。、!！?？:：;；\"'“”‘’()（）[]【】-—_·…~～"
CJK = re.compile(r"[\u4e00-\u9fff]")


def norm(t):
    t = t or ""
    for ch in PUNCT:
        t = t.replace(ch, "")
    return t.strip()


# ---------- OCR ----------

def ocr_frames(files, fps, progress=True):
    """逐帧 OCR，返回每帧的字幕候选 + 画面统计（用于判断幻灯片段）。"""
    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR()
    out, n = [], len(files)
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        W, H = im.size
        g = np.asarray(im.convert("L"), dtype=np.float32)
        try:
            res, _ = ocr(np.asarray(im))
        except Exception:
            res = None
        boxes = []
        for item in (res or []):
            box, txt, score = item[0], item[1], float(item[2])
            if score < 0.42 or not norm(txt):
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            y0, y1 = int(min(ys)), int(max(ys))
            x0, x1 = int(min(xs)), int(max(xs))
            y0c, y1c = max(0, y0), min(H, y1 + 1)
            x0c, x1c = max(0, x0), min(W, x1 + 1)
            patch = g[y0c:y1c, x0c:x1c]
            boxes.append({
                "text": txt.strip(), "score": score,
                "yc": (y0 + y1) / 2 / H, "y0": y0 / H, "y1": y1 / H,
                "xc": (x0 + x1) / 2 / W, "x0": x0 / W, "x1": x1 / W,
                "h": (y1 - y0) / H,
                "bg": float(np.median(patch)) if patch.size else 0.0,
            })
        out.append({"t": i / fps, "boxes": boxes,
                    "mean": float(g.mean()), "std": float(g.std())})
        if progress and (i % 300 == 0 or i == n - 1):
            print(f"    OCR {i + 1}/{n}", flush=True)
    return out


def merge_lines(cand):
    """双行字幕：相邻、居中对齐、间距小 → 合成一条。"""
    merged = []
    for b in cand:
        if merged:
            p = merged[-1]
            if (abs(p["xc"] - b["xc"]) < 0.12 and 0 < b["y0"] - p["y1"] < p["h"] * 0.9):
                p["text"] += b["text"]
                p["y1"] = b["y1"]
                p["yc"] = (p["y0"] + p["y1"]) / 2
                p["score"] = min(p["score"], b["score"])
                p["bg"] = min(p["bg"], b["bg"])
                continue
        merged.append(dict(b))
    return merged


def scene_of(fr):
    """画面类型：person = 真人出镜（上半屏没字）；board = 板书/提纲页（上半屏一堆字）。

    拼台词只用 person——板书页整页塞进横带会变成一片白底小字，没法看。
    出图后会用 skin_ratio 覆写，那个才是准的（OCR 版只用于 review）。"""
    n = sum(1 for b in fr["boxes"]
            if b["yc"] < 0.45 and not WATERMARK.search(b["text"]))
    return "board" if n >= 2 else "person"


def skin_ratio(im):
    """画面中央的肤色像素占比：真人半身出镜 ≈0.5+，板书页只有右下角小头像 ≈0.01。"""
    import numpy as np
    a = np.asarray(im.convert("YCbCr"), dtype=np.int16)
    H, W, _ = a.shape
    reg = a[int(H * 0.20):int(H * 0.72), int(W * 0.18):int(W * 0.82)]
    Y, Cb, Cr = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
    m = (Cb >= 77) & (Cb <= 130) & (Cr >= 132) & (Cr <= 175) & (Y > 60)
    return float(m.mean())


def scene_of_image(path, thresh=0.30):
    """按画面本身判定：真人出镜 / 板书页。"""
    from PIL import Image
    try:
        im = Image.open(path)
    except Exception:
        return "board"
    return "person" if skin_ratio(im) >= thresh else "board"


def pick_caption(boxes, bg_thresh=105, yc_min=0.45, yc_max=0.995):
    """挑出这一帧的真字幕。深色底 = 字幕；背景亮 = 幻灯片正文，不算。
    返回 (text, meta) 或 None。

    bg_thresh 是「字幕框内背景亮度中位数」上限：
      默认 105 适配黑底白/黄字条（上期V2 / 下期V2）。
      白字无底条的口播片（如 转行不降薪.mp4，白字描边直接压在深色衣服/浅墙上）
      框内亮度常在 117~205，会被 105 全挡掉 → 用 --bg-thresh 256 放开亮度判据，
      此时仍靠 yc 下半屏 + 水平居中 + 水印过滤兜住。
    yc_min/yc_max 是字幕纵向带（占整帧高度比例）：
      放开 bg_thresh 后，片头黄底/黑底大字卡、logo、片尾关注卡也会被当成字幕，
      它们的 yc 常在 0.50~0.71；真字幕稳定在 0.78 左右 → 用 --cap-yc-min 0.74 挡掉。
      默认 0.45 保持历史行为不变。
    """
    cand = [b for b in boxes
            if yc_min <= b["yc"] <= yc_max
            and b["h"] >= 0.014                       # 太小的截图文字不算
            and not WATERMARK.search(b["text"])]
    if not cand:
        return None
    cand.sort(key=lambda b: b["yc"])
    dark = [b for b in cand if b["bg"] < bg_thresh]    # 暗底判据（阈值可调）
    if not dark:
        return None
    merged = merge_lines(dark)
    centered = [b for b in merged
                if 0.40 <= b["xc"] <= 0.60 and (b["x1"] - b["x0"]) <= 0.94]
    b = centered[-1] if centered else merged[-1]
    return b["text"], b


def group_frames(ocrs, fps, bg_thresh=105, yc_min=0.45, yc_max=0.995):
    """把有字幕的连续帧按文字归并；无字幕的连续帧并成 slide 段。"""
    dt = 1.0 / fps
    caps, slides = [], []
    for fr in ocrs:
        got = pick_caption(fr["boxes"], bg_thresh, yc_min, yc_max)
        fr["cap"] = got
        key = norm(got[0]) if got else ""
        if key:                                    # ---- 有字幕 ----
            if caps and caps[-1]["key"] == key:
                caps[-1]["frames"].append(fr)
                caps[-1]["end"] = fr["t"]
            else:
                caps.append({"key": key, "text": got[0], "frames": [fr],
                             "start": fr["t"], "end": fr["t"]})
        else:                                      # ---- 无字幕（幻灯片/转场）----
            if slides and fr["t"] - slides[-1]["end"] <= dt * 1.5:
                slides[-1]["frames"].append(fr)
                slides[-1]["end"] = fr["t"]
            else:
                slides.append({"frames": [fr], "start": fr["t"], "end": fr["t"]})
    return caps, slides


def frame_sig(fr):
    """一帧的「画面文字签名」：去掉水印后的文字拼接（大字报/卡片页用它判页）。"""
    return "".join(norm(b["text"]) for b in fr["boxes"] if not WATERMARK.search(b["text"]))


def split_slide(s, fps):
    """把一段「无黑条字幕」的画面按**界面文字变化**拆成多页。
    大字报、图文卡常和片头 logo、黑场连成一段，整段只取一帧必然丢页。"""
    import difflib
    dt = 1.0 / fps
    subs = []
    for fr in s["frames"]:
        sig = frame_sig(fr)
        if subs:
            prev = subs[-1]
            sim = 1.0 if sig == prev["sig"] else (
                difflib.SequenceMatcher(None, prev["sig"], sig).ratio() if sig and prev["sig"] else 0.0)
            if sim >= 0.6 and fr["t"] - prev["end"] <= dt * 1.5:
                prev["frames"].append(fr)
                prev["end"] = fr["t"]
                if len(sig) > len(prev["sig"]):
                    prev["sig"] = sig
                continue
        subs.append({"sig": sig, "frames": [fr], "start": fr["t"], "end": fr["t"]})
    return subs


def dedupe_slides(rows):
    """同一页幻灯片上压过一条字幕（字幕条盖在卡片上），会被拆成两条同画面的页；
    丢后面那条，时间顺延。判据用「几乎同一页」的 0.9 相似度，避免把相邻两页幻灯片误并。"""
    import difflib
    out = []
    for r in rows:
        if r["kind"] == "slide" and out:
            k, crossed = len(out) - 1, 0
            while k >= 0 and out[k]["kind"] != "slide" and crossed < 2:
                crossed += 1
                k -= 1
            if k >= 0 and out[k]["kind"] == "slide" and crossed <= 1:
                a, b = norm(out[k]["text"]), norm(r["text"])
                if a and b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.9:
                    out[k]["end"] = r["end"]
                    continue
        out.append(r)
    return out


def drop_slide_echo(rows, fps):
    import difflib
    """大字卡压过一瞬（≤0.6s）被同时读成字幕的，删掉那条假字幕——它和卡片文字重复。"""
    out = []
    for r in rows:
        if r["kind"] == "caption" and (r["end"] - r["start"]) <= 0.6:
            t = norm(r["text"])
            near = [x for x in rows if x["kind"] == "slide" and x is not r
                    and x["start"] - 1.0 <= r["start"] <= x["end"] + 1.0]
            if t and any(t in norm(x["text"]) or norm(x["text"]) in t
                         or difflib.SequenceMatcher(None, t, norm(x["text"])).ratio() >= 0.8
                         for x in near):
                continue
        out.append(r)
    return out


def pick_rep(g):
    """组内代表帧：取组内 50%~80% 处（避开出场动画和退场边缘），
    且该帧 OCR 到的文字必须与整组一致——避免截到「字幕刚切换」那一帧的上一句。"""
    frs = g["frames"]
    n = len(frs)
    if n == 1:
        return frs[0]
    lo = int(n * 0.5)
    hi = max(lo + 1, int(round(n * 0.8)) + 1)
    pool = [f for f in frs[lo:hi] if f.get("cap") and norm(f["cap"][0]) == g["key"]]
    if not pool:
        pool = [f for f in frs[lo:hi] if f.get("cap")] or frs[lo:hi]
    return max(pool, key=lambda f: f["cap"][1]["score"] if f.get("cap") else 0.0)


def merge_prefix(items, fps):
    """出场动画没打完字的半句、或同一句的 OCR 抖动（≤0.6s 且文字高度相似）并进相邻条。"""
    import difflib
    out = []
    for it in items:
        dur = it["end"] - it["start"] + 1 / fps
        if out and dur <= 0.6:
            a, b = norm(out[-1]["key"]), norm(it["key"])
            sim = difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0
            if a and b and (b.startswith(a) or a.startswith(b) or sim >= 0.75):
                longer = out[-1] if (len(a), out[-1]["end"]) >= (len(b), it["end"]) else it
                out[-1] = {"key": longer["key"], "text": longer["text"],
                           "frames": out[-1]["frames"] + it["frames"],
                           "start": out[-1]["start"], "end": it["end"]}
                continue
        out.append(it)
    return out


def clean_captions(items, fps):
    """去杂：纯英文/数字的极短噪声（转场 logo、日期戳）。"""
    out = []
    for it in items:
        t = it["text"]
        dur = it["end"] - it["start"] + 1 / fps
        if not CJK.search(t) and len(norm(t)) <= 8 and dur <= 1.2:
            continue
        out.append(it)
    return out


def snap_and_check(video, r, dst, frame_w, ocr, expect, tmpdir):
    """按候选时间点截帧，OCR 回读校验是不是期望那句；不匹配就换点重截。
    返回 (ok, 实际用的时间点)。OCR 时间戳和真实画面差半帧时，这一步兜住。"""
    import difflib
    span = max(0.2, r["end"] - r["start"])
    cands = [r["t"]]
    for f in (0.70, 0.45, 0.88, 0.30, 0.15):
        t = r["start"] + span * f
        if all(abs(t - c) > 0.08 for c in cands):
            cands.append(t)
    want = norm(expect)
    tmpf = os.path.join(tmpdir, "_chk.jpg")

    def grab(t):
        t = max(0.0, min(t, max(0.0, r["end"] - 0.02)))
        subprocess.run([_compat.FFMPEG, "-y", "-nostdin", "-v", "error", "-ss", f"{t:.3f}",
                        "-i", video, "-frames:v", "1", "-vf", f"scale={frame_w}:-1",
                        "-q:v", "2", tmpf], capture_output=True)
        return os.path.exists(tmpf) and os.path.getsize(tmpf) > 0

    for t in cands:
        if not grab(t):
            continue
        try:
            res, _ = ocr(np.asarray(Image.open(tmpf).convert("RGB")))
        except Exception:
            res = None
        got = norm("".join(x[1] for x in (res or [])))
        ok = bool(want) and (want in got or (got and got in want)
                             or difflib.SequenceMatcher(None, want, got).ratio() >= 0.55)
        if ok:
            shutil.copyfile(tmpf, dst)
            return True, t
    grab(cands[0])
    shutil.copyfile(tmpf, dst)
    return False, cands[0]


def main():
    ap = argparse.ArgumentParser(description="OCR 扫烧录字幕 → manifest（一句不漏）")
    ap.add_argument("video")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--scale-w", type=int, default=540)
    ap.add_argument("--frame-w", type=int, default=1080, help="最终出图宽度")
    ap.add_argument("--quality", type=int, default=92)
    ap.add_argument("--no-slides", action="store_true", help="不补无字幕的幻灯片段")
    ap.add_argument("--review", action="store_true", help="只打印时间轴，不出图")
    ap.add_argument("--no-verify", action="store_true",
                    help="不做出图后 OCR 回读校验（快，但可能截到上一句）")
    ap.add_argument("--json", default=None, help="OCR 结果缓存路径")
    ap.add_argument("--bg-thresh", type=float, default=105,
                    help="字幕框背景亮度中位数上限，默认 105 适配黑底字幕条；"
                         "白字无底条的口播片设 256 放开亮度判据（仍靠位置/居中/水印过滤兜底）")
    ap.add_argument("--cap-yc-min", type=float, default=0.45,
                    help="字幕中心纵向下限（占帧高比例），默认 0.45；"
                         "放开 bg-thresh 后片头大字卡/片尾关注卡会混入，白字片设 0.74 挡掉")
    args = ap.parse_args()

    if not os.path.exists(args.video):
        sys.exit(f"找不到视频：{args.video}")
    out_dir = args.out_dir or os.path.join(
        os.path.dirname(args.video),
        os.path.splitext(os.path.basename(args.video))[0] + "_字幕图")

    tmp = tempfile.mkdtemp(prefix="ocrscan_")
    try:
        pat = os.path.join(tmp, "f_%05d.jpg")
        print(f"→ 抽帧 {args.fps}fps ...", flush=True)
        subprocess.run([_compat.FFMPEG, "-nostdin", "-v", "error", "-i", args.video,
                        "-vf", f"fps={args.fps},scale={args.scale_w}:-1",
                        "-q:v", "3", pat], capture_output=True)
        files = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.startswith("f_"))
        if not files:
            sys.exit("抽帧失败")
        print(f"   {len(files)} 帧，开始 OCR ...", flush=True)

        if args.json and os.path.exists(args.json):
            ocrs = json.load(open(args.json, encoding="utf-8"))
            print(f"   复用 OCR 缓存 {args.json}")
        else:
            ocrs = ocr_frames(files, args.fps)
            if args.json:
                json.dump(ocrs, open(args.json, "w", encoding="utf-8"), ensure_ascii=False)

        caps, slides = group_frames(ocrs, args.fps, args.bg_thresh, args.cap_yc_min)
        caps = clean_captions(merge_prefix(caps, args.fps), args.fps)

        rows = []
        for it in caps:
            rep = pick_rep(it)
            meta = rep["cap"][1] if rep.get("cap") else {}
            rows.append({"kind": "caption", "text": it["text"],
                         "start": round(it["start"], 2),
                         "end": round(it["end"] + 1 / args.fps, 2),
                         "t": round(rep["t"], 2),
                         "bar": [round(meta.get("y0", 0), 3), round(meta.get("y1", 0), 3)],
                         "score": round(meta.get("score", 0), 2),
                         "scene": scene_of(rep)})
        if not args.no_slides:
            for s in slides:
                # 一段无字幕画面里可能摞了好几页（大字报 + 片头 logo + 黑场），先按文字变化拆开
                for sub in split_slide(s, args.fps):
                    dur = sub["end"] - sub["start"] + 1 / args.fps
                    frs = sub["frames"]
                    # 取后段（动画播完、字全出来），不能取中点——逐行弹出的卡片中点常只有半屏字
                    mid = frs[min(len(frs) - 1, int(round(len(frs) * 0.8)))]
                    if dur < 0.45 or mid["std"] < 15 or mid["mean"] < 25:
                        continue
                    pool_all = [b for b in mid["boxes"] if not WATERMARK.search(b["text"])]
                    # 白底 + 居中 + 极短 → 片头/片尾署名板（「小茶和 Frank」那种），不占一条
                    if (mid["mean"] >= 200 and 0 < len(pool_all) <= 2
                            and len(norm("".join(b["text"] for b in pool_all))) <= 8
                            and all(0.30 <= b["yc"] <= 0.70 for b in pool_all)):
                        continue
                    # 用后段几帧的 OCR 文字拼成这页画面的说明（给台词清单看）
                    txts = []
                    for fr in frs[int(len(frs) * 0.5): max(int(len(frs) * 0.5) + 1,
                                                            int(round(len(frs) * 0.92)))]:
                        pool = [b for b in fr["boxes"]
                                if not WATERMARK.search(b["text"]) and b["score"] >= 0.55]
                        if not pool:
                            continue
                        # 一帧里可能同时有大字和下面一行小字（「跨境支付」+「-Fintech-」），都收
                        for b in sorted(pool, key=lambda b: b["yc"])[:3]:
                            if norm(b["text"]) not in [norm(t) for t in txts]:
                                txts.append(b["text"])
                    if not txts:                       # 纯转场/空镜：没字就别占一条
                        continue
                    # 页内动画会把同一句读成「半句 + 整句」，留最长的
                    txts = [t for t in txts
                            if not any(norm(t) != norm(o) and norm(t) in norm(o) for o in txts)]
                    rows.append({"kind": "slide", "text": " / ".join(txts[:3]),
                                 "start": round(sub["start"], 2),
                                 "end": round(sub["end"] + 1 / args.fps, 2),
                                 "t": round(mid["t"], 2), "bar": [], "score": 0,
                                 "scene": "board"})
        rows.sort(key=lambda r: r["start"])
        rows = dedupe_slides(rows)
        rows = drop_slide_echo(rows, args.fps)

        if args.review:
            prev, n_gap = 0.0, 0
            for i, r in enumerate(rows, 1):
                gap = r["start"] - prev
                mark = ""
                if gap > 1.2:
                    n_gap += 1
                    mark = f"  ⚠ 空档 {gap:.2f}s"
                tag = ("" if r["kind"] == "caption" and r.get("scene") == "person"
                       else f"[{r['kind']}/{r.get('scene')}] ")
                print(f"{i:03d} {r['start']:7.2f}-{r['end']:7.2f} {tag}{r['text'][:36]}{mark}")
                prev = r["end"]
            dur = subprocess.run([_compat.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                                  "-of", "default=nw=1:nk=1", args.video],
                                 capture_output=True, text=True).stdout.strip()
            print(f"→ 共 {len(rows)} 条；空档 {n_gap} 处；视频 {float(dur or 0):.2f}s，"
                  f"最后一条到 {prev:.2f}s")
            return

        os.makedirs(out_dir, exist_ok=True)
        manifest = {"video": os.path.abspath(args.video), "mode": "ocr",
                    "fps": args.fps, "count": len(rows), "images": []}
        ocr = None
        if not args.no_verify:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            print("→ 出图并逐张回读校验 ...", flush=True)
        bad, fixed = [], 0
        for i, r in enumerate(rows, 1):
            name = f"{i:03d}.jpg"
            dst = os.path.join(out_dir, name)
            if ocr is not None and r["kind"] == "caption":
                ok, used = snap_and_check(args.video, r, dst, args.frame_w, ocr,
                                          r["text"], tmp)
                if abs(used - r["t"]) > 0.05:
                    fixed += 1
                if not ok:
                    bad.append((i, r["text"]))
            else:
                subprocess.run([_compat.FFMPEG, "-y", "-nostdin", "-v", "error",
                                "-ss", f"{r['t']:.3f}", "-i", args.video, "-frames:v", "1",
                                "-vf", f"scale={args.frame_w}:-1", "-q:v", "2", dst],
                               capture_output=True)
                used = r["t"]
            if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
                print(f"  ! 截帧失败 #{i} @ {r['t']}s")
                continue
            manifest["images"].append({
                "file": name, "text": r["text"], "kind": r["kind"],
                "start": r["start"], "end": r["end"], "t": round(used, 2),
                "bar": r["bar"], "ocr_score": r["score"],
                "scene": scene_of_image(dst)})
        json.dump(manifest, open(os.path.join(out_dir, "manifest.json"), "w",
                                 encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✓ 完成：{len(manifest['images'])} 张 → {out_dir}")
        if ocr is not None:
            print(f"  回读校验：自动挪点 {fixed} 张；仍对不上 {len(bad)} 张")
            for i, t in bad[:15]:
                print(f"    #{i} {t[:30]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
