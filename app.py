# -*- coding: utf-8 -*-
"""AI 视频总结 —— Web 界面（Gradio）

界面部分：把 summarizer 各步骤的进度实时画出来。
流水线跑在后台线程里，通过队列把「进度快照」流式 yield 给浏览器，
所以长任务（下载、转写）期间页面会持续刷新，不会卡住。
"""

import html
import queue
import sys
import threading
import time

for _stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK，统一转 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import gradio as gr

import summarizer as core


# ---------------------------------------------------------------- 进度面板

# 各步骤状态的图标 / 文字（running 用转圈动画，见 CSS 里的 .vs-spin）
STATUS_ICON = {"pending": "○", "done": "✔", "skip": "⏭", "fail": "✖"}
STATUS_TEXT = {"pending": "等待", "done": "完成", "skip": "跳过", "fail": "失败"}

# Gradio 6 的 css 要传给 launch()，不能传给 Blocks()
CSS = """
.vs-panel { display: flex; flex-direction: column; gap: 6px; font-size: .9rem; }
.vs-top { display: flex; justify-content: space-between; align-items: baseline;
          font-weight: 600; margin-bottom: 4px; }
.vs-elapsed { font-weight: 400; font-size: .8rem; opacity: .6; margin-left: 8px; }
.vs-bar { height: 6px; border-radius: 99px; overflow: hidden;
          background: var(--neutral-200, rgba(127, 127, 127, .18)); }
.vs-bar > i { display: block; height: 100%; border-radius: 99px;
              background: var(--color-accent, #f97316); transition: width .3s ease; }
.vs-bar-lg { height: 10px; }
.vs-step { display: flex; gap: 9px; padding: 7px 10px; border-radius: 8px;
           background: var(--background-fill-secondary, rgba(127, 127, 127, .07));
           border: 1px solid transparent; }
.vs-step.vs-pending { opacity: .5; }
.vs-step.vs-running { border-color: var(--color-accent, #f97316); }
.vs-step.vs-fail { border-color: var(--error-500, #ef4444); }
.vs-dot { width: 16px; flex: 0 0 16px; text-align: center; line-height: 1.5; }
.vs-done .vs-dot { color: #16a34a; }
.vs-fail .vs-dot { color: var(--error-500, #ef4444); }
.vs-spin { display: inline-block; width: 11px; height: 11px; border-radius: 50%;
           border: 2px solid var(--color-accent, #f97316); border-top-color: transparent;
           animation: vs-rot .8s linear infinite; vertical-align: -1px; }
@keyframes vs-rot { to { transform: rotate(360deg); } }
.vs-main { flex: 1; min-width: 0; }
.vs-head { display: flex; justify-content: space-between; gap: 8px; }
.vs-name { font-weight: 600; }
.vs-pct { font-variant-numeric: tabular-nums; opacity: .75; font-size: .82rem; }
.vs-detail { font-size: .8rem; opacity: .7; margin: 1px 0 4px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vs-banner { margin-top: 6px; padding: 7px 10px; border-radius: 8px; font-size: .88rem; }
.vs-banner.ok { background: rgba(22, 163, 74, .14); color: #16a34a; }
.vs-banner.err { background: rgba(239, 68, 68, .14); color: #ef4444; }
.vs-logs { max-height: 190px; overflow-y: auto; white-space: pre-wrap;
           font-family: ui-monospace, Consolas, monospace;
           font-size: .78rem; line-height: 1.7; opacity: .85; }
"""


def _empty_snapshot() -> dict:
    return {"steps": [{"key": k, "label": lab, "status": "pending", "detail": "", "frac": 0.0}
                      for k, lab, _ in core.STEP_DEFS],
            "overall": 0.0, "logs": [], "finished": False}


def _fmt_elapsed(sec: float) -> str:
    s = int(sec)
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


