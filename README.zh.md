[English](./README.md) | **中文**

# 更新 VS Code (stable / Insiders) agent-host 的 SDK 缓存 (claude / codex)

> 目的:**不用订阅 Copilot,也能使用本地的 claude / codex**。

没有 Copilot 订阅时,agent-host 无法自己触发 SDK 下载——日志里会出现

```
[Claude] SDK not downloaded yet; deferring chat metadata until a session triggers the download
```

但「session 触发下载」永远不会发生,于是 claude/codex 一直不可用。
本脚本按**各通道实际期望的版本**把 SDK 包预置到 agent-host 的 sdk-cache 目录,本地即可直接使用 claude / codex。

## 工作原理

- 版本来源——**按通道解析**(每个 VS Code 构建都内置了自己 pin 的 agent-SDK 版本):
  1. 该通道已安装 VS Code `product.json`(`resources/app/product.json`)内置的 `agentSdks`
     映射;新版安装目录带一层 commit 哈希子目录,新旧两种布局都会自动识别、取最新。
     这是该构建的 agent-host 实际会请求的版本——例如 stable 1.136.x 内置 claude `0.3.239`、codex `0.146.0`。
  2. 找不到安装信息 / 旧构建没有 `agentSdks`: 回退到 [microsoft/vscode](https://github.com/microsoft/vscode)
     仓库 `package.json` 的 `devDependencies`——stable 按已装版本推导对应 `release/<x>` 分支,
     Insiders 用 `main`(即 `--branch`)。
  - claude ← `@anthropic-ai/claude-agent-sdk`
  - codex  ← `@openai/codex`
- 下载地址: `https://main.vscode-cdn.net/agent-sdk/<tool>/<version>/<arch>.tgz`
- 支持的安装通道(默认 `both`):

  | 通道 | 本机缓存(Windows) | 本机缓存(其他平台) | 服务器缓存(SSH) |
  |---|---|---|---|
  | Insiders | `~/AppData/Roaming/Code - Insiders/agent-host/sdk-cache/` | `~/.vscode-server-insiders/data/agent-host/sdk-cache/` | `~/.vscode-server-insiders/data/agent-host/sdk-cache/` |
  | Stable | `~/AppData/Roaming/Code/agent-host/sdk-cache/` | `~/.vscode-server/data/agent-host/sdk-cache/` | `~/.vscode-server/data/agent-host/sdk-cache/` |

  实际目录: `<...>/sdk-cache/<tool>/<version>/<arch>/`,arch 如 `win32-x64` / `linux-x64`。
- 完成标记: 解压校验完成后在 `<arch>/` 下新建空文件 `.complete`,与 agent-host 原生布局一致

## 前提

- 本机: Python 3(纯标准库,零依赖);Windows 10 1803+(自带 System32\tar.exe);网络可达 vscode-cdn.net
- 推送服务器: SSH 免密(密钥已配置在 ~/.ssh),服务器自带 tar

## 用法

```bash
python update_agent_sdk.py                       # 更新本机全部通道(claude + codex)
python update_agent_sdk.py --server <ssh别名>     # 本机更新 + SSH 推送服务器(两个通道)
python update_agent_sdk.py --channel stable       # 只更新 Stable 通道
python update_agent_sdk.py --tool codex           # 只更新 codex
python update_agent_sdk.py --dry-run --server <别名>  # 只预览
python update_agent_sdk.py --server-only --server <别名>  # 只推送服务器
```

| 参数 | 说明 |
|---|---|
| `--server <SSH_ALIAS>` | SSH 别名或 `user@host`,来自 ~/.ssh/config;给出后同时推送 linux-x64 到服务器 |
| `--channel insiders\|stable\|both` | 默认 `both` |
| `--tool claude\|codex\|all` | 默认 `all` |
| `--branch <分支或tag>` | 找不到安装的 product.json 时才用到的 vscode 仓库分支/tag;默认 `main`(Insiders 行,目标是 stable 时应指 `release/<x>`) |
| `--local-root <路径>` | 本机缓存根目录,默认从 `%APPDATA%` / `~` 按通道自动推导 |
| `--dry-run` | 只报告动作,不下载不解压不推送 |
| `--local-only` / `--server-only` | 只做一侧 |

行为: 每个 tool 的每个通道,若目标版本目录已有 `.complete` 则跳过;**不删除旧版本**。
**只更新已安装的通道**:某通道的 VS Code profile 目录(本机 `%APPDATA%\Code` / `Code - Insiders`、
服务器 `~/.vscode-server` / `~/.vscode-server-insiders`)不存在时,视为该通道未安装,直接跳过且**不会创建任何目录**。
**同版本已在任何一处装好,就不重复下载**:本机某通道已装好时,其余缺失通道直接复制该已装目录
(`robocopy` / `cp -a`,免下载);服务器同理——服务器某通道已装好时,其余通道在服务器内直接 `cp -a` 复用,
不再下载 linux 包。只有任何一处都没有副本时,才「下载 → 流式校验包内 version 字段 → 解压 → 原子改名 → 写 `.complete`」;
同一版本需要装多个目标时该包也只下载一次(本机各通道、服务器各通道均复用)。
任一失败即清理临时产物,逐个 tool 独立,一个失败不影响另一个。

## 定时示例

Windows(计划任务,每天 09:00,推送场景):

```bat
schtasks /create /tn "agent-sdk-update" /tr "py -3 <你的路径>\vscode-agent-sdk-update\update_agent_sdk.py --server <ssh别名>" /sc daily /st 09:00
```

Linux 服务器(若直接在服务器上跑:服务器上会自动走 `~/.vscode-server[-insiders]` 路径):

```cron
0 17 * * * cd ~/vscode-agent-sdk-update && python3 update_agent_sdk.py --tool all
```

## 许可

[MIT](./LICENSE)

## 常见问题

- **`win-x64` 会 404**: CDN 上 arch 名为 `win32-x64`(缓存目录同样是 `win32-x64`),脚本自动识别,无需关心。
- **明明升级了版本目录里却没有新 SDK 文件**: 检查失败日志中是否版本校验未通过,可用 `--dry-run` 先看打算装哪个版本。
- **首次下载较慢**: claude ≈ 96MB、codex ≈ 133MB,视网速约 1-3 分钟。
- **服务器推送前请先 `ssh <别名>` 手动验证连通性**。
- **缓存里已有更新的版本(如 claude `0.3.258`),但我的 stable 不认**: 旧版脚本从仓库 `main` 分支取版本,而 `main` 是
  Insiders 行;stable 实际期望的是安装的 `product.json` 里 `agentSdks` 的版本。重新跑一遍(或 `--channel stable`)即可把匹配
  版本补上;旧目录不会删除、也不碍事——agent-host 只找它自己构建 pin 的那个版本目录。
- **装了但没找到安装信息(如便携版 zip 不在标准安装目录)**: stable 会回退 `--branch`(默认 `main`),
  此时脚本会打印警告——请显式加 `--branch release/<x>`(对应你当前 stable 的版本,如 `release/1.136`)。
- **`raw.githubusercontent.com` 不通但 `api.github.com` 可用**: 脚本会自动从 raw 回退到 GitHub contents API,无需配置。
