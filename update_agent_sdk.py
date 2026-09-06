#!/usr/bin/env python3
"""自动更新 VS Code (stable / Insiders) agent-host 的 SDK 缓存 (claude / codex)。

用途: 无需 Copilot 订阅即可使用本地 claude / codex——agent-host 日志出现
"[Claude] SDK not downloaded yet; deferring chat metadata until a session
triggers the download" 时永远不会触发下载, 本脚本提前把 SDK 缓存预置到位。

用法示例:
    python update_agent_sdk.py                        # 更新本机 stable+Insiders 全部
    python update_agent_sdk.py --server <ssh别名>      # 本机 + SSH 推送到 Linux 服务器
    python update_agent_sdk.py --channel insiders      # 只更新 Insiders
    python update_agent_sdk.py --tool codex --dry-run  # 只预览

版本来源(按通道取「实际」对应关系): 优先读该通道已安装 VS Code 自带
product.json 的 agentSdks——构建时写死的 claude/codex 版本与 CDN 模板,
即 agent-host 实际会请求的版本(如 stable 1.136.x 内置 claude 0.3.239)。
取不到时回退 microsoft/vscode 仓库 package.json 的 devDependencies:
stable → 对应 release/<x>, insiders → <branch>(默认 main)。
下载地址:  https://main.vscode-cdn.net/agent-sdk/<tool>/<version>/<arch>.tgz
"""

import argparse
import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# GitHub 仓库优先走 raw(raw.githubusercontent 部分地区不可达时回退 api,内容一致)
REPO_PKG_URL = "https://raw.githubusercontent.com/microsoft/vscode/{branch}/package.json"
API_PKG_URL = "https://api.github.com/repos/microsoft/vscode/contents/package.json?ref={branch}"
CDN_URL = "https://main.vscode-cdn.net/agent-sdk/{tool}/{version}/{arch}.tgz"

TOOL_NPM = {
    "claude": "@anthropic-ai/claude-agent-sdk",
    "codex": "@openai/codex",
}

# channel → 常见安装目录名(新版安装目录下还有一层 <commit>/resources/app)
INSTALL_DIR_NAMES = {
    "stable":   ("Microsoft VS Code", "Visual Studio Code"),
    "insiders": ("Microsoft VS Code Insiders", "Visual Studio Code - Insiders"),
}

# channel -> (Windows AppData 子目录, 服务器 ~ 下的子目录)
CHANNELS = {
    "insiders": ("Code - Insiders", ".vscode-server-insiders"),
    "stable": ("Code", ".vscode-server"),
}
CHANNEL_LABEL = {"insiders": "Insiders", "stable": "Stable"}

REMOTE_ARCH = "linux-x64"
USER_AGENT = "agent-sdk-updater/1.2"
CHUNK = 1 << 20  # 1 MiB


class ScriptError(Exception):
    """用户可读错误，打印后退出。"""


def err(msg):
    print(f"错误: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP

def http_download(url, dest: Path, timeout=900):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ScriptError(f"下载失败 {e.code}: {url}")
    except urllib.error.URLError as e:
        raise ScriptError(f"下载失败 {url}: {e.reason}")
    with resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)


def http_get_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ScriptError(f"获取失败 {e.code}: {url}")
    except urllib.error.URLError as e:
        raise ScriptError(f"获取失败 {url}: {e.reason}")


# ---------------------------------------------------------------- 版本

def fetch_versions(branch):
    """返回 {tool: 版本号}，来源 microsoft/vscode <branch> 的 package.json。

    raw.githubusercontent.com 在部分地区不可达,失败时自动回退
    api.github.com 的 contents 接口(base64 内容,同一文件)。
    """
    last = None
    for url in (REPO_PKG_URL.format(branch=branch), API_PKG_URL.format(branch=branch)):
        try:
            text = http_get_text(url)
            pkg = _parse_pkg_text(text)
        except ScriptError as e:
            last = e
            continue
        deps = {}
        for key in ("dependencies", "devDependencies"):
            deps.update(pkg.get(key) or {})
        versions = {}
        for tool, npm in TOOL_NPM.items():
            ver = deps.get(npm)
            if not ver:
                raise ScriptError(f"package.json 中未找到依赖 {npm}")
            versions[tool] = ver.lstrip("~^>=< ")  # 若写成 ^1.2.3 则取 1.2.3
        return versions
    raise ScriptError(f"获取 {branch} 的 package.json 失败: {last}")


