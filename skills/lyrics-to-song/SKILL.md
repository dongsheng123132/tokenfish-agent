---
name: lyrics-to-song
description: 把歌词变成歌：歌词生成歌曲、歌词谱曲、中文歌词成曲、AI 作曲。已经写好一段歌词（或歌词草稿、几句 hook），想做成一首能听的完整人声歌曲——先帮你分好主歌副歌，默认一字不改，确认制作卡后一次生成，按次扣费。
---

# 歌词成曲

这个 Skill 只做一件事：把用户已经写好的歌词变成一首歌。所有远程操作都必须通过同目录下的 `scripts/tokenfish_client.py` 完成——不要配置其他 MCP、不要拼 `curl`、不要直接访问任何 API 地址。

## 两种模式

- **保留原词（默认）**：不改用户歌词里的任何一个字，只负责加段落标签（`[Verse]` / `[Chorus]` / `[Bridge]` 等），帮助上游模型更好地演唱。
- **优化歌词**：只有用户明确说"帮我改一下/润色一下/优化一下歌词"这类授权时才能进入这个模式。改完之后要把改前改后都展示给用户，确认后才继续。

**默认永远是保留原词**，不要主动建议"我帮你改得更好听"。

## 完整流程

1. **整理段落**（免费，不扣费）：读一遍用户给的歌词，按段落结构加标签。保留模式下逐字保留原文，只是加标签换行；优化模式下先展示改动再让用户确认。
2. **确认细节**：风格描述（曲风、情绪、参考）、标题（可选）、是否需要纯伴奏版本（一般不需要，因为已经有歌词了）。
3. **出制作卡**（一次确认）：完整歌词（含段落标签）、标题、风格描述、将使用的模型（默认 `auto`）、"下一步会扣费"。这张卡必须让用户能一眼看到最终会提交的歌词全文，不能只给摘要。
4. **一次提交**：用户明确同意后，生成 `client_request_id`（只在这一步生成，不要提前生成），调用 `tokenfish.music.generate`：
   - `prompt`：风格描述
   - `lyrics`：整理好的歌词（含段落标签）
   - `title`：标题（如果有）
   - `instrumental`：`false`（这个 Skill 默认是要人声成曲；如果用户中途改主意只要伴奏，改用全能包 `tokenfish` 的纯音乐路径）
   - `model`：`auto`（没有特殊要求就不要改）
5. **轮询交付**：如果返回状态不是终态，用 `tokenfish.tasks.get` 按 `task_id` 轮询，直到 `succeeded` 或 `failed`；提醒用户上游出稿一般要 1-2 分钟到几分钟。交付时按 `clips` 顺序给出每首的 `audio_url`、`duration_seconds`，并报告 `charged_cny`（没有就说"以钱包流水为准"）。

## 余额不足 / 出错时

直接转述客户端返回的 `error.message` 和 `next_action`，不要自己编造充值步骤或重试建议。详见 `references/billing-and-errors.md`。

- **`outcome_unknown`：绝对不要重试**（同一个或新的 `client_request_id` 都不行）。如实告诉用户这次请求可能已经提交并扣费，调用 `tokenfish.wallet.get` 展示余额，建议用户查看钱包流水核对。
- **`local_state_unwritable`**：本次未提交、未扣费，让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录后再试。
- 结果里带 `warning` 字段时同样不要重试：把 `warning` 的意思如实告诉用户（这次可能已经扣费）；结果里有 `task_id` 和 `provider` 时，调用 `tokenfish.tasks.get` 并带上这两个值取回作品，不要重新提交。

## 参考

- `references/workflow.md`：两种模式的详细示例（含制作卡格式样例）。
- `references/billing-and-errors.md`：错误码对照表。
- `references/installation.md`：脚本用法与凭据配置。
