# 计费与错误处理

## 计费单位

- 额度单位是 quota，**¥1 = 500000 quota**。客户端已经帮你换算成 `balance_cny`/`charged_cny`，不需要自己再算。
- 价格描述统一用"以实时扣费为准"，不要编造具体单价或总价。
- 只有服务端在响应里明确给出扣费数字时才报告扣费金额；没有给就照实说"以钱包流水为准"，不要按片段数、时长或张数自己推算。

## 遇到错误时怎么和用户说

客户端把所有错误都归一成了 `{"error":{"code","message","retryable","next_action"}}`，直接使用里面的 `message` 转述给用户，不要自己改写或编造更详细的解释：

| `code` | 什么意思 | 你该做什么 |
|---|---|---|
| `not_logged_in` | 还没配置凭据 | 先问用户：开新钱包（`setup`）还是导入已有 key（`login --key-file`），见 `references/installation.md` |
| `insufficient_balance` | 余额不足，本次未扣费 | 转述 `message`；问用户充多少元（整数）和渠道，用 `tokenfish.wallet.topup` 拿付款链接，用户付款后用 `tokenfish.wallet.topup_status` 确认到账，再用同一个 `client_request_id` 重试 |
| `auth_invalid` | 凭据无效 | 引导用户重新 `login --key-file <path>` |
| `rate_limited` | 触发限流，可重试 | 稍等一下重试（生成类用同一个 `client_request_id`；`setup` 表示开钱包次数达上限、没有创建钱包，稍后再 `setup`） |
| `transient` | 上游/网络暂时不可用，已确认没有扣费（或服务端幂等），可重试 | 用同一个 `client_request_id` 原样重试 |
| `outcome_unknown` | 请求可能已经提交并扣费，结果无法确认，**不可重试** | **绝对不要重试**（同一个或新的 `client_request_id` 都不行）；告诉用户可能已扣费，调用 `tokenfish.wallet.get` 展示余额，建议用户查看钱包流水核对 |
| `local_state_unwritable` | 本地状态目录不可写，本次未提交、未扣费 | 让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录后再试 |
| `channel_unavailable` | 所选充值渠道暂不可用，没有创建订单 | 换另一个渠道重新调用 `tokenfish.wallet.topup`；两个都不可用就请用户到虾盘云官网充值 |
| `order_not_found` | 没有这个充值订单 | 确认 `order_id` 原样来自 `tokenfish.wallet.topup` 的返回值 |
| `invalid_arguments` | 参数不对 | 按 `message` 里指出的字段修正后再试 |
| `idempotency_conflict` | 同一个 `client_request_id` 之前提交过不同的参数 | 换一个新的 `client_request_id` 重新走一遍制作卡流程 |

**不要**把余额不足、认证失败之类的错误自己"包装"成更委婉或更详细的话术——直接转述客户端给的 `message` 和 `next_action`，避免向用户传递不准确的充值/重试步骤。

## 充值与到账

- 在对话里充值：`tokenfish.wallet.topup`（`amount` 为 1..500 的整数元，不支持小数；`channel` 为 `alipay` 或 `wechat`，默认 `alipay`）→ 把返回的 `pay_link` 原样给用户 → 用户说付好了之后调用 `tokenfish.wallet.topup_status`。
- `topup_status` 的 `status`：`pending` 未付款、`paid` 已付款正在到账（稍等几秒再查）、`recharged` 已到账（`credited` 为 `true`，同时给出 `balance_cny`）、`failed` 订单失败。
- 充值本身不扣费；key 不会出现在任何付款链接里。