def _parse_pkg_text(text):
    """raw 内容直接是 JSON;api.contents 返回 {"content": <base64>} 的 JSON。"""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScriptError(f"package.json 解析失败: {e}")
    if isinstance(obj, dict) and "content" in obj and "dependencies" not in obj:
        try:
            obj = json.loads(base64.b64decode(obj["content"]).decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            raise ScriptError(f"package.json 解码失败: {e}")
    return obj


def minor_branch(version):
    """"1.136.1" / "1.138.0-insider" → "release/1.136"; 解析不出返回 None。"""
    parts = version.split(".")
    if len(parts) >= 2 and all(p.isdigit() for p in parts[:2]):
        return f"{parts[0]}.{parts[1]}"
    return None


def branch_versions(memo, branch):
    """按分支取 {tool: 版本}(在 memo 里缓存,本机/服务器多个通道只请求一次)。"""
    if branch not in memo:
        memo[branch] = fetch_versions(branch)
    return memo[branch]


def _app_roots(channel):
    """该通道可能的安装根目录列表(不含 resources/app)。"""
    win_name, mac_name = INSTALL_DIR_NAMES[channel]
    roots = []
    if sys.platform == "win32":
        for env in ("LOCALAPPDATA", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base) / "Programs" / win_name if env == "LOCALAPPDATA"
                             else Path(base) / win_name)
    elif sys.platform == "darwin":
        roots.append(Path(f"/Applications/{mac_name}.app/Contents/Resources/app"))
    else:  # Linux 桌面/便携安装常见目录(服务器场景另走 ~/.vscode-server)
        for d in ("/usr/share/code", "/usr/lib/code", "/opt/visual-studio-code",
                  "/snap/code/current/usr/share/code"):
            roots.append(Path(d))
    return roots


def find_product_json(channel):
    """在该通道的安装目录里找 product.json,返回 (路径, dict) 或 (None, None)。

    新版安装布局为 <install>/<commit>/resources/app/product.json(commit 为
    12 位短哈希子目录),旧布局为 <install>/resources/app/product.json;
    同一目录可能残留多版(自动更新),取修改时间最新的一个。
    """
    cands = []
    for root in _app_roots(channel):
        cands.append(root / "resources" / "app" / "product.json")
        try:
            for d in root.iterdir():
                if d.is_dir():
                    cands.append(d / "resources" / "app" / "product.json")
        except OSError:
            pass
    best, best_mt = None, -1
    for cand in cands:
        try:
            mt = cand.stat().st_mtime
        except OSError:
            continue
        if mt > best_mt:
            best, best_mt = cand, mt
    if best is None:
        return None, None
    try:
        with open(best, encoding="utf-8") as f:
            return best, json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  警告: 读取 {best} 失败({e}),改走仓库分支回退", flush=True)
        return None, None


def resolve_versions(product, channel, tools, branch, memo):
    """按通道解析每个 tool 的目标版本,返回 {tool: (版本, 来源说明)}。

    优先级(即「实际」的版本对应关系):
      1. 已安装 VS Code product.json 内置 agentSdks(构建时写死,最准);
      2. stable 且已知安装版本 → vscode 仓库 release/<x>(与 1 内容一致);
      3. 回退参数 --branch(stable 默认 main 只适用于 Insiders,会提示)。
    """
    pver = (product or {}).get("version") or ""
    sdks = (product or {}).get("agentSdks") or {}
    result = {}
    for tool in tools:
        spec = sdks.get(tool)
        ver = spec.get("version") if isinstance(spec, dict) else spec
        if isinstance(ver, str) and ver:
            result[tool] = (ver, f"已装 VS Code {pver} 内置 product.json agentSdks")
            continue
        if channel == "stable" and pver:
            minor = minor_branch(pver)
            if minor:
                try:
                    rver = branch_versions(memo, f"release/{minor}").get(tool)
                except ScriptError:
                    rver = None
                if rver:
                    result[tool] = (rver, f"已装 VS Code {pver} 对应 vscode release/{minor}")
                    continue
        try:
            ver = branch_versions(memo, branch).get(tool)
        except ScriptError as e:
            raise ScriptError(f"[{tool}] 无可用版本来源: {e}")
        if not ver:
            raise ScriptError(f"[{tool}] vscode {branch} 的 package.json 中没有 {TOOL_NPM[tool]}")
        hint = " (警告: main 实际是 Insiders 行,若目标是 stable 请用 --branch release/<x>)" \
            if channel == "stable" and branch == "main" else ""
        result[tool] = (ver, f"vscode {branch} devDependencies{hint}")
    return result


