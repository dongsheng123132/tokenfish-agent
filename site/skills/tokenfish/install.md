<!--
  占位文件 — 由 build/build.py 用 build/templates/install.md.tmpl 生成后会整体覆盖本文件
  （build/ 由另一分支实现）。这里先让 skills/tokenfish/index.html 里的
  「复制安装消息」按钮引用的 https://tokenfish.cn/skills/tokenfish/install.md 能打开，
  结构对齐 SPEC.md §4/§5。内容与站点根目录的 install.md 一致（同一个全能包）。
-->

# 安装 TokenFish 全能包

写给 AI 读：收到用户「安装 TokenFish 全能包」的请求后，按下面步骤自己完成。

## 1. 下载

```
/dist/tokenfish/skillhub/zh-CN/<version>/tokenfish-<version>.zip
/dist/tokenfish/canonical/zh-CN/<version>/tokenfish-<version>.zip
```

> 占位说明：真实 `<version>` 和对应的 `archive_sha256` 在同目录的 `install.json` 里，
> 由 `build/build.py` 生成，不要凭空拼版本号。

## 2. 校验 SHA-256

必须与 `install.json` 的 `archive_sha256` 一致，不一致就停止并告知用户。

## 3. 安全解压

拒绝绝对路径、包含 `..` 的路径、反斜杠路径分隔符、符号链接、非预期顶层目录（顶层目录必须是 `tokenfish`）。

## 4. 安装到宿主目录

| AI 工具 | 技能目录 |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.codex/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` |
| Cursor | `~/.cursor/skills/` |
| 豆包工作 | 使用其自身技能目录 |

## 5. 验证凭据

```
python scripts/tokenfish_client.py verify
```

没有凭据文件时，提示用户把虾盘云 / TokenFish key 存到文件后运行
`python scripts/tokenfish_client.py login --key-file <path>`，不要让用户把 key 贴进对话。

之后即可写歌、作图，按次扣费，以钱包流水为准。
