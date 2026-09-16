# -*- coding: utf-8 -*-
"""AI 视频总结 —— 核心处理流程

流程：
  网页视频（yt-dlp，优先提取自带字幕）──┐
                                        ├→ 文字稿 ──→ DeepSeek 大模型 ──→ 结构化总结
  本地文件（内嵌字幕 / 提取音轨）───────┘     ↑
        没有字幕时：音轨 → 云端 Qwen3-ASR 语音转写
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
WORK_ROOT = BASE_DIR / "work"      # 纯临时目录：只放中间产物，用完即删
OUTPUT_ROOT = BASE_DIR / "输出"    # 成品目录：转录稿 + 总结，永久保留
COOKIES_FILE = BASE_DIR / "cookies.txt"  # 可选：B站等需要登录的视频，导出 cookies 放这里

# 正在跑的临时目录（已 resolve），清理时跳过，避免误删在用的文件
_ACTIVE_WORKDIRS: set[Path] = set()

CHUNK_SECONDS = 600      # 转写切分：10 分钟/段（16kHz 单声道 wav 约 19MB，远低于接口限制）
MAX_PART_CHARS = 12000   # 总结时单次送入模型的文本长度上限

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.siliconflow.cn",
    "asr_model": "Qwen/Qwen3-ASR-1.7B",
    "llm_model": "deepseek-ai/DeepSeek-V4-Flash",
}

DEFAULT_PROMPT = """你是专业的视频内容总结助手。请根据视频《{title}》的转录稿（方括号内是时间点），输出一份结构化总结，用中文书写：

# 视频总结：{title}

## 一句话概括
（视频讲了什么，50 字以内）

## 核心要点
（按主题分条列出，每条末尾标注大约时间点，如「(12:30)」；转录稿中方括号 [hh:mm:ss] 就是时间标记）

## 关键结论
（视频最重要的观点或结论）

## 值得摘录的原话
（2-3 句有代表性的话，没有可省略）