def local_arch():
    system = platform.system()
    machine = platform.machine().lower()
    mapping = {
        ("Windows", ("amd64", "x86_64")): "win32-x64",
        ("Windows", ("arm64", "aarch64")): "win32-arm64",
        ("Linux", ("x86_64", "amd64")): "linux-x64",
        ("Linux", ("aarch64", "arm64")): "linux-arm64",
        ("Darwin", ("arm64", "aarch64")): "darwin-arm64",
        ("Darwin", ("x86_64", "amd64")): "darwin-x64",
    }
    for (sys_, machines), arch in mapping.items():
        if system == sys_ and machine in machines:
            return arch
    raise ScriptError(f"无法识别的平台: {system}/{machine}")


# ---------------------------------------------------------------- 路径(不暴露用户名)

def default_local_root(channel):
    """按 channel 推导本机缓存根目录: Windows 走 %APPDATA%, 其他平台走 ~。"""
    win_dir, _ = CHANNELS[channel]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise ScriptError("缺少环境变量 APPDATA, 无法推导缓存路径, 请用 --local-root 指定")
        return Path(appdata) / win_dir / "agent-host" / "sdk-cache"
    _, server_dir = CHANNELS[channel]
    return Path.home() / server_dir / "data" / "agent-host" / "sdk-cache"


def local_channel_exists(channel: str, local_root) -> bool:
    """通道是否已安装(VS Code profile 目录存在);显式 --local-root 时视为已确认。"""
    if local_root is not None:
        return True
    win_dir, server_dir = CHANNELS[channel]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return bool(appdata) and (Path(appdata) / win_dir).is_dir()
    return (Path.home() / server_dir).is_dir()


def remote_channel_exists(server: str, channel: str) -> bool:
    """服务器上该通道的 profile 目录(~/.vscode-server[-insiders])是否存在。"""
    return ssh_run(server, f'test -d "$HOME/{CHANNELS[channel][1]}" && echo YES || echo NO') == "YES"


# ---------------------------------------------------------------- 校验/解压(复用系统 tar,规避 Windows 长路径问题)

def find_tar():
    if sys.platform == "win32":
        # 优先 Win10+ 自带的 bsdtar(支持超长路径),Git Bash 的 "/usr/bin/tar" 无法被 CreateProcess 直接执行
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        if cand.is_file():
            return str(cand)
    t = shutil.which("tar")
    if t:
        return t
    raise ScriptError("未找到 tar 命令(Win10+ 自带 System32\\tar.exe,Linux 默认自带)")


