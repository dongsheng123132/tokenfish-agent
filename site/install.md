<!--
  占位文件 — 由 build/build.py 生成后会整体覆盖本文件（另一分支正在实现 build/）。
  本文件只是让 index.html 里「复制这条消息」按钮引用的
  https://tokenfish.cn/install.md 现在就能打开、结构先对齐 SPEC.md §4/§5。
  真实版本会由 build/templates/install.md.tmpl 渲染，包含真实的
  zip 下载地址、SHA-256、版本号。这里默认对应「TokenFish 全能包」
  （skills/tokenfish/），因为首页的安装消息未指定具体子包。
-->

# 安装 TokenFish（全能包）

这段说明是写给 AI 读的：你（AI）在收到用户「安装 TokenFish」的请求后，按下面步骤自己完成。

## 1. 下载

从以下相对路径下载对应「渠道 × 语言」的 zip（v0 只出两个渠道：`skillhub/zh-CN`、`canonical/zh-CN`）：

```
/dist/tokenfish/skillhub/zh-CN/<version>/tokenfish-<version>.zip
/dist/tokenfish/canonical/zh-CN/<version>/tokenfish-<version>.zip
```

> 占位说明：具体 `<version>` 与该版本对应的 `install.json`（含 `archive`、`archive_sha256` 字段）
> 由 `build/build.py` 生成后写入本目录，届时以 `install.json` 里的字段为准，不要凭空拼版本号。

## 2. 校验

对下载到的 zip 计算 SHA-256，必须与 `install.json` 里的 `archive_sha256` 完全一致，不一致就停止安装并告知用户。

## 3. 安全解压

解压前检查压缩包条目，拒绝：

- 绝对路径
- 包含 `..` 的路径（路径穿越）
- 反斜杠路径分隔符
- 符号链接
- 非预期的顶层目录（顶层目录必须是 `tokenfish`）

## 4. 安装到宿主目录

按你所在的 AI 工具，把解压出的 `tokenfish/` 目录放到对应位置：

| AI 工具 | 技能目录 |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.codex/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` |
| Cursor | `~/.cursor/skills/` |
| 豆包工作 | 使用其自身技能目录 |

## 5. 验证凭据

运行：

```
python scripts/tokenfish_client.py verify
```

如果本地没有凭据文件（`~/.tokenfish/credentials.json`，也会只读尝试 `~/.uking/device.json`），
告诉用户：「需要一把 TokenFish / 虾盘云 key，把 key 存到一个文件里后运行
`python scripts/tokenfish_client.py login --key-file <path>`」。不要让用户把 key 直接贴在对话里。

安装完成后即可使用写歌、作图能力；每次调用按次扣费，以钱包流水为准。
