# 脚本与凭据

所有工具都通过同目录下的 `scripts/tokenfish_client.py` 调用，不需要额外安装依赖（纯 Python 3.10+ 标准库）。

## 常用命令

```bash
python scripts/tokenfish_client.py verify          # 验证凭据、查余额，不扣费
python scripts/tokenfish_client.py tools            # 查看全部工具及参数
python scripts/tokenfish_client.py call <tool>      # 调用工具，参数 JSON 从 stdin 传入
python scripts/tokenfish_client.py whoami           # 查看打码后的 key 和余额
python scripts/tokenfish_client.py topup            # 打开充值页并把 key 复制到剪贴板
```

## 凭据从哪来

- 优先读 `~/.tokenfish/credentials.json`。
- 如果这个文件不存在，会**只读**尝试 `~/.uking/device.json`（U-King 用户共用同一个钱包），不会写入这个文件。
- 沙箱里 home 目录不可写时：让用户把环境变量 `TOKENFISH_HOME` 设为一个可写目录（任务记录和 `login` 的凭据都放那里；`whoami` 会显示实际用的 `state_dir`）。
- 如果两个都没有，任何调用都会返回 `not_logged_in` 错误。这种情况下，让用户准备好一个包含 key 的文本文件，然后运行：

```bash
python scripts/tokenfish_client.py login --key-file /path/to/key.txt
```

`key` 本身永远不会出现在任何命令输出里；`whoami` 只显示打码后的样子（如 `sk-****1234`）。不要要求用户把 key 直接贴在聊天里，也不要把 key 写进任何命令行参数或环境变量。
