#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_pages.py — 按「人工指定的分页」批量出精确 3:4 台词拼图

为什么要有这个：拼台词的价值在于**语言节奏**——每张图最后一句要留钩子让人想翻下一张。
这种判断只有人能做，所以流程是「先看清单挑句子 → 写分页 → 一键出图」。

分页文件（pages.txt）每行一页，支持范围和逗号：
    4-7
    8,9,10,11
    # 井号开头是注释
    12-15   # 这一页末尾留「所以我从来不会先说数字」当钩子

用法：
    # 1) 先看清单（序号 + 时间 + 参考文本），规划分页
    python build_pages.py <视频> --subdir <字幕图目录> --list

    # 2) 按分页出图（精确 1080×1440）
    python build_pages.py <视频> --subdir <字幕图目录> --pages pages.txt --outdir ./台词图 --prefix 上期

    # 只检查版式放不放得下，不真的出图
    python build_pages.py <视频> --subdir <字幕图目录> --pages pages.txt --check
"""
import os, sys, re, argparse, tempfile, shutil, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("strip_stack", os.path.join(HERE, "strip_stack.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def fmt_t(t):
    m, s = divmod(int(t), 60)
    return f"{m:02d}:{s:02d}"


def parse_pages(path):
    """解析分页文件 → [(indices, note)]"""
    pages = []
    for raw in open(path, encoding="utf-8"):
        line = raw.split("#", 1)
        body, note = line[0].strip(), (line[1].strip() if len(line) > 1 else "")
        if not body:
            if note:
                pages.append(([], "[注释] " + note))
            continue
        idx = []
        for part in re.split(r"[,，\s]+", body):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                idx.extend(range(int(a), int(b) + 1))
            else:
                idx.append(int(part))
        pages.append((idx, note))
    return [p for p in pages if p[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--subdir", required=True)
    ap.add_argument("--pages", default=None, help="分页文件")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--prefix", default="page")
    ap.add_argument("--list", action="store_true", help="打印台词清单")
    ap.add_argument("--check", action="store_true", help="只检查版式，不出图")
    ap.add_argument("--start-line", type=int, default=1)
    ap.add_argument("--end-line", type=int, default=0, help="0=到末尾")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--ratio", type=float, default=0.75)
    ap.add_argument("--min-pad", type=int, default=18)
    ap.add_argument("--strip-ratio", type=float, default=1.55)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--skip", default="", help="要排除的序号，逗号分隔（如重复残句 24,34,171）")
    ap.add_argument("--only", default="", help="只出这几页，逗号/范围（如 1-3,7）；按分页文件里的页序")
    ap.add_argument("--slide-frac", type=float, default=0.34,
                    help="整页卡片行占画高比例（默认 0.34）")
    ap.add_argument("--keep-slides", action="store_true",
                    help="不跳过幻灯片页（大字卡会作为整块插入，不裁成横带）")
    ap.add_argument("--slide-mode", default="raw", choices=("text", "raw", "full"),
                    help="卡片行版式：raw=按原图 1:1 裁，字号跟原片一致（默认）；"
                         "text=裁出大字铺满整宽（会缩放）；full=整页缩略")
    ap.add_argument("--slide-pad-bottom", type=int, default=0,
                    help="大字卡行：下边界再往下多带多少原画面（字不动，于是字偏上、底部留呼吸）")
    ap.add_argument("--slide-pad-top", type=int, default=0,
                    help="大字卡行：上边界再往上多带多少原画面；传负数=上边界往下切，字跟上一句贴更近")
    ap.add_argument("--headroom", type=float, default=0.05,
                    help="头图头顶上方多露多少画面（占画高比例，默认 0.05；调大如 0.16 头顶上方露出更多）")
    ap.add_argument("--header-fit", default="contain", choices=("contain", "cover"),
                    help="头图放不下时：contain=缩小+两侧模糊；cover=不缩放，从顶部往下原像素裁")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="关闭按字幕条形状签名去重；用于短句字形相近被误判重复（如「发卡和付款」/「卡组织和银行」）的区间。"
                         "默认仍去重，历史结果不受影响")
    ap.add_argument("--header-top", type=float, default=-1.0,
                    help="头图从画面哪个纵向比例开始切；默认 -1 = 自动找头顶。"
                         "板书/白板段传 0 强制从画面最顶部切，避免 detect_head_top 把板书首行黑字当头顶、"
                         "把顶部大标题裁掉（用户要求「保留板书完整」时用）")
    ap.add_argument("--tight-slide", action="store_true",
                    help="紧挨大字卡的那条横带底部不留白（贴更紧）；默认关闭，"
                         "关着才保证每一处空隙等高")
    args = ap.parse_args()

    items = ss.load_items(args.subdir)
    end = args.end_line or len(items)

    if args.list:                              # 只列清单
        print(f"{'#':>4}  {'时间':>6}  参考文本（以图为准）")
        for i in range(args.start_line, min(end, len(items)) + 1):
            it = items[i - 1]
            print(f"{i:>4}  {fmt_t(it['start']):>6}  {it.get('text','').strip()[:60]}")
        return

    if not args.pages:
        raise SystemExit("要么 --list，要么 --pages")

    pages = parse_pages(args.pages)
    if not pages:
        raise SystemExit("分页文件是空的")

    skipset = {int(x) for x in re.split(r"[,，\s]+", args.skip) if x.strip()}
    if skipset:
        pages = [( [i for i in idx if i not in skipset], note) for idx, note in pages]
        pages = [p for p in pages if p[0]]

    if args.only:                              # 只出某几页
        keep = set()
        for part in re.split(r"[,，\s]+", args.only):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                keep.update(range(int(a), int(b) + 1))
            else:
                keep.add(int(part))
        pages = [(pi, idx, note) for pi, (idx, note) in enumerate(pages, 1) if pi in keep]
    else:
        pages = [(pi, idx, note) for pi, (idx, note) in enumerate(pages, 1)]

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="pages_")
    try:
        print(f"{'页':>3}  {'行':>2}  {'尺寸':>10}  {'末尾一句（钩子）'}")
        print("-" * 72)
        rows_report = []
        for pi, idx, note in pages:
            skip_kinds = () if args.keep_slides else ("slide",)
            frames, used, bars = ss.grab_frames(args.video, items, idx, args.width, tmp,
                                                dedupe=not args.no_dedupe, tag=f"p{pi}",
                                                skip_kinds=skip_kinds)
            slide_rows = [k for k, i in enumerate(used)
                          if items[i - 1].get("kind") == "slide"]
            if not frames:
                print(f"{pi:>3}  ✗  抽帧失败：{idx}")
                continue
            canvas, info = ss.compose_page(
                frames, width=args.width, ratio=args.ratio, bars=bars,
                strip_ratio=args.strip_ratio, min_pad=args.min_pad, quality=args.quality,
                slide_rows=slide_rows, slide_frac=args.slide_frac,
                slide_mode=args.slide_mode,
                slide_pad_bottom=args.slide_pad_bottom,
                slide_pad_top=args.slide_pad_top, headroom=args.headroom,
                header_fit=args.header_fit, tight_slide=args.tight_slide,
                header_top=args.header_top)
            last_text = items[max(used) - 1].get("text", "").strip()[:34]
            flag = "" if info["exact"] else f"  ⚠放不下：本页最多 {info['max_rows']} 行"
            if info.get("header_scaled"):
                flag += "  ↕首图缩"
            dropped = [i for i in idx if i not in used
                       and i <= len(items) and items[i - 1].get("kind") != "slide"]
            skipped = [i for i in idx if i not in used
                       and i <= len(items) and items[i - 1].get("kind") == "slide"]
            drop_s = ""
            if dropped:
                drop_s += f"  ⚠去重:{dropped}"
            if skipped:
                drop_s += f"  ↩跳过幻灯片:{skipped}"
            print(f"{pi:>3}  {len(frames):>2}  {canvas.size[0]}×{canvas.size[1]}  "
                  f"{last_text}{flag}{drop_s}")
            if note:
                print(f"      └ {note}")
            if not args.check and args.outdir:
                out = os.path.join(args.outdir, f"{args.prefix}_{pi:02d}.jpg")
                canvas.save(out, quality=args.quality)
            rows_report.append(info)

        ok = sum(1 for r in rows_report if r["exact"])
        print("-" * 72)
        print(f"共 {len(rows_report)} 张，精确 3:4 命中 {ok}/{len(rows_report)}")
        if rows_report:
            r0 = rows_report[0]
            print(f"该片第一行大图下每页最多 {r0['max_rows']} 行"
                  f"（字幕条 ~{r0['min_strip'] - 36}px + 上下各 18px 留白）")
        if args.outdir and not args.check:
            print(f"输出目录：{args.outdir}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