要求：忠实于原文，不要编造转录稿中没有的内容；要点覆盖视频前后各部分，不要只写开头内容。"""


# ---------------------------------------------------------------- 进度上报

# 流程步骤（顺序即界面显示顺序，数值是该步骤在「总进度」里的权重）
STEP_DEFS = [
    ("fetch",      "获取视频",   0.10),
    ("audio",      "提取音轨",   0.22),
    ("transcribe", "语音转写",   0.42),
    ("summarize",  "生成总结",   0.23),
    ("export",     "保存转录稿", 0.03),
]


class Reporter:
    """流程进度收集器：把「日志 + 各步骤状态 + 步骤内进度」打包成快照回调给界面。

    快照结构（界面直接拿去渲染）：
        {"steps": [{"key", "label", "status", "detail", "frac"}, ...],
         "overall": 0.0~1.0, "logs": [...], "finished": bool}

    步骤状态：pending 等待 / running 进行中 / done 完成 / skip 跳过 / fail 失败
    """

    EMIT_INTERVAL = 0.15   # 秒；限流，避免高频回调把界面刷爆
    MAX_LOGS = 300

    def __init__(self, on_change=None, on_log=None, on_progress=None):
        self._on_change = on_change or (lambda snap: None)
        self._on_log = on_log or (lambda msg: None)
        self._on_progress = on_progress or (lambda frac, detail: None)
        self.steps = [{"key": k, "label": label, "status": "pending",
                       "detail": "", "frac": 0.0} for k, label, _ in STEP_DEFS]
        self._weights = {k: w for k, _, w in STEP_DEFS}
        self.logs = []
        self.finished = False
        self._last_emit = 0.0

    # -------------------------------------------------- 快照 / 回调
    def snapshot(self) -> dict:
        total = sum(self._weights.values()) or 1.0
        got = 0.0
        for s in self.steps:
            w = self._weights[s["key"]]
            if s["status"] in ("done", "skip"):
                got += w                      # 跳过的不占时间，直接算完成
            elif s["status"] == "running":
                got += w * s["frac"]
        return {"steps": [dict(s) for s in self.steps],
                "overall": min(1.0, got / total),
                "logs": list(self.logs),
                "finished": self.finished}

    def _emit(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_emit < self.EMIT_INTERVAL:
            return
        self._last_emit = now
        snap = self.snapshot()
        self._on_change(snap)
        running = next((s for s in snap["steps"] if s["status"] == "running"), None)
        self._on_progress(snap["overall"], running["detail"] if running else "")

    # -------------------------------------------------- 给流程调用
    def _get(self, key: str) -> dict:
        for s in self.steps:
            if s["key"] == key:
                return s
        raise KeyError(f"未知步骤：{key}")

    def update(self, key, status=None, frac=None, detail=None, force=False) -> None:
        s = self._get(key)
        if status is not None:
            s["status"] = status
        if frac is not None:
            s["frac"] = max(0.0, min(1.0, float(frac)))
        if detail is not None:
            s["detail"] = str(detail)
        self._emit(force=force)

    def start(self, key, detail="") -> None:
        """开始一个步骤（进度归零）"""
        self.update(key, status="running", frac=0.0, detail=detail, force=True)

    def progress(self, key, frac, detail=None) -> None:
        """步骤内进度 0~1；同一段内只增不减，避免界面来回跳"""
        s = self._get(key)
        if s["status"] == "running":
            frac = max(s["frac"], float(frac))
        self.update(key, status="running", frac=frac, detail=detail)

    def done(self, key, detail=None) -> None:
        self.update(key, status="done", frac=1.0, detail=detail, force=True)

    def skip(self, key, detail=None) -> None:
        if self._get(key)["status"] == "skip":   # 已标记过就别覆盖说明文字
            return
        self.update(key, status="skip", frac=1.0, detail=detail, force=True)

    def fail(self, key, detail=None) -> None:
        self.update(key, status="fail", detail=detail, force=True)

    def fail_running(self, detail="") -> None:
        """出错时把当前进行中的那一步标红"""
        for s in self.steps:
            if s["status"] == "running":
                self.fail(s["key"], detail)
                return
        self._emit(force=True)

    def finish(self) -> None:
        self.finished = True
        self._emit(force=True)

    def log(self, msg) -> None:
        text = str(msg)
        self.logs.append(text)
        if len(self.logs) > self.MAX_LOGS:
            del self.logs[:-self.MAX_LOGS]
        self._on_log(text)
        self._emit()


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- ffmpeg

def get_ffmpeg() -> str:
    """优先系统 ffmpeg，否则用 imageio-ffmpeg 内置的 ffmpeg"""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run_ffmpeg(args: list, total_seconds: float = 0, on_progress=None) -> None:
    """执行 ffmpeg。

    给了 total_seconds（已知素材时长）就解析 ffmpeg 的 -progress 输出，
    按 0~1 回调 on_progress(frac)，界面据此显示转码进度。
    """
    tracked = bool(on_progress) and total_seconds > 0
    cmd = [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    if tracked:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += args

    if not tracked:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 执行失败：{proc.stderr.strip()[:500]}")
        return

    # 要读进度：stdout 走管道逐行解析，stderr 落临时文件（避免管道写满导致卡死）
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True, encoding="utf-8", errors="replace")
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            m = re.match(r"out_time=(\d+):(\d{2}):(\d{2})", line)
            if m:
                done = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                on_progress(min(1.0, done / total_seconds))
        proc.wait()
        errf.seek(0)
        err = errf.read()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 执行失败：{err.strip()[:500]}")
    on_progress(1.0)


def _get_duration(path: Path) -> int:
    proc = subprocess.run([get_ffmpeg(), "-i", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2})[.]\d+", proc.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return 0


# ---------------------------------------------------------------- 网页视频提取

def _base_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "retries": 3}
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    opts.update(extra)
    return opts


def _download_hook(d: dict, rep: Reporter) -> None:
    """yt-dlp 下载进度：占「提取音轨」步骤的前 80%（后 20% 是转码）"""
    status = d.get("status")
    got = d.get("downloaded_bytes") or 0
    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
    if status == "downloading":
        if total:
            frac = got / total
            rep.progress("audio", 0.80 * frac,
                         f"下载音轨 {frac * 100:.0f}%（{_mb(got)}/{_mb(total)}）")
        else:
            rep.progress("audio", 0.10, f"下载音轨中（已下载 {_mb(got)}）")
    elif status == "finished":
        rep.progress("audio", 0.80, f"音轨下载完成（{_mb(got)}），正在转换格式…")
    elif status == "error":
        rep.log("音轨下载出错，正在重试…")


def _postproc_hook(d: dict, rep: Reporter) -> None:
    if d.get("status") == "started":
        rep.progress("audio", 0.82, "转换为 mp3…")


def fetch_web(url: str, workdir: Path, rep: Reporter) -> dict:
    """提取网页视频，返回 {"kind": "subtitle"|"audio", "title": ..., "text"/"audio_path": ...}"""
    import yt_dlp

    rep.start("fetch", "解析网页地址")
    rep.log("正在解析网页视频地址…")
    info = None
    try:
        with yt_dlp.YoutubeDL(_base_opts(skip_download=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        rep.log(f"直接解析未成功（{type(e).__name__}），尝试从网页源码中寻找视频…")
        media_url, page_title = _probe_page(url, rep.log)
        if media_url:
            rep.log(f"找到嵌入视频：{media_url}")
            try:
                with yt_dlp.YoutubeDL(_base_opts(skip_download=True)) as ydl:
                    info = ydl.extract_info(media_url, download=False)
            except Exception as e2:
                raise RuntimeError(f"提取嵌入视频失败：{e2}") from e2
        else:
            raise RuntimeError(
                "无法从该网页提取视频。请确认：① 网址是视频页面；"
                "② 视频无需登录即可观看；③ 站点在 yt-dlp 支持范围内。") from e

    title = (info or {}).get("title") or "未命名视频"
    rep.log(f"视频标题：{title}")
    rep.progress("fetch", 0.6, "尝试提取自带字幕")

    # 1) 优先尝试自带字幕（有字幕就不需要转写，最快最准）
    rep.log("尝试提取视频自带字幕…")
    try:
        with yt_dlp.YoutubeDL(_base_opts(
            skip_download=True,
            writesubtitles=True, writeautomaticsub=True,
            subtitleslangs=["all"], subtitlesformat="srt/vtt/json3",
            paths={"home": str(workdir)}, outtmpl="subs.%(ext)s",
            ignoreerrors=True, overwrites=True,
        )) as ydl:
            ydl.download([url])
    except Exception as e:
        rep.log(f"字幕提取出错（将改用语音转写）：{e}")

    sub_text = _find_best_subtitle(workdir)
    if sub_text:
        rep.log(f"✅ 成功提取字幕（{len(sub_text)} 字），跳过语音转写")
        rep.done("fetch", f"标题：{title}")
        rep.skip("audio", "自带字幕，无需音轨")
        rep.skip("transcribe", "自带字幕，无需转写")
        return {"kind": "subtitle", "title": title, "text": sub_text}

    rep.done("fetch", f"标题：{title}")

    # 2) 没有字幕则下载音轨
    rep.start("audio", "下载音轨")
    rep.log("未找到可用字幕，下载视频音轨…")
    try:
        with yt_dlp.YoutubeDL(_base_opts(
            format="bestaudio/best",
            outtmpl=str(workdir / "audio.%(ext)s"),
            ffmpeg_location=get_ffmpeg(),
            postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            overwrites=True,
            progress_hooks=[lambda d: _download_hook(d, rep)],
            postprocessor_hooks=[lambda d: _postproc_hook(d, rep)],
        )) as ydl:
            ydl.download([url])
    except Exception as e:
        raise RuntimeError(f"音轨下载失败：{e}") from e

    audio = _find_audio_file(workdir)
    if audio is None:
        raise RuntimeError("音轨下载完成但未找到音频文件，请重试")
    rep.log(f"✅ 音轨就绪：{audio.name}（{_mb(audio.stat().st_size)}）")

    # 3) 统一转成 16kHz 单声道 wav（转写接口最稳的格式），完成后「提取音轨」才算结束
    wav = to_wav(audio, workdir, rep, lo=0.80)
    rep.done("audio", f"{wav.name}（{_mb(wav.stat().st_size)}）")
    _unlink(audio)      # 后面转写只用 wav，原始下载档（mp3，几十 MB）立刻删掉
    return {"kind": "audio", "title": title, "audio_path": wav}


def to_wav(src: Path, workdir: Path, rep: Reporter, lo: float = 0.0,
           name: str = "full.wav") -> Path:
    """把任意音/视频转成 16kHz 单声道 wav（云端转写最稳的格式），带进度"""
    out = workdir / name
    duration = _get_duration(src)
    rep.progress("audio", lo, "读取音频信息…")

    def on_frac(f):
        rep.progress("audio", lo + (1.0 - lo) * f, f"转换为 16kHz 单声道 wav {f * 100:.0f}%")

    run_ffmpeg(["-i", str(src), "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", str(out)],
               total_seconds=duration, on_progress=on_frac)
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError("音频转换失败：没有输出有效音频")
    return out


def _probe_page(url: str, log) -> tuple:
    """yt-dlp 解析失败时的兜底：从网页源码里找视频（B站/YouTube 嵌入、mp4/m3u8 直链）"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        html = r.text
    except Exception as e:
        log(f"网页抓取失败：{e}")
        return (None, "")

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    bv = re.search(r"[Bb][Vv][0-9A-Za-z]{10}", html)
    if bv:
        return (f"https://www.bilibili.com/video/{bv.group(0)}", title)
    yt = re.search(r"youtube\.com/(?:embed|v)/([0-9A-Za-z_-]{11})", html)
    if yt:
        return (f"https://www.youtube.com/watch?v={yt.group(1)}", title)

    found = []
    for u in re.findall(r'https?://[^\s"\'<>\\]+?\.(?:m3u8|mp4)[^\s"\'<>\\]*', html):
        if u not in found:
            found.append(u)
    for u in re.findall(r'src=["\']([^"\']+?\.(?:m3u8|mp4)[^"\']*)["\']', html, re.I):
        u = urljoin(url, u)
        if u not in found:
            found.append(u)
    if found:
        return (found[0], title)
    return (None, title)


