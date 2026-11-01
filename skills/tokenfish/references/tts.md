# 配音 / 文字转语音 / 旁白

调用工具：`tokenfish.tts.generate`（计费）。参数：

| 参数 | 说明 |
|---|---|
| `input` | 要合成的文本，必填。计费字符数不超过 5000（汉字按 2、其他按 1 计），超了客户端会直接报参数错、不发请求。 |
| `voice` | 音色，`female-tianmei`（女声，默认）/ `male-qn-qingse`（男声）。 |
| `client_request_id` | 必填，制作卡确认后再生成，同一张卡只用一个值。 |

## 流程

1. 和用户把要说的话确认清楚：文本内容、用途（旁白 / 口播 / 配音）、音色（默认女声，也可选男声）。
2. 出制作卡：完整文本、音色、"下一步会扣费"。文本较长（接近 5000 计费字符）时提醒用户计费口径：汉字按 2、其他按 1。
3. 用户确认后生成 `client_request_id`，调用一次 `tokenfish.tts.generate`。**TTS 是同步的，返回即终态，不需要轮询。**
4. 交付：给出返回里的 `file`（本地 MP3 路径）。MP3 会落到第一个可写的目录：`TOKENFISH_OUTPUT_DIR`（若设置）→ `./tokenfish-outputs/` → `<state_dir>/outputs`；如果 `file` 为 `null` 且带 `warning`，原样转述（文件没能保存，以钱包流水核对，不要重试）。
5. 报告 `charged_cny`（没有就说"以钱包流水为准"，不要按字数或时长自己估算价格）；并**原样**给出 `links.site`、`balance_after_cny` 和 `topup_hint`（有就原样给）。

音色固定两个：`female-tianmei`（女声）、`male-qn-qingse`（男声），默认女声。模型由客户端内置（`minimax-speech-2.8-turbo`），不需要用户选择。
