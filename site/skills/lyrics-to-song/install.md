# 安装 歌词成曲

写给 AI 读：收到用户「安装 歌词成曲」的请求后，按下面步骤自己完成，不要让用户手动操作。

## 1. 下载

从以下相对路径下载对应「渠道 × 语言」的 zip（v0 提供两个渠道，内容一致，任选其一）：

```
/dist/lyrics-to-song/skillhub/zh-CN/0.2.0/lyrics-to-song-0.2.0.zip
/dist/lyrics-to-song/canonical/zh-CN/0.2.0/lyrics-to-song-0.2.0.zip
```

本文件同目录的 `install.json` 记录了本页默认渠道对应的真实版本号与 `archive_sha256`，请以它为准，不要凭空拼版本号。

## 2. 校验 SHA-256

对下载到的 zip 计算 SHA-256，必须与 `install.json` 里的 `archive_sha256` 完全一致：

```
2c351cd8ca6db21b60af7c0e4b0648f3c0ccb8b96060710fa31f9063620970f5
```

不一致就停止安装并告知用户，不要继续解压。

## 3. 安全解压

解压前检查压缩包里的每一个条目，发现以下任意一种情况就拒绝解压并告知用户：

- 绝对路径
- 路径中包含 `..`（路径穿越）
- 路径使用反斜杠 `\` 作为分隔符
- 符号链接条目
- 顶层目录不是 `lyrics-to-song`

## 4. 安装到宿主目录

把解压出的 `lyrics-to-song/` 整个目录放进当前 AI 工具的技能目录：

| AI 工具 | 技能目录 |
|---|---|
| Claude Code | `~/.claude/skills/` |
| Codex | `~/.codex/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` |
| Cursor | `~/.cursor/skills/` |
| 豆包工作 | 使用其自身的技能目录 |

## 5. 验证凭据

```
python scripts/tokenfish_client.py verify
```

如果提示没有凭据（`not_logged_in`），**先问用户**：开一个新的 TokenFish 钱包（免注册；新钱包余额为 0，需要先充值），还是导入已有的 key。

用户同意开新钱包后运行：

```
python scripts/tokenfish_client.py setup
```

把返回的 `backup_notice`、`saved_to` 和 `next_step` 原样转述给用户（key 就是钱包，请备份那个文件；新钱包余额为 0，接着就问用户要充多少元）；输出里只有打码的 key，不要也不可能展示完整 key。

用户已有 key 的话，让用户把 key 存到一个文件里，然后运行：

```
python scripts/tokenfish_client.py login --key-file <path>
```

不要让用户把 key 直接贴进对话。验证通过后即可开始使用，按次扣费，以钱包流水为准；余额不足时按 SKILL.md 的「充值」流程在对话里拿付款链接。