def _find_audio_file(workdir: Path):
    audio_exts = {".mp3", ".m4a", ".aac", ".opus", ".webm", ".wav", ".mka", ".ogg", ".flac"}
    cands = [p for p in workdir.iterdir() if p.suffix.lower() in audio_exts]
    if not cands:
        return None
    cands.sort(key=lambda p: -p.stat().st_size)
    return cands[0]


# ---------------------------------------------------------------- 本地文件

def process_local(path, workdir: Path, rep: Reporter, title=None) -> dict:
    src = Path(path)
    if not src.exists():
        raise RuntimeError(f"文件不存在：{path}")
    title = title or src.stem
    rep.start("fetch", "读取本地文件")
    rep.log(f"本地文件：{src.name}")

    # 尝试内嵌字幕
    rep.progress("fetch", 0.5, "检查内嵌字幕")
    sub_path = workdir / "embedded.srt"
    try:
        run_ffmpeg(["-i", str(src), "-map", "0:s:0", "-c:s", "srt", str(sub_path)])
    except RuntimeError:
        sub_path = None  # 没有字幕流
    if sub_path and sub_path.exists() and sub_path.stat().st_size > 0:
        text = _parse_srt(sub_path.read_text(encoding="utf-8", errors="replace"))
        if len(text.strip()) >= 20:
            rep.log(f"✅ 提取到内嵌字幕（{len(text.strip())} 字），跳过语音转写")
            rep.done("fetch", f"文件：{src.name}")
            rep.skip("audio", "内嵌字幕，无需音轨")
            rep.skip("transcribe", "内嵌字幕，无需转写")
            return {"kind": "subtitle", "title": title, "text": text.strip()}

    rep.done("fetch", f"文件：{src.name}")

    # 提取音轨 → 16kHz 单声道 wav
    rep.start("audio", "提取音轨")
    rep.log("未找到内嵌字幕，提取音轨…")
    wav = workdir / "audio.wav"
    run_ffmpeg(["-i", str(src), "-vn", "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", str(wav)],
               total_seconds=_get_duration(src),
               on_progress=lambda f: rep.progress("audio", f, f"提取音轨 {f * 100:.0f}%"))
    if not wav.exists() or wav.stat().st_size < 1000:
        raise RuntimeError("未能从文件中提取音轨（文件可能损坏或没有声音）")
    rep.done("audio", f"{wav.name}（{_mb(wav.stat().st_size)}）")
    return {"kind": "audio", "title": title, "audio_path": wav}


