**English** | [中文](./README.zh.md)

# Update VS Code (stable / Insiders) agent-host SDK cache (claude / codex)

> Goal: **use local claude / codex without a Copilot subscription**.

Without a Copilot subscription, agent-host cannot trigger the SDK download itself — the log shows

```
[Claude] SDK not downloaded yet; deferring chat metadata until a session triggers the download
```

but "session triggers the download" never happens, leaving claude / codex unavailable.
This script pre-seeds the SDK packages into the agent-host sdk-cache directory with the **actual
version each channel expects**, so local claude / codex just work.

## How it works

- Version source — resolved **per channel** (each VS Code build pins its own agent-SDK versions):
  1. The `agentSdks` map embedded in that channel's installed VS Code `product.json` — for
     servers read via SSH from `~/.vscode-server[-insiders]` (`cli/servers/<Quality>-<commit>/server/…`,
     old `bin/<commit>/…` also detected, `.staging` downloads excluded, newest wins).
     This is what the agent-host of that exact build requests — e.g. stable 1.136.x ships
     claude `0.3.239`, codex `0.146.0`.
  2. No install info found / build without `agentSdks`: `devDependencies` of the vscode
     repository's `package.json` — stable uses the matching `release/<x>` branch (derived from
     the installed version), Insiders uses `main` (the `--branch` fallback).
  - claude ← `@anthropic-ai/claude-agent-sdk`
  - codex  ← `@openai/codex`
- Download URL: `https://main.vscode-cdn.net/agent-sdk/<tool>/<version>/<arch>.tgz`
- Supported install channels (default `both`):

  | Channel | Local cache (Windows) | Local cache (other platforms) | Server cache (SSH) |
  |---|---|---|---|
  | Insiders | `~/AppData/Roaming/Code - Insiders/agent-host/sdk-cache/` | `~/.vscode-server-insiders/data/agent-host/sdk-cache/` | `~/.vscode-server-insiders/data/agent-host/sdk-cache/` |
  | Stable | `~/AppData/Roaming/Code/agent-host/sdk-cache/` | `~/.vscode-server/data/agent-host/sdk-cache/` | `~/.vscode-server/data/agent-host/sdk-cache/` |

  Actual layout: `<...>/sdk-cache/<tool>/<version>/<arch>/`, where arch is e.g. `win32-x64` / `linux-x64`.
- Completion marker: after extraction + verification the script writes an empty `.complete` file under `<arch>/`,
  matching the agent-host native layout.

## Requirements

- Local: Python 3 (standard library only, zero dependencies); Windows 10 1803+ ships System32\tar.exe; network access to vscode-cdn.net
- Server push: passwordless SSH (key configured in ~/.ssh), tar present on the server

## Usage

```bash
python update_agent_sdk.py                       # update all channels locally (claude + codex)
python update_agent_sdk.py --server <ssh alias>   # local + SSH push to the server (both channels)
python update_agent_sdk.py --channel stable       # only the Stable channel
python update_agent_sdk.py --tool codex           # only codex
python update_agent_sdk.py --dry-run --server <alias>  # preview only
python update_agent_sdk.py --server-only --server <alias>  # server push only
```

| Argument | Description |
|---|---|
| `--server <SSH_ALIAS>` | SSH alias or `user@host` from ~/.ssh/config; pushes linux-x64 to the server when given |
| `--channel insiders\|stable\|both` | default `both` |
| `--tool claude\|codex\|all` | default `all` |
| `--branch <branch or tag>` | vscode repository branch/tag used as fallback when no installed `product.json` is found; default `main` (the Insiders line — for a stable install use `release/<x>`) |
| `--local-root <path>` | local cache root; by default derived per channel from `%APPDATA%` / `~` |
| `--dry-run` | report planned actions only, no download/extract/push |
| `--local-only` / `--server-only` | run one side only |

Behavior: per tool per channel, if the target version directory already has `.complete` it is skipped; **old versions are never deleted**.
**Only installed channels are updated**: if a channel's VS Code profile directory (locally `%APPDATA%\Code` / `Code - Insiders`,
on the server `~/.vscode-server` / `~/.vscode-server-insiders`) does not exist, the channel is treated as not installed, skipped,
and **no directories are created for it**.
**If the same version is already installed anywhere, it is not downloaded again**: when one local channel has it installed,
other missing channels copy the installed directory directly (`robocopy` / `cp -a`, no download); the same holds on the server —
if one server channel has it, the others reuse it via an in-server `cp -a`, so no linux package is downloaded.
Only when no copy exists anywhere does the script do "download → stream-verify the in-package version field → extract → atomic
rename → write `.complete`"; a package needed by multiple targets is downloaded only once (shared across local channels and
server channels alike). Any failure cleans up temp artifacts; tools are independent, one failing does not block the other.

## Scheduling examples

Windows (Task Scheduler, daily 09:00, with push):

```bat
schtasks /create /tn "agent-sdk-update" /tr "py -3 <your path>\vscode-agent-sdk-update\update_agent_sdk.py --server <ssh alias>" /sc daily /st 09:00
```

Linux server (when running directly on the server, it automatically uses the `~/.vscode-server[-insiders]` paths):

```cron
0 17 * * * cd ~/vscode-agent-sdk-update && python3 update_agent_sdk.py --tool all
```

## License

[MIT](./LICENSE)

## FAQ

- **`win-x64` gives 404**: the CDN arch name is `win32-x64` (the cache directory is `win32-x64` too); the script detects it automatically.
- **The version directory exists but the SDK files are stale**: check whether the version verification failed in the logs; use `--dry-run` to see which version it intends to install.
- **First download is slow**: claude ≈ 96MB, codex ≈ 133MB, roughly 1-3 minutes depending on your connection.
- **Verify connectivity with `ssh <alias>` before a server push**.
- **The cache contains a newer version (e.g. claude `0.3.258`) than my stable build expects**: older script versions took the version from the repo `main` branch, which is the Insiders line.
  The expected version for stable is the `agentSdks` entry of the installed `product.json` — just run the script again (or `--channel stable`); it installs the matching version next to the old
  ones. Old directories are never deleted and are harmless — the agent-host only looks up the version its own build pins.
- **`agentSdks` / `main` mismatch still possible?**: if no installed `product.json` is found (e.g. portable zip), stable falls back to `--branch` which defaults to `main`; the script prints a warning in that case — pass `--branch release/<x>` explicitly.
- **GitHub is unreachable but `api.github.com` works**: the script falls back from `raw.githubusercontent.com` to the GitHub contents API automatically.
