# TokenFish Agent v0 — 规格（实现者必读，唯一依据）

> 目标：用户在 豆包工作 / WorkBuddy / Claude Code / Codex / Cursor 里**发一条消息**就能写歌、作图，按虾盘云钱包扣费。
> 门面品牌 **TokenFish**（`tokenfish.cn`），后端是现有的**虾盘云** API（不新建账本、不新建账号体系）。
> 机制参考 Beatra（beatra.ai）：一个能力面 + 多个薄场景 Skill + 随包客户端 + 写给 AI 读的 install.md。**不复制 Beatra 的代码、文案、视觉。**
> 本仓只做客户端、Skill 包、构建脚本和静态站点。**不部署、不改 DNS、不发布到任何市场、不改服务端。**

## 0. 仓库结构（要产出的）

```
client/tokenfish_client.py     # 唯一的远程调用实现，Python 3.10+ 纯标准库
skills/tokenfish/              # 全能包：SKILL.md + references/*.md + manifest.json + agents/openai.yaml
skills/lyrics-to-song/         # 场景包「歌词成曲」：SKILL.md + references/*.md + manifest.json
build/build.py                 # 把 client 复制进每个包的 scripts/，按 渠道×语言 出 zip + sha256 + install.json + install.md
build/templates/install.md.tmpl
site/                          # 纯静态站（无框架、无构建步骤），见 §4
tests/                         # unittest，客户端对一个本地假 HTTP 服务测试，不碰真实 API
README.md
```

## 1. 后端事实（虾盘云，已从服务端源码核实；不要自己猜别的端点）

- Base：默认 `https://api.u-claw.org.cn`；可用非机密环境变量 `TOKENFISH_API_BASE` 覆盖。鉴权 `Authorization: Bearer <key>`。
- 额度单位：quota，**¥1 = 500000 quota**。
- **余额**：`GET /api/usage/token/` → `{"code":true,"data":{"name","total_available","total_used","total_granted",...}}`。`total_available` 可为负（欠费）。
- **MiniMax 音乐**（经 pay-server，同步，上游 1–2 分钟，请求超时给 240s）：
  `POST /v1/music/generations` body `{"model":"minimax-music-v2.6","prompt":<风格描述>,"lyrics":<可选>,"is_instrumental":<可选 bool>,"duration":<可选 1..300>,"idempotency_key":<12–128 位 [A-Za-z0-9-_.]>}`
  → `{"id","object":"music","model","status","quota_charged"?,"output_url"?}`。同 `idempotency_key` 重放不重复扣费。
  查询 `GET /v1/music/generations/{id}`；下载 `GET /v1/music/generations/{id}/content`（需 Bearer）。
  余额不足 → HTTP 402 `{"error":{"type":"insufficient_quota","message":...}}`。**不要传非法 duration**（服务端当前先扣费后校验，客户端必须先校验）。
- **Suno 音乐**（经 new-api，异步，¥1.2/次一次出 2 首）：
  `POST /suno/submit/music` body `{"prompt":<歌词>,"tags":<风格>,"title":<标题>,"mv":"chirp-v5-5","make_instrumental":<bool，可选>}` → `{"code":"success","data":"<task_id>"}`
  轮询 `GET /suno/fetch/{task_id}` → `{"data":{"status":"SUCCESS|FAILURE|IN_PROGRESS|...","data":[{"audio_url","title","duration",...}]}}`。
  **此路径没有服务端幂等**，客户端必须自己做（§2.4）。
- **作图**：`POST /v1/images/generations`（OpenAI 兼容）body `{"model":"gpt-image-2","prompt","n":1..4,"size","quality":"low|medium|high|auto"}` → `data[].url` 或 `data[].b64_json`。
- **已知坑**：欠费时 pay-server 路径返回 402，而 new-api 路径（`/suno/*`、`/v1/models`）返回 **401 "Invalid token"**。客户端必须统一成同一种错误（§2.5）。

## 2. 客户端 `client/tokenfish_client.py`

### 2.1 命令（AI 调用的唯一入口）
```
python tokenfish_client.py verify                  # 读余额验证凭据，不扣费
python tokenfish_client.py tools                   # 打印工具清单（名字、说明、是否计费、参数 JSON Schema）
python tokenfish_client.py call <tool>             # 参数 JSON 从 stdin 读；结果 JSON 打到 stdout
python tokenfish_client.py login --key-file <path> # 从文件读 key，写入凭据文件；绝不从 argv/env 读 key
python tokenfish_client.py whoami                  # 只显示打码的 key（sk-****末4位）和余额
python tokenfish_client.py topup                   # 用浏览器打开充值页 https://api.u-claw.org/recharge ，并把 key 复制到剪贴板（Windows 用 clip，mac 用 pbcopy，Linux 尝试 xclip/wl-copy，失败则提示用 whoami）；**不在 stdout 打印 key，不把 key 放进 URL**
```
退出码：0 成功；2 参数错误；3 需要用户动作（未登录/余额不足）；4 暂时性失败（可原样重试）；1 其他。stdout 只放 JSON，诊断走 stderr。