# ---------------------------------------------------------------- 字幕解析

def _find_best_subtitle(workdir: Path) -> str | None:
    cands = [p for p in workdir.iterdir()
             if p.suffix.lower() in {".srt", ".vtt", ".json3", ".json"}]
    if not cands:
        return None

    def score(p: Path):
        zh = 0 if re.search(r"zh|cn|chi", p.stem, re.I) else 1
        return (zh, -p.stat().st_size)

    cands.sort(key=score)
    for p in cands:
        text = parse_subtitle(p)
        if text and len(text.strip()) >= 20:
            return text.strip()
    return None


def parse_subtitle(path: Path) -> str:
    if path.suffix.lower() in {".json3", ".json"}:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            parts = []
            for ev in data.get("events", []):
                seg_text = "".join(s.get("utf8", "") for s in ev.get("segs", []))
                if seg_text.strip():
                    t = int(ev.get("tStartMs", 0)) // 1000
                    parts.append(f"[{_fmt_ts(t)}] {seg_text.strip()}")
            return "\n".join(parts)
        except Exception:
            return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".vtt" or text.lstrip().startswith("WEBVTT"):
        return _parse_vtt(text)
    return _parse_srt(text)


def _parse_srt(text: str) -> str:
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})[,.]\d{1,3}\s*-->", block)
        lines = [re.sub(r"<[^>]+>", "", l).strip() for l in block.splitlines() if l.strip()]
        content = " ".join(l for l in lines if not l.isdigit() and "-->" not in l)
        if not content:
            continue
        if m:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            out.append(f"[{_fmt_ts(t)}] {content}")
        else:
            out.append(content)
    return "\n".join(out)


