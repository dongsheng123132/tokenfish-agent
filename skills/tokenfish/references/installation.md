# 脚本与凭据

所有工具都通过同目录下的 `scripts/tokenfish_client.py` 调用，不需要额外安装依赖（纯 Python 3.10+ 标准库）。

## 常用命令

```bash
python scripts/tokenfish_client.py setup           # 没有任何凭据时免注册开一个新钱包（已有凭据则不动）
python scripts/tokenfish_client.py verify          # 验证凭据、查余额，不扣费
python scripts/tokenfish_client.py tools            # 查看全部工具及参数
python scripts/tokenfish_client.py call <tool>      # 调用工具，参数 JSON 从 stdin 传入
python scripts/tokenfish_client.py whoami           # 查看打码后的 key 和余额
python scripts/tokenfish_client.py topup            # 在本机浏览器打开充值页（桌面环境用）
```

## 凭据从哪来

- 优先读 `~/.tokenfish/credentials.json`。
- 如果这个文件不存在，会**只读**尝试 `~/.uking/device.json`（U-King 用户共用同一个钱包），不会写入这个文件。
- 沙箱里 home 目录不可写时：让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录（任务记录和 `login` 的凭据都放那里；`whoami` 会显示实际用的 `state_dir`）。
- 如果两个都没有，任何调用都会返回 `not_logged_in` 错误。这种情况下**先问用户**：开一个新钱包（免注册；新钱包余额为 0，需要先充值），还是导入已有的 key。

开新钱包（用户同意后）：

```bash
python scripts/tokenfish_client.py setup
```

返回 `status: "created"`，带 `saved_to`（key 文件的位置）、`backup_notice` 和 `next_step`，原样转述给用户（key 就是钱包，请备份；新钱包余额为 0，接着就问用户要充多少元并走 `tokenfish.wallet.topup`）；输出里只有打码的 `key_masked`，永远没有完整 key。`status: "already_configured"` 表示本机已有凭据，没有新开。沙箱可能清空文件时，先让用户把环境变量 `TOKENFISH_HOME` 设为一个持久的可写目录再 `setup`。

导入已有 key：让用户准备好一个包含 key 的文本文件，然后运行：

```bash
python scripts/tokenfish_client.py login --key-file /path/to/key.txt
```

`key` 本身永远不会出现在任何命令输出里；`whoami` 只显示打码后的样子（如 `sk-****1234`）。不要要求用户把 key 直接贴在聊天里，也不要把 key 写进任何命令行参数或环境变量。
