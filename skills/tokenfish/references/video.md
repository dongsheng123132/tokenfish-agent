# 一句话做视频（文生视频）

调用工具：`tokenfish.video.generate`（计费，**按秒计费**）。参数：

| 参数 | 说明 |
|---|---|
| `prompt` | 画面描述，必填，不超过 2000 字符。写清主体、动作、场景、光线、镜头感、风格（例如"一只橘猫在窗台上打哈欠，阳光洒进来，写实风格"）。 |
| `duration` | 时长（秒），整数 5..15，默认 5。越长越贵。 |
| `resolution` | `480p`（默认，最省）/ `720p` / `1080p`。清晰度越高越贵。 |
| `model` | 默认 `doubao-seedance-2-0-mini-260615`（最便宜）。其余可选：`doubao-seedance-2-0-fast-260128`、`doubao-seedance-2-0-260128`、`wanx2.1-t2v-turbo`、`hy-video-1.5`。不确定就用默认。 |
| `client_request_id` | 必填，制作卡确认后再生成，同一张卡只用一个值。 |

可用模型和参考价可以先用 `tokenfish.models.list`（`{"capability":"text_to_video"}`，不扣费）确认；价格字段写的是"以实时扣费为准"。

## 流程

1. 和用户把画面聊清楚：主体、动作、场景、光线、镜头、风格；**问清时长（5..15 秒）和清晰度（480p / 720p / 1080p）**。默认给最省的组合：`duration=5`、`resolution=480p`、默认模型。
2. **先报估价再生成**：客户端会返回 `estimated_cost_cny`（按 5 秒 / 480p 基准价 × 时长倍数 × 清晰度倍数算的估算；`cost_note` 固定为"估算，实扣以钱包流水为准"）。视频按秒计费、整体比图片和配音贵，制作卡里要写明估价并说明只是估算，用户同意后再提交。
3. 出制作卡：画面描述全文、时长、清晰度、模型、"下一步会扣费，预计约 ¥X（估算，实扣以钱包流水为准）"。
4. 用户确认后生成 `client_request_id`，调用**一次** `tokenfish.video.generate`。视频是异步任务：客户端提交后会每 5 秒查一次状态，**最多等约 180 秒**。
   - 拿到终态：`status` 为 `succeeded` 时会自动下载并校验 MP4，返回 `file` 本地路径；`failed` 时把 `fail_reason` 如实告诉用户。
   - **没拿到终态不算失败**：返回 `status: "running"` 和 `task_id`，并带一条 `warning`。这是正常的异步模式（和音乐任务一样），**不要重新提交**。
5. 超时/还没出片时：告诉用户"还在生成，已经提交、可能已扣费，稍后取回"，然后把返回里的 `task_id` 和 `provider`（值为 `video`）传给 `tokenfish.tasks.get` 继续查；查成功后客户端会自动下载 MP4，返回 `file` 和 `video_url`。可以每隔几十秒查一次，**绝对不要重新提交（会再次扣费）**。
6. 交付：给出本地 MP4 的 `file` 路径和 `video_url`（说明产物是 mp4；`video_url` 是临时链接，提醒用户及时下载或另存）；报告 `charged_cny`（没有就说"以钱包流水为准"）、`estimated_cost_cny`（估算）、`cost_note`，并**原样**给出 `links.site`、`balance_after_cny` 和 `topup_hint`（有就原样给）。

## 计费

- 按秒计费，时长和清晰度都会放大价格；模型之间也有差价（默认的 mini 最便宜）。
- 客户端给的 `estimated_cost_cny` 只是**估算**（基准价 5 秒 / 480p：mini ≈ ¥2.9、fast ≈ ¥4.9、pro ≈ ¥6.9；时长 ×(duration/5)，720p ×1.5，1080p ×2.5）。`cost_note` 一律是"估算，实扣以钱包流水为准"，不要把估算当成账单。`wanx2.1-t2v-turbo`、`hy-video-1.5` 没有参考价，估算字段为 `null`，直接说"以钱包流水为准"。
- 真正扣了多少只看钱包流水；`charged_cny` 只有服务端给了才有，没有就如实说不知道。

## 失败处理

- **参数错误**（`invalid_arguments`）：客户端在发请求前就拒绝（时长不在 5..15、清晰度/模型不在枚举里、`prompt` 或 `client_request_id` 缺失），不会扣费。按返回的合法取值改，不用换 `client_request_id`（还没提交过）。
- **余额不足**（`insufficient_balance`）：本次未扣费，走 SKILL.md 的「充值」流程；到账后用同一个 `client_request_id` 重试。
- **`outcome_unknown`**：提交可能已经到了服务端、可能已扣费——**绝对不要重试**（同一个或新的 `client_request_id` 都不行）。展示当前余额，请用户看钱包流水核对。
- **`download_failed`**：视频已经生成成功（可能已扣费），只是下载或校验没过（HTTP 错误、返回的是 JSON/HTML、文件太小、不是 MP4）。客户端已删掉坏文件、**没有重新提交**。用返回的 `task_id` + `provider: "video"` 调 `tokenfish.tasks.get` 重新下载；仍不行就让用户用 `video_url` 在浏览器下载。
- **`warning`**（常见于超时返回 `running`）：不要重试，按提示用 `tokenfish.tasks.get` 取回。
- **服务端明确失败**（`status: "failed"`）：把 `fail_reason` 转述给用户；不要把同一个任务当成"还在跑"反复轮询，也不要自作主张重新提交——先问用户要不要换描述/参数再生成一次（那是新的付费提交，需要重新确认）。
