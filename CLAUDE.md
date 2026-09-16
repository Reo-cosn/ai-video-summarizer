# CLAUDE.md

AI 视频总结工具（个人项目，Windows + Git Bash 环境）。

- **技术栈**：Python + Gradio + yt-dlp + 硅基流动 API（Qwen3-ASR 云端转写 + DeepSeek 总结）
- **核心逻辑**：`summarizer.py`（获取视频 → 提取音轨 → 转写 → 总结）；UI：`app.py`（Gradio）
- **启动**：`start.bat`（Windows）/ `start.sh`（macOS、Linux）——两者行为一致：自动建 .venv、装依赖、开浏览器；手动：`.venv\Scripts\python app.py`（Windows）或 `.venv/bin/python app.py`（Unix）
- **配置**：`config.json`（API key 等，勿外传）；`cookies.txt`（可选，B站登录态）
  - ⚠️ `run_pipeline(..., api_key=...)` 会把 key **写回 config.json**，写测试时别传假 key，否则会覆盖用户的真 key
- **目录约定**：成品（转录稿 `.txt` + 总结 `.md`）存 `输出/`，永久保留；`work/` 是纯临时目录，
  中间音频**用完即删**（下载的 mp3、整轨 wav、切分出的每段 wav），跑完整个 workdir 直接删掉。
  `purge_work()` 在启动和每次任务开始时兜底清理崩溃残留，跳过 `_ACTIVE_WORKDIRS` 里正在跑的目录
- **启动耗时**：约 5.5s，其中 `import gradio` 占 ~4.8s（1537 个模块，属硬成本）；
  `build_demo()` 0.36s、`launch()` 0.29s。gradio 版本检查在后台线程，不阻塞启动
- **用户选型**：云端转写（无 GPU，不用本地 Whisper）；Web 界面；主要来源 B站 + 本地文件
- **约定**：代码注释和 UI 文案用中文；ffmpeg 来自 imageio-ffmpeg（系统未装 ffmpeg）
- **实时进度**：`summarizer.Reporter` 汇总「日志 + 各步骤状态/进度」（`STEP_DEFS` 定义步骤与权重），
  每次变化回调一个快照 dict；`app.py` 把流水线放后台线程，用 `queue` 收快照再流式 yield 给界面。
  真实进度来源：ffmpeg `-progress`（转码）、yt-dlp `progress_hooks`（下载）、ASR 分段计数。
  `run_pipeline` 仍兼容旧的 `log=` / `progress=` 回调（不传 `reporter` 时生效）。
- **Gradio 6 注意**：`theme`、`css` 参数要放在 `.launch()` 而非 `gr.Blocks()`；
  多输出的流式函数 yield 元组（`(进度面板, 日志, summary, 转录稿, 总结文件)`），不支持 yield 字典（会报 ValueError: didn't return enough output values）