def render_panel(snap=None, error: str = "", elapsed: float = 0) -> str:
    """把进度快照渲染成 HTML：总体进度条 + 每个步骤的状态和进度条"""
    snap = snap or _empty_snapshot()
    rows = []
    for s in snap["steps"]:
        st = s["status"]
        frac = 1.0 if st in ("done", "skip") else (float(s["frac"]) if st == "running" else 0.0)
        dot = ('<span class="vs-spin"></span>' if st == "running"
               else f'<span class="vs-glyph">{STATUS_ICON.get(st, "○")}</span>')
        right = f"{frac * 100:.0f}%" if st == "running" else STATUS_TEXT.get(st, "")
        detail = (f'<div class="vs-detail" title="{html.escape(s["detail"], quote=True)}">'
                  f'{html.escape(s["detail"])}</div>') if s["detail"] else ""
        rows.append(
            f'<div class="vs-step vs-{st}">'
            f'<div class="vs-dot">{dot}</div>'
            f'<div class="vs-main">'
            f'<div class="vs-head"><span class="vs-name">{html.escape(s["label"])}</span>'
            f'<span class="vs-pct">{right}</span></div>'
            f'{detail}'
            f'<div class="vs-bar"><i style="width:{frac * 100:.1f}%"></i></div>'
            f'</div></div>')

    overall = float(snap["overall"])
    running = next((s for s in snap["steps"] if s["status"] == "running"), None)
    if error:
        cur = "已中断"
    elif snap.get("finished"):
        cur = "全部完成"
    elif running:
        cur = f"当前：{running['label']}" + (f" · {running['detail']}" if running["detail"] else "")
    else:
        cur = "等待开始…"

    clock = f'<span class="vs-elapsed">已用 {_fmt_elapsed(elapsed)}</span>' if elapsed >= 1 else ""
    banner = ""
    if error:
        banner = f'<div class="vs-banner err">❌ {html.escape(error)}</div>'
    elif snap.get("finished"):
        banner = '<div class="vs-banner ok">✅ 全部步骤已完成</div>'

    return ('<div class="vs-panel">'
            f'<div class="vs-top"><span>总体进度{clock}</span>'
            f'<span class="vs-pct">{overall * 100:.0f}%</span></div>'
            f'<div class="vs-bar vs-bar-lg"><i style="width:{overall * 100:.1f}%"></i></div>'
            f'<div class="vs-detail">{html.escape(cur)}</div>'
            + "".join(rows) + banner + "</div>")


def render_logs(snap=None) -> str:
    logs = (snap or {}).get("logs") or []
    if not logs:
        return '<div class="vs-logs">（暂无日志）</div>'
    return '<div class="vs-logs">' + "\n".join(html.escape(m) for m in logs) + "</div>"


# ---------------------------------------------------------------- 界面