def run_tar(args):
    tar = find_tar()
    try:
        p = subprocess.run([str(tar)] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise ScriptError(f"无法运行 tar: {e}")
    if p.returncode != 0:
        raise ScriptError(f"tar 失败({' '.join(args)}): {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def verify_tgz(tgz: Path, npm: str, expected: str):
    """流式校验 tgz:根为 node_modules/,且其中 <npm>/package.json 的 version 等于期望版本。"""
    listing = run_tar(["-tzf", str(tgz)])
    lines = [l.replace("\\", "/") for l in listing.splitlines()]
    root = lines[0].split("/", 1)[0] if lines else ""
    if root != "node_modules":
        raise ScriptError(f"{tgz.name} 归档根为 '{root}',期望 node_modules/")
    member = f"node_modules/{npm}/package.json"
    if not any(l.lstrip(".").lstrip("/") == member for l in lines):
        raise ScriptError(f"{tgz.name} 中缺少 {member}")
    out = run_tar(["-x", "-O", "-z", "-f", str(tgz), member])
    try:
        actual = json.loads(out).get("version")
    except json.JSONDecodeError:
        raise ScriptError(f"{tgz.name} 中 {member} 不是合法 JSON")
    if actual != expected:
        raise ScriptError(f"版本校验失败:{tgz.name} 内 {npm} 的 version={actual},期望 {expected}")


# ---------------------------------------------------------------- 下载缓存(同一版本包在多个 channel 间复用)

class TgzCache:
    """按 (tool, version, arch) 缓存已下载并校验过的 tgz, 避免 stable/insiders 重复下载。"""

    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix="agent-sdk-tgz-"))
        self._paths = {}

    def get(self, tool, version, arch):
        key = (tool, version, arch)
        if key not in self._paths:
            tgz = self._dir / f"{tool}-{version}-{arch}.tgz"
            url = CDN_URL.format(tool=tool, version=version, arch=arch)
            print(f"  下载 {url}", flush=True)
            http_download(url, tgz)
            verify_tgz(tgz, TOOL_NPM[tool], version)
            self._paths[key] = tgz
        return self._paths[key]

    def cleanup(self):
        shutil.rmtree(self._dir, ignore_errors=True)


# ---------------------------------------------------------------- 目录复制(已有安装直接复用,免下载)

def copy_tree(src: Path, dst: Path):
    """把已装好的 <arch> 目录整体复制到 dst(robocopy / cp -a),避免重复下载解压。"""
    if sys.platform == "win32":
        robocopy = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "robocopy.exe"
        args = [str(robocopy), str(src), str(dst), "/E", "/COPY:DAT", "/R:1", "/W:1",
                "/NFL", "/NDL", "/NJH", "/NJS", "/NP"]
    else:
        args = ["cp", "-a", str(src) + os.sep, str(dst)]
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise ScriptError(f"无法运行目录复制({args[0]}): {e}")
    if sys.platform == "win32":
        ok = p.returncode <= 7          # robocopy 0-7 均表示成功(1=有复制)
    else:
        ok = p.returncode == 0
    if not ok:
        raise ScriptError(f"目录复制失败 ({args[0]}): {p.stdout.strip() or p.stderr.strip()}")


def find_local_source(roots, tool: str, version: str, arch: str):
    """在其他通道的缓存根目录里找已完整安装的 (tool, version, arch),找到则免下载直接复用。"""
    for root in roots:
        cand = root / tool / version / arch
        if (cand / ".complete").is_file() and (cand / "node_modules").is_dir():
            return cand
    return None


# ---------------------------------------------------------------- 本机更新

def install_local(root: Path, tool: str, version: str, arch: str, cache: TgzCache, sources):
    """原子安装 <root>/<tool>/<version>/<arch>,已有副本则复制复用,否则走下载。"""
    target = root / tool / version / arch
    if target.exists():
        return "已是最新(目录已存在)"
    src = find_local_source(sources, tool, version, arch)
    version_dir = root / tool / version
    tmp = None
    try:
        version_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-update-", dir=str(version_dir)))
        ready = tmp / "ready"
        if src is not None:
            print(f"  复用 {src}(同版本已安装,免下载)", flush=True)
            ready.mkdir()
            copy_tree(src, ready)
        else:
            tgz = cache.get(tool, version, arch)
            extract_dir = tmp / "x"
            extract_dir.mkdir()
            print("  解压中…", flush=True)
            run_tar(["-xzf", str(tgz), "-C", str(extract_dir)])
            if not (extract_dir / "node_modules").is_dir():
                raise ScriptError(f"解压后缺少 {extract_dir}/node_modules/")
            os.replace(extract_dir, ready)

        os.replace(ready, target)          # 同盘改名,近乎原子
        (target / ".complete").touch()
        return "已更新(复用已有的同版本副本)" if src is not None else "已更新(解压校验完成,.complete 已写入)"
    except BaseException:
        if tmp is not None and not target.exists():  # 失败时清理,避免半成品留在缓存
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 远程推送

