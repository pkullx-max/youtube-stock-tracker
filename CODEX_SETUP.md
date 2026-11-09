# 把此文件交给你自己的 Codex

可复制这段话到克隆后的项目对话：

> 请阅读 README.md、AGENTS.md 和 CODEX_SETUP.md，在当前电脑部署这个 YouTube 股票观点追踪工具。先检查系统、Python 和 Node；创建独立 .venv，安装依赖，运行测试、status 和一次 dry-run。保留默认四个频道，除非我另行指定。仅使用我自己授权的浏览器登录态，不索要仓库作者的凭据。检查真实视频的元数据和完整转写证据；如缺少 Apple Silicon 或遇到 YouTube 验证，明确报告限制，不能假称成功。首次先处理最新一个视频，读完完整 transcript，再生成有逐股证据的 Markdown/HTML 和两个可点击绝对路径链接。不要提前标记 reported。部署成功后询问我是否每天北京时间20:30自动运行；获授权才建立绑定到我自己当前任务的唯一自动化。无内容增量不通知；失败不能冒充无更新。

## 人工安装顺序

1. macOS Apple Silicon、Python 3.12、Node.js 22+、Git；网络能访问 YouTube 和模型下载站点。其他 macOS/Linux 可测试发现和字幕路径，但没有 MLX 音频后备；Windows 原生不支持本项目的 fcntl 文件锁，未承诺可直接部署。
2. 在仓库根目录安装：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tracker.py status
.venv/bin/python tracker.py run --backfill-days 1 --dry-run
```

3. `config.json` 默认不读取浏览器 Cookie；如本人授权使用已登录 Chrome，将 `cookies_from_browser` 从 null 改成 `"chrome"`。只在本机读取，不导出 Cookie。修改后不要把自己的额外私人频道配置上传。
4. 无字幕时会下载音频运行 MLX Whisper，首次下载模型可能较大。YouTube 的 403/PO Token 验证还需要 provider，见 README；不是装好 Python 依赖就保证所有视频可用。
5. 首次小批量测试：

```bash
.venv/bin/python tracker.py run --backfill-days 1 --limit 1
.venv/bin/python tracker.py pending
```

`pending` 中的 `ready` 是等待 Codex 分析，不是已经有日报。无视频时可经用户同意扩大 backfill-days；不要一次下载全部历史。

## 每日任务提示词（用户授权后使用）

在本仓库执行 run --backfill-days 1，然后 pending。读取本用户自己的投递记录和完整 transcript JSON；严格遵循 AGENTS.md 证据规则。生成北京时间当天 reports/YYYY-MM-DD.md/html，最近三期逐视频逐股票完整展开，最新在前。标题后先给各博主真实视频日期、市场判断和重要动作的简短 bullet，再给含视频发布日期的动作表和完整分析。股票字段：公司/ticker、立场、操作、理由、公司评价、条件/价位、期限、风险、时间戳、原视频链接、置信度、历史变化。价格请以视频画面为准。没有明确观点原样写：本期有新视频，但没有完整证据支持的明确股票推荐或操作变化。同步 latest.md/html。文件生成后 mark-report-ready；确认当前用户固定任务中完整报告和两个可点击绝对路径 HTML 链接已显示后才 mark-reported，否则 mark-delivery-pending。无内容增量只更新本地文件、不发消息、不新增纯快照待投递；失败单独报告，不假称无新视频。不得创建替代任务，不做外部股票研究，不泄露凭据。

以上是给 Codex 的流程提示，不是系统定时器配置；克隆仓库不会自动复制作者的自动任务，也不需要作者的任务 ID。