def build_demo():
    cfg = core.load_config()

    def save_key(api_key, model_override):
        key = (api_key or "").strip()
        if not key:
            return "❌ Key 不能为空，请粘贴后再保存。"
        cfg = core.load_config()
        cfg["api_key"] = key
        if model_override and model_override.strip():
            cfg["llm_model"] = model_override.strip()
            core.save_config(cfg)
            return f"✅ 已保存。总结模型（手动指定）：`{cfg['llm_model']}`"
        core.save_config(cfg)
        try:
            model = core.pick_llm_model(cfg)
            if model:
                cfg["llm_model"] = model
                core.save_config(cfg)
                return f"✅ 已保存。自动选择总结模型：`{model}`"
        except Exception as e:
            return f"✅ Key 已保存，但自动选择模型失败：{e}（可在模型框手动填写）"
        return "✅ 已保存。"

    def process(url, file, custom_prompt, api_key):
        """跑完整流程，同时把进度流式吐给界面（每帧 = 一个完整输出元组）"""
        file_path = file if file is not None else None
        local_name = None
        if file_path:
            if isinstance(file_path, list):
                file_path = file_path[0]
            local_name = getattr(file_path, "orig_name", None)

        events = queue.Queue()          # 后台线程 → 界面的进度快照
        state = {"summary": "", "transcript": None, "summary_file": None,
                 "error": "", "done": False}

        def worker():
            try:
                rep = core.Reporter(on_change=events.put)
                summary, transcript, summary_file = core.run_pipeline(
                    url, file_path, custom_prompt, api_key,
                    reporter=rep, local_name=local_name)
                state["summary"] = summary
                state["transcript"] = transcript
                state["summary_file"] = summary_file
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["done"] = True

        started = time.time()
        yield render_panel(), render_logs(), "", None, None
        threading.Thread(target=worker, daemon=True).start()

        snap = None
        last_beat = started
        while True:
            try:
                snap = events.get(timeout=0.2)
            except queue.Empty:
                if state["done"]:
                    break
                if time.time() - last_beat > 2:      # 心跳：让「已用时间」持续走动
                    last_beat = time.time()
                    yield (render_panel(snap, elapsed=time.time() - started),
                           render_logs(snap), "", None, None)
                continue
            while True:                              # 抽干积压，只画最新一帧
                try:
                    snap = events.get_nowait()
                except queue.Empty:
                    break
            yield (render_panel(snap, elapsed=time.time() - started),
                   render_logs(snap), "", None, None)

        yield (render_panel(snap, state["error"], time.time() - started),
               render_logs(snap), state["summary"],
               state["transcript"], state["summary_file"])

    with gr.Blocks(title="AI 视频总结") as demo:
        gr.Markdown(
            "# 🎬 AI 视频总结\n"
            "粘贴视频网页地址（B站、新闻网站等）或上传本地视频/音频，自动生成结构化总结。\n\n"
            "**流程**：获取视频 → 提取音轨 → 语音转写（Qwen3-ASR）→ DeepSeek 生成总结\n\n"
            "**结果保存**：转录稿和总结存入 `输出/` 目录长期保留；中间音频用完即删，不占空间。"
        )

        with gr.Accordion("⚙️ 设置（首次使用必填）", open=not cfg.get("api_key")):
            with gr.Row():
                key_box = gr.Textbox(
                    label="硅基流动 API Key",
                    value=cfg.get("api_key", ""),
                    type="password",
                    placeholder="sk-xxxxxxxx（在 cloud.siliconflow.cn 注册获取）",
                    scale=3,
                )
                save_btn = gr.Button("💾 保存", scale=1)
            with gr.Row():
                model_box = gr.Textbox(
                    label="总结模型（可选，留空自动选择）",
                    value="",
                    placeholder="如 deepseek-ai/DeepSeek-V3.1",
                    scale=3,
                )
                key_status = gr.Markdown(scale=2)

        with gr.Row():
            url_box = gr.Textbox(
                label="视频网页地址",
                placeholder="https://www.bilibili.com/video/BVxxxxxxxxxx（一次处理一个）",
                scale=2,
            )
            file_box = gr.File(label="本地视频 / 音频文件", file_types=["video", "audio"], scale=1)

        prompt_box = gr.Textbox(
            label="额外总结要求（可选）",
            lines=2,
            placeholder="例如：用英文总结 / 300 字以内 / 重点讲最后 30 分钟的内容",
        )
        run_btn = gr.Button("🚀 开始总结", variant="primary")

        progress_html = gr.HTML(render_panel())          # 各步骤实时进度
        with gr.Accordion("📋 运行日志", open=False):
            logs_html = gr.HTML(render_logs(), autoscroll=True, padding=False)
        summary_md = gr.Markdown()
        with gr.Row():
            transcript_file = gr.File(label="📄 完整转录稿（.txt）", scale=1)
            summary_file = gr.File(label="📝 总结（.md）", scale=1)

        save_btn.click(save_key, inputs=[key_box, model_box], outputs=[key_status])
        run_btn.click(process, inputs=[url_box, file_box, prompt_box, key_box],
                      outputs=[progress_html, logs_html, summary_md,
                               transcript_file, summary_file])

    return demo


if __name__ == "__main__":
    core.purge_work(min_age=0)   # 刚启动，本进程没有任务在跑，残留临时文件直接清空
    build_demo().launch(inbrowser=True, server_name="127.0.0.1", server_port=7860,
                        theme=gr.themes.Soft(), css=CSS)
