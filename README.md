# TokenFish Agent

让 AI 工具（豆包工作 / WorkBuddy / Claude Code / Codex / Cursor）一句话调用写歌、作图能力。后端：虾盘云。规格见 [SPEC.md](SPEC.md)，实现约定见 [AGENTS.md](AGENTS.md)。

## 仓库结构

```
client/tokenfish_client.py     # 唯一的远程调用实现，Python 3.10+ 纯标准库
skills/tokenfish/               # 全能包：写歌 + 作图
skills/lyrics-to-song/          # 场景包：歌词成曲
build/build.py                  # 打包脚本：出 zip + sha256 + install.json/install.md
tests/                          # unittest，对本地假 HTTP 服务测试，不碰真实 API
site/                           # 静态站（另一分支维护，见其 PR）
```

## 客户端

```bash
python client/tokenfish_client.py verify                  # 验证凭据、查余额，不扣费
python client/tokenfish_client.py tools                    # 列出全部工具及参数
python client/tokenfish_client.py call <tool>               # 参数 JSON 从 stdin 传入，结果 JSON 打到 stdout
python client/tokenfish_client.py setup                     # 没有任何凭据时免注册开一个新钱包（余额 0；已有凭据则不动）
python client/tokenfish_client.py login --key-file <path>   # 从文件读 key 并写入凭据文件（导入已有 key）
python client/tokenfish_client.py whoami                    # 显示打码后的 key 和余额
python client/tokenfish_client.py topup                     # 在本机浏览器打开充值页（桌面环境用）
```

凭据来源：`~/.tokenfish/credentials.json`，不存在时只读回退到 `~/.uking/device.json`（U-King 用户共用同一个钱包）。key 永远不出现在 stdout/stderr/日志/命令参数/环境变量里。

状态目录（任务记录 `tasks.json`，以及 home 不可写时 `login` 存的凭据）取第一个可写的：`$TOKENFISH_HOME` → `~/.tokenfish` → `<临时目录>/tokenfish-<用户名>` → `<cwd>/.tokenfish`。沙箱化的宿主里 home 常常不可写，建议把 `TOKENFISH_HOME` 设为一个可写目录（`whoami` 会显示实际用的 `state_dir`）；作图文件目录可用 `TOKENFISH_OUTPUT_DIR` 指定。

## v0.2：对话内开钱包、充值、回链

- **`setup`**（CLI 子命令，不是 `call` 工具）：本机没有任何凭据（含 `~/.uking/device.json`）时，免注册开一个新的空钱包（`POST /device/bind`），key 和 `wallet_id` 存进状态目录。新钱包没有体验金，余额为 0，要先充值。输出 `backup_notice`（key 就是钱包，请备份）和 `next_step`。已有凭据绝不新开；AI 开之前必须先告知用户并取得同意。
- **`tokenfish.wallet.topup`**：`{amount: 1..500 的整数元, channel: alipay|wechat}` → `{order_id, channel, amount_cny, pay_link, next_step}`。支付宝给网关的 `payUrl`，微信给 `site/pay.html` 的链接（页面本地画二维码）。key 只出现在 `POST /recharge/create` 的请求体里，不进任何 URL 和输出。
- **`tokenfish.wallet.topup_status`**：`{order_id}` → `{order_id, status, credited, balance_cny?}`；`status` 为 `recharged` 才算到账。
- **成功结果自带链接**：音乐每个 clip、图片（有 URL 的）带 `play_url`（指向 `site/play.html`），顶层带 `links`、`balance_after_cny`、`topup_hint`；这些由客户端打印，不靠 AI 拼。
- **静态页**：`site/play.html`（试听 / 预览 / 下载）与 `site/pay.html`（微信付款码）读 URL 的 `#` 片段（不发给服务器），二维码由 `site/assets/qrcode.js`（qrcode-generator，MIT）在浏览器本地画。

## 打包

```bash
python build/build.py
```

为 `skills/` 下每个包、每个渠道×语言（v0：`skillhub/zh-CN`、`canonical/zh-CN`）生成确定性 zip（同输入同 sha256）+ `install.json` + 由 `build/templates/install.md.tmpl` 渲染的 `install.md`，产物写到 `dist/`（已 gitignore）；若 `site/` 目录已存在，同步覆盖对应的 `install.json`/`install.md`。

## 测试

```bash
python -m unittest discover -s tests -v
```

用 `tests/fake_server.py` 起一个本地假 HTTP 服务模拟后端，不发出任何真实网络请求。
