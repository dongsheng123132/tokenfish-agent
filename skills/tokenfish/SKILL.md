---
name: tokenfish
description: 想在正在用的 AI 对话里直接写一首歌、配一段纯音乐或生成一张图，不想跳去别的网站、不想另外开账号充值——用你已有的余额，一句话在对话里生成音乐或图片，按次扣费。
---

# TokenFish：在对话里写歌、作图

TokenFish 是一个门面：真正干活的是 `scripts/tokenfish_client.py`，它替你调用后端的音乐、图片生成能力，并按你已有的钱包余额计费。**所有远程操作都必须通过这个脚本完成**——不要建议用户配置其他 MCP 服务，不要拼 `curl` 命令，不要直接访问任何 API 地址。

## 先判断用户要什么

1. **用户要一首完整的歌，并且已经给了歌词** → 走"歌词成曲"流程，见 `references/music.md` 的「已有歌词」小节（保留原词 / 优化歌词两种模式，默认保留）。
2. **用户只要配乐、伴奏、纯音乐（不需要人声/歌词）** → 最短路径：直接把 `instrumental` 设为 `true`，不要问歌词、不要生成歌词。见 `references/music.md` 的「纯音乐」小节。
3. **用户要一首歌但还没写词、只有大概想法** → 先帮用户把风格、情绪、用途聊清楚，生成一版简短的风格描述（`prompt`），再按第 1 步走（可选是否需要歌词）。
4. **用户要生成图片** → 见 `references/images.md`。

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

## 硬规则（每次都适用）

- **一次确认、一次提交**：免费的策划/歌词整理/风格描述可以来回改；但只要下一步会扣费，就必须先给用户看一张完整的"制作卡"（内容见下），用户明确同意后才提交，且只提交一次。制作卡定稿之后才生成 `client_request_id`（生成方式：内容确定性摘要即可，比如基于当前时间和用户会话的简短唯一字符串；不要在制作卡确认前提前生成，否则同一次沟通中反复修改会占用不同的幂等键）。
- **制作卡必须包含**：完整歌词（如果有）或图片的完整提示词、标题（如果有）、风格/尺寸等参数、将使用的模型、以及"下一步会扣费"的提示。
- **余额不足时**：转述客户端返回的 `message`（说明本次未扣费），然后按上面的「充值」流程提供付款链接；不要自己编充值步骤或金额。
- **出现 `outcome_unknown` 时绝对不要重试**（同一个或新的 `client_request_id` 都不行）：如实告诉用户这次请求可能已经提交并扣费，调用 `tokenfish.wallet.get` 展示当前余额，建议用户查看钱包流水核对；确认没扣费之前不要再次提交。出现 `local_state_unwritable` 表示本次未提交、未扣费，让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录后再试。结果里带 `warning` 字段时同样不要重试：把 `warning` 的意思如实告诉用户（这次可能已经扣费）；结果里有 `task_id` 和 `provider` 时，调用 `tokenfish.tasks.get` 并带上这两个值取回作品，不要重新提交。
- **交付时**：按返回顺序给出每个 clip 的 `play_url`（作品页，可在线试听和下载）和 `audio_url`、时长（如果有）；图片给出每张的 `play_url`（有就给）和路径 / URL。同时**原样**给出 `links.site`（虾盘云官网）、扣费 `charged_cny`（没有就说"以钱包流水为准"，不要自己按时长或数量估算费用），以及 `balance_after_cny` 和 `topup_hint`（有就原样给）。这些链接和文字都是客户端生成的，不要改写、不要省略；`play_url` 是临时链接，约 24 小时内有效，提醒用户及时下载。
- **不确定余额或凭据是否正常时**，先调用 `tokenfish.wallet.get`（不扣费），别直接猜。
- 价格只能引用工具返回里的说明（如"以实时扣费为准"），不要编造具体数字。

更多细节：
- `references/music.md`：音乐生成的两条路径（已有歌词 / 纯音乐）与参数说明。
- `references/images.md`：图片生成流程与参数说明。
- `references/billing-and-errors.md`：计费单位、常见错误怎么向用户解释。
- `references/installation.md`：脚本怎么用、凭据怎么配置（开新钱包 / 导入已有 key）。
