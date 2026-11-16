#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_compat.py — 跨平台兼容层（Windows / macOS / Linux 通用）。

所有脚本 `from _compat import ...` 即可，无需各自处理系统差异：
  - FFMPEG / FFPROBE : 二进制路径，默认走 PATH，可用环境变量 FFMPEG_BIN / FFPROBE_BIN 覆盖
  - font_candidates() / find_font() / load_font() : 按 OS 探测中文字体
  - pil_getdata() : Pillow 新旧版图像像素迭代兼容
  - resolve_asr_backend() / asr_transcribe() : whisper 双后端（mlx-whisper / openai-whisper）自适应

设计目标：纯命令行、agent 无关、零硬编码个人路径。任何 agent 复制整个 scripts/ 目录即可用。
"""
import os
import platform
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------- ffmpeg ----
# 默认走 PATH；可用环境变量指向自定义二进制（Windows 上常见 C:\ffmpeg\bin\ffmpeg.exe）
FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_BIN") or "ffprobe"


def set_ffmpeg(ffmpeg_bin=None, ffprobe_bin=None):
    """运行时覆盖二进制路径（供 --ffmpeg 参数调用）。"""
    global FFMPEG, FFPROBE
    if ffmpeg_bin:
        FFMPEG = ffmpeg_bin
    if ffprobe_bin:
        FFPROBE = ffprobe_bin


# ----------------------------------------------------------------- fonts ----
def font_candidates():
    """按当前 OS 返回中文字体候选路径列表（可先由 CJK_FONT 环境变量指定）。"""
    env = os.environ.get("CJK_FONT")
    sysname = platform.system()
    if sysname == "Windows":
        base = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
        cands = [
            os.path.join(base, "Fonts", "msyh.ttc"),
            os.path.join(base, "Fonts", "msyhbd.ttc"),
            os.path.join(base, "Fonts", "simhei.ttf"),
            os.path.join(base, "Fonts", "simsun.ttc"),
            os.path.join(base, "Fonts", "Deng.ttf"),
        ]
    elif sysname == "Darwin":
        cands = [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/PingFang.ttc",
        ]
    else:  # Linux / BSD / 其它
        cands = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    return ([env] if env else []) + [p for p in cands if p]


def find_font():
    """返回第一个存在的字体路径；找不到返回 None（调用方退化为默认字体）。"""
    for p in font_candidates():
        if p and os.path.exists(p):
            return p
    return None


def load_font(size, path=None):
    """跨平台加载字体：优先指定 path，其次按 OS 探测；失败返回 PIL 默认字体。"""
    from PIL import ImageFont
    path = path or find_font()
    if path and os.path.exists(path):
        for idx in (1, 0):  # .ttc 常见中文字形在 index 1
            try:
                return ImageFont.truetype(path, int(size), index=idx)
            except Exception:
                continue
    return ImageFont.load_default()


def pil_getdata(img):
    """像素迭代兼容：Pillow 旧版 getdata() / 新版 get_flattened_data()。返回 list。"""
    if hasattr(img, "getdata"):
        return list(img.getdata())
    if hasattr(img, "get_flattened_data"):
        return list(img.get_flattened_data())
    raise AttributeError("Pillow image has neither getdata() nor get_flattened_data()")


# ------------------------------------------------------------------- ASR ----
try:
    import mlx_whisper
    HAVE_MLX = True
except Exception:
    mlx_whisper = None
    HAVE_MLX = False

try:
    import whisper as openai_whisper
    HAVE_OW = True
except Exception:
    openai_whisper = None
    HAVE_OW = False

MODEL_MLX = "mlx-community/whisper-large-v3-turbo"
MODEL_OW = "large-v3-turbo"


def resolve_asr_backend(prefer="auto"):
    """选择听写后端。auto：mlx（Apple Silicon GPU）优先，否则 openai-whisper。"""
    if prefer == "mlx":
        if not HAVE_MLX:
            sys.exit("未安装 mlx_whisper：python -m pip install mlx-whisper")
        return "mlx"
    if prefer == "openai":
        if not HAVE_OW:
            sys.exit("未安装 openai-whisper：python -m pip install openai-whisper")
        return "openai"
    # auto
    if HAVE_MLX:
        return "mlx"
    if HAVE_OW:
        return "openai"
    sys.exit("未安装任何听写后端。Apple Silicon 装 mlx-whisper：python -m pip install mlx-whisper；"
             "其它平台装 openai-whisper：python -m pip install openai-whisper")


def asr_transcribe(video, backend="auto", language="zh", initial_prompt=None):
    """整段 ASR，返回统一的 segments 列表（含 text/start/end/words）。

    两个后端输出结构对齐：mlx-whisper 原生返回 segments；openai-whisper 也返回 segments。
    """
    backend = resolve_asr_backend(backend)
    audio = os.path.join(tempfile.gettempdir(), "vss_asr_%d.wav" % os.getpid())
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", video,
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio],
            capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("抽音频失败：" + (r.stderr or "")[:300])
        prompt = initial_prompt or "以下是一段普通话视频，含有出镜解说与字幕。请使用简体中文输出，并带上正常的中文标点。"
        if backend == "mlx":
            res = mlx_whisper.transcribe(
                audio, path_or_hf_repo=MODEL_MLX, language=language,
                initial_prompt=prompt, condition_on_previous_text=False,
                word_timestamps=True, verbose=False)
        else:
            res = openai_whisper.transcribe(
                audio, model=MODEL_OW, language=language,
                initial_prompt=prompt, condition_on_previous_text=False,
                word_timestamps=True, verbose=False)
        return res.get("segments", [])
    finally:
        if os.path.exists(audio):
            try:
                os.unlink(audio)
            except OSError:
                pass


def backend_label(backend):
    """给日志用的后端显示名。"""
    return "mlx-whisper (Apple GPU)" if backend == "mlx" else "openai-whisper (CPU)"
