# YouTube 股票观点追踪（Codex 工作流）

发现新视频 → 获取完整字幕或本地转写 → 证据质量检测 → Codex逐股分析 → Markdown/HTML日报 → 投递确认。

这是本地工具，不是开箱即用的云网站，也不是自动荐股程序。Python负责采集与状态管理；自然语言分析、日报编写与每日唤醒需要你自己的Codex会话和授权配置。没有调用OpenAI API，不需要在代码里填API Key。默认追踪视野环球财经、老李玩钱、精英财经LABanker、XTrader猫姐美股；可修改config.json。

## 快速部署

推荐Apple Silicon Mac + Python3.12 + Node.js22+。把仓库克隆到自己的电脑，在Codex里打开目录，使用 [CODEX_SETUP.md](CODEX_SETUP.md) 的可复制提示词；其中也有逐步安装命令。

```bash
git clone https://github.com/pkullx-max/youtube-stock-tracker.git
cd youtube-stock-tracker
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tracker.py run --backfill-days 1 --dry-run
```

其他macOS/Linux可使用发现及字幕路径，但MLX音频后备仅支持Apple Silicon；Windows原生文件锁不兼容。未提供Docker/云端一键部署。完整分析依赖Codex读取证据，不能只运行tracker.py就期待自动生成日报。

## YouTube访问与转写

顺序：官方字幕、自动字幕、yt-dlp字幕、本地MLX Whisper。标准视频、Shorts、已结束直播回放可处理；预告和进行中的直播延期。RSS不可用时回退频道视频页发现。

默认 `cookies_from_browser: null`，不会读取登录态。确需登录验证时本人授权后改为 `"chrome"`。不得共享Cookie。Node需在PATH中；yt-dlp显式使用Node解决EJS。音频由imageio-ffmpeg提供ffmpeg。

部分视频需要额外的 [bgutil PO-token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)。requirements里的Python插件不包含Node服务端。可按其官方说明安装与插件匹配的服务端版本；本项目验证组合为插件1.3.2，默认脚本位置为用户主目录下bgutil-ytdlp-pot-provider/server，或启动本机127.0.0.1:4416 HTTP服务。不要把此未认证服务暴露公网。YouTube访问策略会变化，PO Token不保证消除403；遇bot verification要保留失败记录并人工核查，不能绕过证据门槛。升级下载器后须重新验证实际视频。

## 命令与状态

```bash
.venv/bin/python tracker.py run --backfill-days 1 --limit 1
.venv/bin/python tracker.py pending
.venv/bin/python tracker.py status
.venv/bin/python tracker.py retry --video-id VIDEO_ID
.venv/bin/python tracker.py mark-report-ready --video-id VIDEO_ID --report /absolute/path/report.md
.venv/bin/python tracker.py mark-delivery-pending --report /absolute/path/report.md
.venv/bin/python tracker.py mark-reported --video-id VIDEO_ID --report /absolute/path/report.md
```

状态：discovered/retry → transcribing → ready → report_ready → reported。三次失败或重复中断进入needs_manual_review。ready代表转写可供分析，不代表观点或投递完成。非阻塞文件锁避免重复运行。

`data/state.json`是运行权威；字幕在data/transcripts、音频在data/audio。正常run自动清理reported超过7天的视频字幕/音频，以及过期未追踪研究转写；永久保留状态和日报。未闭环ready/report_ready/retry等不会被删除。只使用自己有权访问的公开视频，遵守平台规则与内容权利，不把转写全文提交公开仓库。

## 报告标准

- 使用完整transcript JSON：evidence_ready=true、timeline_coverage>=0.85、max_gap_seconds<=300；Shorts更严格为0.98/15秒并标注“短视频观点，论证可能不完整”。
- 首屏简短概括每位博主最新视频日期、市场判断和重要action，区分成交与计划；下面完整保留最近三期逐股票分析。
- 表格写视频发布时间及其时区，不用报告日期代替；每只股票单独记录公司、可靠代码、立场、操作、完整逻辑、公司评价、条件、期限、风险、准确时间戳、原链接、置信度和历史变化。
- 只还原买入、加仓、持有、观望/等待价位、减仓、卖出、看空/回避；普通提及注明无操作。价格写“请以视频画面为准”。不做外部公司研究，不以标题、缩略图或模型猜测做推荐证据。
- 无明确证据时原样写“本期有新视频，但没有完整证据支持的明确股票推荐或操作变化。”
- 每日生成reports/YYYY-MM-DD.md/html，无新视频也保留三期快照并写“今日无新视频”，但只本地更新，不发无变化通知。

渲染与稳定入口（将日期换为真实当天）：

```bash
.venv/bin/python render_daily_html.py reports/YYYY-MM-DD.md reports/YYYY-MM-DD.html
cp reports/YYYY-MM-DD.md reports/latest.md
cp reports/YYYY-MM-DD.html reports/latest.html
```

有视频内容增量才把完整报告、视频信息、累计变化和两条HTML链接合并交付。必须使用本用户机器的绝对路径Markdown链接；本地文件链接只在持有文件的电脑上有效，不是公开网站链接。只有确认报告和链接已在本人选定任务显示后才能mark-reported；失败或未确认则mark-delivery-pending，不创建替代任务。

## 发布内容与隐私

仓库仅含工具、公开频道配置、部署说明和测试，不包含作者的日报、转写、音频、运行状态、登录态、个人目录、固定任务ID或Codex自动化配置。部署者需自行配置自己的运行环境、浏览器授权和自动任务。生成的观点是对视频的还原，不构成投资建议；视频原话可能错误，应回看原视频核实。
