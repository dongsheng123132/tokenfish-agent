# 图片生成

调用工具：`tokenfish.images.generate`（计费）。参数：

| 参数 | 说明 |
|---|---|
| `prompt` | 图片描述，必填。 |
| `n` | 张数，1..4，默认 1。 |
| `size` | 尺寸字符串，默认 `1024x1024`。按用户需求调整（比如竖版海报可以用更高的高度）。 |
| `quality` | `low` / `medium` / `high` / `auto`，默认 `auto`。 |
| `client_request_id` | 必填，制作卡确认后再生成。 |

## 流程

1. 和用户把描述确认清楚（主体、风格、构图、张数、用途），必要时建议合适的 `size`。
2. 出制作卡：最终提示词全文、张数、尺寸、画质、"下一步会扣费"。
3. 用户确认后生成 `client_request_id`，调用一次 `tokenfish.images.generate`。
4. 图片生成是同步的，返回即是终态：按 `images` 顺序展示每张图的本地路径（在 `./tokenfish-outputs/` 下）或 URL。
5. 报告 `charged_cny`（没有就说"以钱包流水为准"），不要按张数自己估算价格。

模型固定为 `gpt-image-2`（客户端已内置，不需要用户选择）。
