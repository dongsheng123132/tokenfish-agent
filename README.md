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
python client/tokenfish_client.py login --key-file <path>   # 从文件读 key 并写入凭据文件
python client/tokenfish_client.py whoami                    # 显示打码后的 key 和余额
python client/tokenfish_client.py topup                     # 打开充值页并把 key 复制到剪贴板
```

凭据来源：`~/.tokenfish/credentials.json`，不存在时只读回退到 `~/.uking/device.json`（U-King 用户共用同一个钱包）。key 永远不出现在 stdout/stderr/日志/命令参数/环境变量里。

状态目录（任务记录 `tasks.json`，以及 home 不可写时 `login` 存的凭据）取第一个可写的：`$TOKENFISH_HOME` → `~/.tokenfish` → `<临时目录>/tokenfish-<用户名>` → `<cwd>/.tokenfish`。沙箱化的宿主里 home 常常不可写，建议把 `TOKENFISH_HOME` 设为一个可写目录（`whoami` 会显示实际用的 `state_dir`）；作图文件目录可用 `TOKENFISH_OUTPUT_DIR` 指定。

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
