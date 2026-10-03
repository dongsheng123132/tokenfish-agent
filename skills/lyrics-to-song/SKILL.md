---
name: lyrics-to-song
description: 把歌词变成歌：歌词生成歌曲、歌词谱曲、中文歌词成曲、AI 作曲。已经写好一段歌词（或歌词草稿、几句 hook），想做成一首能听的完整人声歌曲——先帮你分好主歌副歌，默认一字不改，确认制作卡后一次生成，按次扣费。
---

# 歌词成曲

这个 Skill 只做一件事：把用户已经写好的歌词变成一首歌。所有远程操作都必须通过同目录下的 `scripts/tokenfish_client.py` 完成——不要配置其他 MCP、不要拼 `curl`、不要直接访问任何 API 地址。

## 第一次使用：没有凭据时

客户端返回 `not_logged_in`（本机没有任何凭据）时，**先问用户，不要自己直接开钱包**：

> 你还没有 TokenFish 钱包。我可以免注册给你开一个新钱包（新钱包余额是 0，需要先充值才能生成）；如果你已经有 key，也可以导入。你想怎么做？

- **用户同意开新钱包** → 运行 `python scripts/tokenfish_client.py setup`。把返回的 `backup_notice` 和 `saved_to` 原样转述给用户（key 就是钱包，请备份那个文件）；**永远不要展示完整 key**（输出里只有打码的 `key_masked`）。返回 `already_configured` 表示本机其实已有凭据、没有新开，直接继续。
  **`setup` 返回 `created` 之后，立刻进入下面的「充值」流程**：新钱包没有任何体验金，余额是 0。马上问用户要充多少元、用支付宝还是微信，然后调用 `tokenfish.wallet.topup`；不要先尝试一次付费生成（它一定会因余额不足失败）。`next_step` 字段是给用户看的提示，原样转述。
- **用户已有 key** → 让用户把 key 存到一个文件，再运行 `login --key-file <path>`（见 `references/installation.md`）。
- `setup` 返回 `rate_limited`：同一网络出口的开钱包次数有上限，没有创建任何钱包，稍后再试，不要连续重试。

## 充值

余额不足（`insufficient_balance`），或用户主动要充值时：

1. 问用户充多少元（1 到 500 的整数，不支持小数）、用支付宝还是微信（默认支付宝）。
2. 调用 `tokenfish.wallet.topup`（`{"amount": N, "channel": "alipay"|"wechat"}`；免费，不扣费）。
3. 把返回的 `pay_link` **原样**给用户，让用户自己点开付款；不要改写链接，不要自己拼付款链接。返回 `channel_unavailable` 时换另一个渠道。
4. 用户说付好了（「好了」）之后，调用 `tokenfish.wallet.topup_status`（`{"order_id": ...}`）：`credited` 为 `true` 表示已到账，同时给出 `balance_cny`；`status` 是 `pending` 或 `paid` 说明还没到账，稍等几秒再查；`failed` 如实告诉用户，并让用户到虾盘云官网（https://www.u-claw.org.cn）联系客服。
5. 到账后，之前因余额不足被拒绝的那次生成可以用同一个 `client_request_id` 原样重试。

在对话里充值一律走 `tokenfish.wallet.topup`（`topup` 命令只是桌面环境里打开浏览器充值页用的，对话流程里不要用）。

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
5. **轮询交付**：如果返回状态不是终态，用 `tokenfish.tasks.get` 按 `task_id` 轮询，直到 `succeeded` 或 `failed`；提醒用户上游出稿一般要 1-2 分钟到几分钟。交付时按 `clips` 顺序给出每首的 `play_url`（作品页，可在线试听和下载）、`audio_url`、`duration_seconds`，并**原样**给出 `links.site`、`charged_cny`（没有就说"以钱包流水为准"）、`balance_after_cny` 和 `topup_hint`（有就原样给）；`play_url` 是临时链接，最短约 12 小时就会失效，提醒用户尽快下载。

## 余额不足 / 出错时

直接转述客户端返回的 `error.message` 和 `next_action`，不要自己编造重试建议；余额不足时按上面的「充值」流程提供付款链接。详见 `references/billing-and-errors.md`。

- **`outcome_unknown`：绝对不要重试**（同一个或新的 `client_request_id` 都不行）。如实告诉用户这次请求可能已经提交并扣费，调用 `tokenfish.wallet.get` 展示余额，建议用户查看钱包流水核对。
- **`local_state_unwritable`**：本次未提交、未扣费，让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录后再试。
- 结果里带 `warning` 字段时同样不要重试：把 `warning` 的意思如实告诉用户（这次可能已经扣费）；结果里有 `task_id` 和 `provider` 时，调用 `tokenfish.tasks.get` 并带上这两个值取回作品，不要重新提交。

## 参考

- `references/workflow.md`：两种模式的详细示例（含制作卡格式样例）。
- `references/billing-and-errors.md`：错误码对照表。
- `references/installation.md`：脚本用法与凭据配置。