def ssh_run(server: str, cmd: str):
    try:
        p = subprocess.run(["ssh", server, cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise ScriptError(f"无法运行 ssh: {e}")
    if p.returncode != 0:
        raise ScriptError(f"ssh {server} 失败: {p.stderr.strip() or p.stdout.strip() or str(p.returncode)}")
    return p.stdout.strip()


def scp_push(server: str, src: Path, dest: str):
    try:
        p = subprocess.run(["scp", str(src), f"{server}:{dest}"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        raise ScriptError(f"无法运行 scp: {e}")
    if p.returncode != 0:
        raise ScriptError(f"scp 失败: {p.stderr.strip()}")


def remote_path(channel, tool, version):
    return f"$HOME/{CHANNELS[channel][1]}/data/agent-host/sdk-cache/{tool}/{version}"


def ssh_read_product(server: str, channel: str):
    """读服务器上该通道最新 bin/<commit>/product.json(取不到返回 None,不致命)。"""
    home_dir = CHANNELS[channel][1]
    try:
        out = ssh_run(server, f'find "$HOME/{home_dir}/bin" -maxdepth 2 -name product.json '
                              f'-printf "%T@ %p\\n" 2>/dev/null | sort -rn | head -1')
    except ScriptError:
        return None
    if not out.strip():
        return None
    path = out.split(" ", 1)[1] if " " in out else out
    try:
        text = ssh_run(server, f'cat -- "{path}"')
        return json.loads(text)
    except (ScriptError, json.JSONDecodeError):
        return None


def _server_cache_dir(tool: str, channel: str, version: str):
    return f"{remote_path(channel, tool, version)}/{REMOTE_ARCH}"


def _server_install_cmd(tool: str, channel: str, version: str, remote_tgz: str):
    """服务器端原子解压安装命令: mkdir → 解压到 .tmp → 校验 → 改名 → 写 .complete。"""
    cache_path = remote_path(channel, tool, version)
    tmp_dir = f"{cache_path}/{REMOTE_ARCH}.tmp"
    target = f"{cache_path}/{REMOTE_ARCH}"
    return (
        f'mkdir -p -- "{cache_path}" && '
        f'rm -rf -- "{tmp_dir}" && mkdir -p -- "{tmp_dir}" && '
        f'tar -xzf "{remote_tgz}" -C "{tmp_dir}" && '
        f'test -f "{tmp_dir}/node_modules/{TOOL_NPM[tool]}/package.json" && '
        f'mv -- "{tmp_dir}" "{target}" && '
        f'touch -- "{target}/.complete"'
    )


def _server_copy_cmd(tool: str, dst_channel: str, src_channel: str, version: str):
    """服务器内 cp -a 复用同版本已装副本(mv 前用文件存在性做校验)。"""
    cache_path = remote_path(dst_channel, tool, version)
    tmp_dir = f"{cache_path}/{REMOTE_ARCH}.tmp"
    target = f"{cache_path}/{REMOTE_ARCH}"
    return (
        f'mkdir -p -- "{cache_path}" && '
        f'rm -rf -- "{tmp_dir}" && '
        f'cp -a -- "{_server_cache_dir(tool, src_channel, version)}" "{tmp_dir}" && '
        f'test -f "{tmp_dir}/node_modules/{TOOL_NPM[tool]}/package.json" && '
        f'mv -- "{tmp_dir}" "{target}" && '
        f'touch -- "{target}/.complete"'
    )


def push_server(server: str, tool: str, specs: dict, cache: TgzCache):
    """按 channel 推送 linux-x64;specs = {channel: 期望版本}(只含已安装通道)。

    某通道已装好时跳过;同版本在服务器其他通道已装时直接服务器内 cp -a 复用,
    免下载 linux 包;都没有时才 scp tgz 过去逐个通道解压(同版本只传一次)。
    """
    statuses = {}
    missing = []
    for c, ver in specs.items():
        if ssh_run(server, f'if [ -f "{_server_cache_dir(tool, c, ver)}/.complete" ]; '
                           f'then echo YES; else echo NO; fi') == "YES":
            statuses[c] = "已是最新(服务器 .complete 已存在)"
        else:
            missing.append((c, ver))
    if not missing:
        return statuses

    by_ver = {}
    for c, ver in missing:
        by_ver.setdefault(ver, []).append(c)

    # 同版本已装在其他通道 → 服务器内直接复制
    for ver, group in by_ver.items():
        src = next((c for c, v in specs.items() if v == ver and statuses.get(c)), None)
        if src is None:
            continue
        for c in group:
            ssh_run(server, _server_copy_cmd(tool, c, src, ver))
            statuses[c] = "服务器已更新(复用另一通道同版本副本,免下载)"

    rest = [(c, ver) for ver, group in by_ver.items() for c in group
            if c not in statuses]
    if not rest:
        return statuses

    remote_tgzs = []
    try:
        by_ver = {}
        for c, ver in rest:
            by_ver.setdefault(ver, []).append(c)
        for ver, group in by_ver.items():
            tgz = cache.get(tool, ver, REMOTE_ARCH)
            remote_tgz = f"/tmp/agent-sdk-{tool}-{ver}.tgz"
            print(f"  scp → {server}:{remote_tgz}", flush=True)
            scp_push(server, tgz, remote_tgz)
            remote_tgzs.append(remote_tgz)
            for c in group:
                ssh_run(server, _server_install_cmd(tool, c, ver, remote_tgz))
                statuses[c] = f"服务器已更新({ver} linux-x64,.complete 已写入)"
    finally:
        for remote_tgz in remote_tgzs:
            try:
                ssh_run(server, f'rm -f -- "{remote_tgz}"')
            except ScriptError:
                pass
    return statuses


# ---------------------------------------------------------------- 主流程

def plan_local(root: Path, tool: str, version: str, arch: str, sources):
    target = root / tool / version / arch
    if target.exists():
        return "已是最新(跳过)"
    src = find_local_source(sources, tool, version, arch)
    if src is not None:
        return f"将复用 {src} 的已装副本(免下载)"
    return f"将下载 {version} 并安装到 {target}"


def plan_server(server: str, tool: str, specs: dict):
    """dry-run: 报告服务器每个通道将做什么;specs = {channel: 期望版本}。"""
    statuses = {}
    for c, ver in specs.items():
        installed = ssh_run(server, f'if [ -f "{_server_cache_dir(tool, c, ver)}/.complete" ]; '
                                    f'then echo YES; else echo NO; fi') == "YES"
        if installed:
            statuses[c] = "已是最新(跳过)"
        elif any(v == ver and ssh_run(server, f'if [ -f "{_server_cache_dir(tool, c2, v)}/.complete" ]; '
                                               f'then echo YES; else echo NO; fi') == "YES"
                 for c2, v in specs.items() if c2 != c):
            statuses[c] = "将复用服务器另一通道同版本副本(免下载)"
        else:
            statuses[c] = f"将下载并推送 {ver} linux-x64 到服务器"
    return statuses


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="更新 VS Code agent-host 的 SDK 缓存(claude/codex,stable+Insiders),"
                    "版本按通道取「实际」对应关系(已安装 VS Code product.json 的 agentSdks,"
                    "取不到时回退 vscode 仓库 release/<x> 或 main);"
                    "免 Copilot 订阅也可本地使用 claude/codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--server", metavar="SSH_ALIAS", help="SSH 别名或 user@host;指定后同步 linux-x64 到远程服务器")
    ap.add_argument("--tool", choices=("claude", "codex", "all"), default="all", help="默认 all")
    ap.add_argument("--channel", choices=("insiders", "stable", "both"), default="both",
                    help="更新哪些安装通道(默认 both)")
    ap.add_argument("--branch", default="main",
                    help="回退用的 vscode 仓库分支或 tag(默认 main,即 Insiders 行)。"
                         "找不到安装的 product.json 时: insiders 应保持 main,"
                         "stable 请指定 release/<x> 才能拿到对应版本")
    ap.add_argument("--local-root", type=Path, default=None,
                    help="本机缓存根目录(默认按 channel 从 %%APPDATA%% / ~ 自动推导:"
                         "Code 与 Code - Insiders 下的 agent-host/sdk-cache)")
    ap.add_argument("--dry-run", action="store_true", help="只报告打算做什么,不落盘")
    ap.add_argument("--local-only", action="store_true", help="只更新本机,跳过远程")
    ap.add_argument("--server-only", action="store_true", help="只推远程,忽略本机")
    args = ap.parse_args()

    if args.server_only and not args.server:
        ap.error("--server-only 需要同时指定 --server")
    if args.server_only and args.local_only:
        ap.error("--local-only 与 --server-only 不能同时指定")

    tools = ["claude", "codex"] if args.tool == "all" else [args.tool]
    channels = ["insiders", "stable"] if args.channel == "both" else [args.channel]
    do_local = not args.server_only
    do_remote = bool(args.server) and not args.local_only

    failures = 0
    cache = TgzCache()
    try:
        memo = {}                 # branch → {tool: 版本},避免同一分支重复请求

        # ---- 1. 每个通道先解析「实际」期望版本(本机读安装 product.json,服务器 SSH 读) ----
        local_specs = {}          # channel -> {tool: (版本, 来源)}
        if do_local:
            for channel in channels:
                label = CHANNEL_LABEL[channel]
                if not local_channel_exists(channel, args.local_root):
                    print(f"  本机[{label}]: 通道未安装(无 {CHANNELS[channel][0]} 目录),跳过", flush=True)
                    continue
                _, product = find_product_json(channel)
                try:
                    specs = resolve_versions(product, channel, tools, args.branch, memo)
                except ScriptError as e:
                    err(f"[本机 {label}] {e}")
                    failures += 1
                    continue
                local_specs[channel] = specs
                print(f"  本机[{label}] 期望: " + " · ".join(
                    f"{t} {v}({s})" for t, (v, s) in specs.items()), flush=True)

        server_specs = {}         # channel -> {tool: (版本, 来源)}
        if do_remote:
            for channel in channels:
                label = CHANNEL_LABEL[channel]
                if not remote_channel_exists(args.server, channel):
                    print(f"  服务器[{label}]: 通道未安装(服务器无 {CHANNELS[channel][1]} 目录),跳过", flush=True)
                    continue
                product = ssh_read_product(args.server, channel)
                try:
                    specs = resolve_versions(product, channel, tools, args.branch, memo)
                except ScriptError as e:
                    err(f"[服务器 {label}] {e}")
                    failures += 1
                    continue
                server_specs[channel] = specs
                print(f"  服务器[{label}] 期望: " + " · ".join(
                    f"{t} {v}({s})" for t, (v, s) in specs.items()), flush=True)

        # ---- 2. 逐个 tool 执行(每个通道用自己解析出的版本) ----
        if do_local and local_specs:
            arch = local_arch()
            sources = [args.local_root or default_local_root(c) for c in CHANNELS]
            for tool in tools:
                for channel, specs in local_specs.items():
                    ver, _ = specs[tool]
                    root = args.local_root or default_local_root(channel)
                    label = CHANNEL_LABEL[channel]
                    try:
                        status = plan_local(root, tool, ver, arch, sources) if args.dry_run \
                            else install_local(root, tool, ver, arch, cache, sources)
                    except ScriptError as e:
                        err(f"[本机 {label} {tool}] {e}")
                        failures += 1
                        continue
                    print(f"  本机[{label}]: {status}", flush=True)

        if do_remote and server_specs:
            for tool in tools:
                specs_tool = {c: specs[tool][0] for c, specs in server_specs.items()}
                try:
                    statuses = plan_server(args.server, tool, specs_tool) if args.dry_run \
                        else push_server(args.server, tool, specs_tool, cache)
                except ScriptError as e:
                    err(f"[服务器 {tool}] {e}")
                    failures += 1
                    continue
                for channel in server_specs:
                    print(f"  服务器[{CHANNEL_LABEL[channel]}]: {statuses[channel]}", flush=True)

        return 1 if failures else 0
    finally:
        cache.cleanup()


if __name__ == "__main__":
    sys.exit(main())