def _parse_vtt(text: str) -> str:
    out = []
    t = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            m = re.search(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})[.]\d{3}\s*-->", line)
            if m:
                t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            out.append(f"[{_fmt_ts(t)}] {line}" if t is not None else line)
    return "\n".join(out)


# ---------------------------------------------------------------- 云端语音转写（硅基流动，默认 Qwen3-ASR）

def transcribe_audio(audio_path: Path, workdir: Path, cfg: dict, rep: Reporter) -> str:
    rep.start("transcribe", "切分音频")
    duration = _get_duration(audio_path)
    rep.log(f"音频时长约 {_fmt_ts(duration)}，按 {CHUNK_SECONDS // 60} 分钟一段切分…")
    seg_dir = workdir / "segments"
    seg_dir.mkdir(exist_ok=True)
    run_ffmpeg(["-i", str(audio_path), "-f", "segment",
                "-segment_time", str(CHUNK_SECONDS), "-c", "copy",
                str(seg_dir / "seg_%03d.wav")],
               total_seconds=duration,
               on_progress=lambda f: rep.progress("transcribe", 0.08 * f, "切分音频…"))
    segs = sorted(seg_dir.glob("seg_*.wav"))
    if not segs:
        raise RuntimeError("音频切分失败")
    _unlink(audio_path)     # 整轨已切好，删掉它省空间（16kHz wav 约 115 MB/小时）

    parts = []
    for i, seg in enumerate(segs):
        ts = i * CHUNK_SECONDS
        rep.progress("transcribe", 0.08 + 0.92 * i / len(segs),
                     f"转写第 {i + 1}/{len(segs)} 段（{_fmt_ts(ts)} 起）")
        rep.log(f"云端转写第 {i + 1}/{len(segs)} 段（从 {_fmt_ts(ts)} 开始）…")
        try:
            text = _transcribe_chunk(seg, cfg)
        finally:
            _unlink(seg)    # 每段传完即删，别攒着（单段约 19 MB）
        text = re.sub(r"<\s*\|[^|]*\|\s*>", "", text or "").strip()  # 去掉 SenseVoice 特殊标记
        if text:
            parts.append(f"[分段起点 {_fmt_ts(ts)}]\n{text}")
    shutil.rmtree(seg_dir, ignore_errors=True)
    transcript = "\n\n".join(parts)
    if len(transcript) < 10:
        raise RuntimeError("转写结果为空：请检查 API Key 余额，或该视频基本没有语音内容")
    rep.log(f"✅ 转写完成，共约 {len(transcript)} 字")
    rep.done("transcribe", f"共 {len(transcript)} 字")
    return transcript


