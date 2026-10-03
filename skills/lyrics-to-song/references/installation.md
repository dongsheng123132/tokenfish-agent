# 脚本与凭据

所有工具都通过同目录下的 `scripts/tokenfish_client.py` 调用，纯 Python 3.10+ 标准库，不需要额外安装依赖。

```bash
python scripts/tokenfish_client.py verify          # 验证凭据、查余额，不扣费
python scripts/tokenfish_client.py call tokenfish.music.generate   # 参数 JSON 从 stdin 传入
python scripts/tokenfish_client.py call tokenfish.tasks.get        # 轮询任务状态
python scripts/tokenfish_client.py whoami          # 查看打码后的 key 和余额
python scripts/tokenfish_client.py topup           # 打开充值页并把 key 复制到剪贴板
```

## 凭据从哪来

- 优先读 `~/.tokenfish/credentials.json`。
- 不存在时只读尝试 `~/.uking/device.json`（U-King 用户共用同一个钱包），不会写入这个文件。
- 都没有时，任何调用都会返回 `not_logged_in`。让用户准备一个包含 key 的文本文件，运行：

```bash
python scripts/tokenfish_client.py login --key-file /path/to/key.txt
```

`key` 永远不会出现在任何命令输出里；`whoami` 只显示打码后的样子（如 `sk-****1234`）。不要让用户把 key 直接贴进聊天。