### 2.2 凭据
- 真相源：`~/.tokenfish/credentials.json` `{"key": "..."}`，写入时原子写、仅当前用户可读（POSIX 0600；Windows 尽力用 `icacls` 收紧，失败要 stderr 警告）。
- 若该文件不存在，**只读**地尝试 `~/.uking/device.json` 的 `key` 字段（U-King 用户共用同一个钱包）。**永不写这个文件。**
- key 永不出现在 stdout、日志、异常信息、命令参数、环境变量里。

### 2.3 工具（v0）

| 工具 | 计费 | 行为 |
|---|---|---|
| `tokenfish.wallet.get` | 否 | 余额 → `{"balance_cny": total_available/500000 保留2位, "total_available_quota", "topup": "运行 `python scripts/tokenfish_client.py topup` 在浏览器充值"}` |
| `tokenfish.models.list` | 否 | 入参 `{"capability":"text_to_music"|"text_to_image"}`；缺参或非法时报错**并列出全部合法取值和一句说明**。v0 返回静态候选：music → `auto`（顺序 suno → minimax），image → `gpt-image-2`；价格字段写"以实时扣费为准"，不写死数字 |
| `tokenfish.music.generate` | **是** | 入参 `{"prompt":风格(必填,≤1000),"lyrics":可选≤5000,"title":可选≤80,"instrumental":bool,"model":"auto|suno|minimax","client_request_id":必填}`。instrumental=true 时不得带 lyrics。auto：先 Suno，提交失败且非余额问题时回退 MiniMax（换新的内部请求，但同一 client_request_id 映射到最终任务）。返回统一任务对象 |
| `tokenfish.images.generate` | **是** | 入参 `{"prompt","n","size","quality","client_request_id"}`；b64 结果落盘到 `./tokenfish-outputs/`，返回文件路径和 url |
| `tokenfish.tasks.get` | 否 | 入参 `{"task_id"}`；按任务来源查询 Suno 或 MiniMax，归一化为统一任务对象 |
| `tokenfish.tasks.list` | 否 | 列本地任务记录（最新在前，最多 20） |

统一任务对象：`{"task_id","provider":"suno|minimax|image","status":"queued|running|succeeded|failed","clips":[{"audio_url","title","duration_seconds"}],"images":[...],"charged_cny":<只有服务端给了才填，否则 null>,"client_request_id"}`。**不根据片段数或时长推算费用。**

### 2.4 幂等（客户端侧）
- 本地任务表 `~/.tokenfish/tasks.json`：`client_request_id → {args_hash, task_id, provider, created_at}`。
- 同 id + 同参数：直接返回已有任务，不重新提交；同 id + 不同参数：返回 `idempotency_conflict`，不提交。
- MiniMax 额外把 client_request_id 作为 `idempotency_key` 传给服务端（不合规时用 sha256 截断生成合规值）。

### 2.5 错误归一（写给 AI 看的错误）
stdout 输出 `{"error":{"code":..., "message":中文, "retryable":bool, "next_action":...}}`：
- 402 / `insufficient_quota` → `insufficient_balance`，message：「余额不足，本次未扣费。请运行 topup 充值，充值后可用同一 client_request_id 原样重试。」退出码 3
- 401 → 先查余额：`total_available <= 0` 视为 `insufficient_balance`；否则 `auth_invalid`「凭据无效，请重新 login」。退出码 3
- 429 → `rate_limited`，retryable；5xx/超时/网络 → `transient`，retryable，提示「用同一 client_request_id 原样重试」。退出码 4
- 参数校验失败 → `invalid_arguments`，列出哪个字段、合法范围。退出码 2

### 2.6 归因
所有请求带 `User-Agent: tokenfish-client/<版本> (<package_slug>; <platform>)`。platform 从环境推断（`CLAUDECODE` 等环境变量 → claude-code，`CODEX_*` → codex，Windows 路径含 `DoubaoWork` → doubao-work，含 `WorkBuddy`/`.workbuddy` → workbuddy，否则 unknown），package_slug 从所在包的 manifest.json 读。

## 3. Skill 包