def _transcribe_chunk(path: Path, cfg: dict) -> str:
    url = f"{cfg['base_url'].rstrip('/')}/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    last_err = ""
    for attempt in range(3):
        try:
            with open(path, "rb") as f:
                r = requests.post(url, headers=headers,
                                  files={"file": (path.name, f, "audio/wav")},
                                  data={"model": cfg["asr_model"]},
                                  timeout=(30, 600))
            if r.status_code == 200:
                return (r.json().get("text") or "")
            last_err = f"HTTP {r.status_code}：{r.text[:200]}"
            if r.status_code == 400 and "model" in r.text.lower():
                # 模型名失效，按顺序换备用转写模型
                for backup in ("Qwen/Qwen3-ASR-1.7B", "FunAudioLLM/SenseVoiceSmall"):
                    if backup != cfg["asr_model"]:
                        cfg["asr_model"] = backup
                        save_config(cfg)
                        break
                continue
            if 400 <= r.status_code < 500:
                break
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"转写接口调用失败：{last_err}")


# ---------------------------------------------------------------- 大模型总结（DeepSeek）

def summarize(transcript: str, title: str, cfg: dict, custom_prompt: str, rep: Reporter) -> str:
    rep.start("summarize", "准备文本")
    if len(transcript) > MAX_PART_CHARS:
        # 长文稿：先分段总结，再合并
        rep.log("转录稿较长，先分段总结再合并…")
        parts = [transcript[i:i + MAX_PART_CHARS] for i in range(0, len(transcript), MAX_PART_CHARS)]
        part_summaries = []
        for i, part in enumerate(parts):
            rep.progress("summarize", 0.85 * i / len(parts), f"分段总结 {i + 1}/{len(parts)}")
            rep.log(f"分段总结 {i + 1}/{len(parts)}…")
            p = (f"以下是视频《{title}》转录稿的第 {i + 1}/{len(parts)} 段（方括号内为时间点）。"
                 f"请列出本段的核心要点，每条附时间点，中文，简明。\n\n{part}")
            part_summaries.append(_chat(p, cfg))
        rep.progress("summarize", 0.85, "合并分段要点…")
        rep.log("合并生成最终总结…")
        combined = (f"视频《{title}》的转录稿分段要点如下：\n\n"
                    + "\n\n".join(f"【第 {i + 1} 段】\n{s}" for i, s in enumerate(part_summaries)))
        final_prompt = DEFAULT_PROMPT.format(title=title) + "\n\n以下是分段要点（不是原文）：\n\n" + combined
    else:
        final_prompt = DEFAULT_PROMPT.format(title=title) + "\n\n以下是完整转录稿：\n\n" + transcript

    if custom_prompt and custom_prompt.strip():
        final_prompt += f"\n\n【用户的额外要求】{custom_prompt.strip()}"

    rep.progress("summarize", 0.9, "大模型生成总结中…")
    rep.log("大模型生成总结中…")
    text = _chat(final_prompt, cfg)
    rep.done("summarize", f"总结共 {len(text)} 字")
    return text


def _chat(user_text: str, cfg: dict, allow_model_fix: bool = True) -> str:
    url = f"{cfg['base_url'].rstrip('/')}/v1/chat/completions"
    payload = {
        "model": cfg["llm_model"],
        "messages": [
            {"role": "system", "content": "你是专业的视频内容总结助手，输出使用 Markdown 格式。"},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                              json=payload, timeout=(30, 300))
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            last_err = f"HTTP {r.status_code}：{r.text[:200]}"
            if r.status_code == 400 and "model" in r.text.lower() and allow_model_fix:
                # 模型名可能已失效，自动换一个可用的
                new_model = pick_llm_model(cfg)
                if new_model and new_model != cfg["llm_model"]:
                    cfg["llm_model"] = new_model
                    save_config(cfg)
                    return _chat(user_text, cfg, allow_model_fix=False)
            if 400 <= r.status_code < 500:
                break
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"总结接口调用失败：{last_err}")