通用规则：
- 中文为主；SKILL.md frontmatter 只有 `name`、`description`；description 第一句写**用户想要什么**（用用户会搜的词），不写"我们有什么"。
- **不在 slug / 标题 / description 里使用第三方品牌词**（Suno、MiniMax 等），正文可如实说"音乐模型由上游提供，默认先用效果最好的那个"。
- 所有远程操作只通过 `scripts/tokenfish_client.py`；不要让 AI 配置宿主 MCP、不要写 curl。
- **一次确认，一次执行**：免费的策划/歌词整理先做；付费前给一张"制作卡"（完整歌词、标题、风格、模型、"下一步会扣费"），用户同意后只提交一次；卡定稿后才生成 client_request_id。
- 余额不足：原样转述客户端返回的 message；不自己编充值说明。
- 交付：按返回顺序给出每个 clip 的试听链接、时长；报告服务端给的扣费（没有就说"以钱包流水为准"）。
- `manifest.json`：`{"name","version":"0.1.0","display_name","homepage":"https://tokenfish.cn/skills/<slug>","localized":{"zh":{"title","summary","tags":[...]},"en":{...}},"channel":"<构建时写入>","locale":"<构建时写入>"}`
- `agents/openai.yaml`（给 Codex）：`interface: {display_name, short_description, default_prompt}`。

包：
1. `skills/tokenfish/`（全能包）：写歌、作图的路由（最短路径：用户给了歌词就走歌词成曲流程，只要配乐就 instrumental），references 拆 `music.md`、`images.md`、`billing-and-errors.md`、`installation.md`。
2. `skills/lyrics-to-song/`（歌词成曲）：两种模式——**保留原词**（不改任何字，只加段落标签 `[Verse]`/`[Chorus]`/`[Bridge]`）与**优化歌词**（用户明确授权改写时）；默认保留。先整理段落，再出制作卡，再提交，再轮询交付。

## 4. 静态站 `site/`（参考 Beatra 的信息架构，文案与视觉原创）

- `index.html`：首屏一句定位「在你的 AI 里写歌、作图——发一条消息就能用」＋ **[复制这条消息]** 按钮（复制：`安装 TokenFish，需要我操作时告诉我：https://tokenfish.cn/install.md`）＋ 支持的 AI 工具名单（豆包工作、WorkBuddy、千问、Claude Code、Codex、Cursor）＋ 能力卡片（写歌 / 作图，后续：视频、配音）＋「开发者 API（虾盘云）」二级入口。首屏不出现"下载 App"。
- `skills/index.html`：货架；`skills/<slug>/index.html`：一句话用途、示例、**复制安装消息**（`安装 TokenFish 歌词成曲，需要我操作时告诉我：https://tokenfish.cn/skills/lyrics-to-song/install.md`）、价格说明（"按次扣费，以钱包流水为准"）。
- `install.md`、`skills/<slug>/install.md`、`install.json`：由 `build/build.py` 生成后复制进 site/。install.md 写给 AI：下载 zip → 校验 SHA-256 → 安全解压（拒绝绝对路径、`..`、反斜杠、符号链接、非预期顶层目录）→ 装到宿主 skills 目录（列出：Claude Code `~/.claude/skills/`、Codex `~/.codex/skills/`、WorkBuddy `~/.workbuddy/skills/`、Cursor `~/.cursor/skills/`、豆包工作用其技能目录）→ 运行 `verify`；没有凭据时告诉用户"需要一把 TokenFish/虾盘云 key，放到文件后运行 login --key-file"。**禁止出现 `curl … | bash`。**
- `llms.txt`：Start here → 能力 → 安装 → 计费 → 开发者 API。
- `console.html`：v0 占位页，说明钱包、作品、已连接 AI 工具在后续版本上线。
- zip 下载地址先用相对路径 `/dist/<slug>/<channel>/<version>/<slug>-<version>.zip`。

## 5. 构建 `build/build.py`

- 渠道×语言：`skillhub/zh-CN`、`canonical/zh-CN`（v0 只出这两个）。
- 每个包：复制 client → `scripts/tokenfish_client.py`，写入 manifest 的 channel/locale，打 zip（顶层目录 = slug），算 sha256，写 `install.json`（`{"package","version","channel","locale","archive","archive_sha256"}`）和由模板渲染的 `install.md`，产物到 `dist/` 并同步到 `site/`。
- 构建可重复：同输入同 sha256（固定 zip 时间戳、排序）。

## 6. 验收（实现者跑完原样贴输出，不要自己下"通过"结论）

1. `python -m unittest discover -s tests -v`：覆盖凭据读取优先级与打码、key 不出现在任何 stdout/stderr、402/401(余额≤0)/401(余额>0)/429/5xx 的错误归一、Suno 本地幂等（同参返回旧任务、异参冲突）、MiniMax 幂等键合规化、instrumental+lyrics 拒绝、duration 越界在发请求前就拒绝、Suno 与 MiniMax 任务状态归一化。
2. `python build/build.py` 两次输出的 sha256 一致；解压后结构正确。
3. 站点所有相对链接在 `python -m http.server -d site` 下可达（写个小脚本检查）。
4. 全仓 `grep -ri "suno\|minimax"` 只允许出现在 client 的实现代码、tests 和 references 的"上游说明"里，不得出现在任何 slug、title、description、站点标题里。