def pick_llm_model(cfg: dict) -> str | None:
    """从账号可用模型中自动挑选最新的 DeepSeek 对话模型"""
    try:
        r = requests.get(f"{cfg['base_url'].rstrip('/')}/v1/models",
                         headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=30)
        if r.status_code == 200:
            ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict)]
            cands = [i for i in ids
                     if "deepseek" in i.lower()
                     and not any(x in i.lower() for x in ("terminus", "coder", "exp"))]
            if cands:
                # 同版本下优先 Flash（更快更便宜），其次官方短名模型
                return sorted(cands, key=lambda i: (_model_ver(i)[:2],
                                                    0 if "flash" in i.lower() else 1,
                                                    _model_ver(i)[2]), reverse=True)[0]
    except Exception:
        pass
    return None


def _model_ver(model_id: str):
    m = re.search(r"v(\d+)(?:[.](\d+))?", model_id.lower())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2) or 0), -len(model_id))


# ---------------------------------------------------------------- 主流程

def run_pipeline(url: str, file_path, custom_prompt: str, api_key: str,
                 reporter: Reporter | None = None,
                 log=None, progress=None, local_name=None) -> tuple:
    """完整流程，返回 (总结 markdown, 转录稿文件路径, 总结文件路径)

    reporter：Reporter 实例，界面用它实时显示各步骤进度；
    log / progress：旧版回调，仅在没传 reporter 时生效（保持向后兼容）。
    """
    rep = reporter or Reporter(on_log=log, on_progress=progress)

    cfg = load_config()
    if api_key and api_key.strip():
        cfg["api_key"] = api_key.strip()
        save_config(cfg)
    if not cfg["api_key"]:
        raise RuntimeError("请先在「设置」中填写硅基流动 API Key（注册地址 cloud.siliconflow.cn）")

    purge_work(WORK_ROOT)        # 清掉上次崩溃/中断残留的临时文件
    workdir = WORK_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    workdir.mkdir(parents=True, exist_ok=True)
    _ACTIVE_WORKDIRS.add(workdir.resolve())

    try:
        if url and url.strip():
            media = fetch_web(url.strip(), workdir, rep)
        elif file_path:
            media = process_local(file_path, workdir, rep, title=local_name)
        else:
            raise RuntimeError("请粘贴视频网址或上传本地文件")

        title = media["title"]
        if media["kind"] == "subtitle":
            transcript = media["text"]
            rep.skip("transcribe", "已有字幕，无需转写")   # 兜底，正常已由上一步标记
        else:
            transcript = transcribe_audio(media["audio_path"], workdir, cfg, rep)

        summary = summarize(transcript, title, cfg, custom_prompt, rep)

        rep.start("export", "写入结果文件")
        stem = _sanitize(title)
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        trans_file = _unique_path(OUTPUT_ROOT / f"转录稿_{stem}.txt")
        trans_file.write_text(transcript, encoding="utf-8")
        sum_file = _unique_path(OUTPUT_ROOT / f"总结_{stem}.md")
        sum_file.write_text(summary, encoding="utf-8")
        rep.done("export", f"已存入「输出/{sum_file.name}」")
    except Exception as e:
        rep.fail_running(str(e))     # 界面把出错的那一步标红
        raise
    finally:
        _ACTIVE_WORKDIRS.discard(workdir.resolve())
        # 成品已进「输出/」，临时目录整个删掉，不留垃圾（失败时也删）
        shutil.rmtree(workdir, ignore_errors=True)

    rep.finish()
    return summary, str(trans_file), str(sum_file)


# ---------------------------------------------------------------- 工具函数

def purge_work(root: Path = WORK_ROOT, min_age: int = 600) -> None:
    """清空临时目录。

    work/ 只放中间产物（成品都在「输出/」里），所以除了正在跑的，其余都能直接删。
    min_age：跳过刚建不久的目录，多开进程时不会误删另一个实例正在用的文件。
    """
    if not root.exists():
        return
    now = time.time()
    for d in root.iterdir():
        try:
            if not d.is_dir() or d.resolve() in _ACTIVE_WORKDIRS:
                continue
            if now - d.stat().st_mtime < min_age:
                continue
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _unlink(path) -> None:
    """删文件，删不掉也不报错——清临时文件不该把整个流程拖垮"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _unique_path(path: Path) -> Path:
    """同名文件已存在就加编号，避免标题相同的两个视频互相覆盖"""
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not cand.exists():
            return cand
    return path


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", name).strip()[:60] or "video"


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _fmt_ts(sec: int) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
