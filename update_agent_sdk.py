#!/usr/bin/env python3
"""自动更新 VS Code (stable / Insiders) agent-host 的 SDK 缓存 (claude / codex)。

用途: 无需 Copilot 订阅即可使用本地 claude / codex——agent-host 日志出现
"[Claude] SDK not downloaded yet; deferring chat metadata until a session
triggers the download" 时永远不会触发下载, 本脚本提前把 SDK 缓存预置到位。

用法示例:
    python update_agent_sdk.py                        # 更新本机 stable+Insiders 全部
    python update_agent_sdk.py --server <ssh别名>      # 本机 + 推送到远程服务器(Linux/Windows)
    python update_agent_sdk.py --channel insiders      # 只更新 Insiders
    python update_agent_sdk.py --tool codex --dry-run  # 只预览

版本来源(按通道取「实际」对应关系): 优先读该通道已安装 VS Code 自带
product.json 的 agentSdks——构建时写死的 claude/codex 版本与 CDN 模板,
即 agent-host 实际会请求的版本(如 stable 1.136.x 内置 claude 0.3.239)。
取不到时回退 microsoft/vscode 仓库 package.json 的 devDependencies:
stable → 对应 release/<x>, insiders → <branch>(默认 main)。
下载地址:  https://main.vscode-cdn.net/agent-sdk/<tool>/<version>/<arch>.tgz
服务器架构由 SSH 探测(uname / cmd), Windows 服务器取 win32-x64 / win32-arm64,
Linux 取 linux-x64 / linux-arm64;可用 --remote-arch 覆盖。
"""

import argparse
import base64
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
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

USER_AGENT = "agent-sdk-updater/1.4"
CHUNK = 1 << 18            # 256 KiB 分块:小块读更容易发现「不发数据了」
DOWNLOAD_ATTEMPTS = 10     # CDN 传到一半断流是常见的,带 Range 续传慢慢下完
UPLOAD_ATTEMPTS = 3        # scp 偶发被远端残留文件/连接抖动打断
READ_TIMEOUT = 60          # 单次 socket 读超时:卡住时快速失败重试,而不是干等


class ScriptError(Exception):
    """用户可读错误，打印后退出。"""


def err(msg):
    """把用户可读错误打到 stderr。"""
    print(f"错误: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP

def _download_once(url, dest: Path):
    """下载或续传一次;没收到完整长度就抛 OSError(由调用方决定重试)。

    已下的部分保留在 dest,下一次用 Range 从断点接着下 —— CDN 传大包时
    中途不再发数据是常态,每次都从头来会永远下不完。
    """
    have = dest.stat().st_size if dest.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
        if getattr(resp, "status", 200) == 206:      # 服务端支持续传
            total, mode = have + int(resp.headers.get("Content-Length") or 0), "ab"
        else:                                        # 不支持 Range: 只能重下
            have, total, mode = 0, int(resp.headers.get("Content-Length") or 0), "wb"
        done, last_pct = have, (have * 100 // total if total else 0)
        with open(dest, mode) as f:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct >= last_pct + 10:         # 每 10% 报一次进度
                        last_pct = pct
                        print(f"  …{pct}%", flush=True)
                    if done >= total:
                        break
    if total and done > total:                       # 服务端多发就截断到声明长度
        with open(dest, "rb+") as f:
            f.truncate(total)
        done = total
    if total and done != total:
        raise OSError(f"连接中断(收到 {done}/{total} 字节)")


def http_size(url, timeout=30):
    """HEAD 拿 Content-Length(拿不到返回 0)。用于判断已下的是不是完整包。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length") or 0)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def http_download(url, dest: Path, attempts=DOWNLOAD_ATTEMPTS):
    """流式下载 url 到 dest,断了就从断点续传重试。

    单次读超时设短(READ_TIMEOUT)以便卡住时快速失败;每次重试都接着已下部分
    继续,所以链路一直抖也能慢慢下完;但连续两次一次字节都没多,说明对面不是
    抖动而是真不给,提前放弃。4xx 是永久的(比如 arch 名写错),不重试。
    """
    last, stalled = None, 0
    size = dest.stat().st_size if dest.exists() else 0
    for attempt in range(1, attempts + 1):
        try:
            _download_once(url, dest)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise ScriptError(f"下载失败 {e.code}: {url}") from e
            last = e
        except (urllib.error.URLError, OSError) as e:
            last = e
        grown = dest.stat().st_size if dest.exists() else 0
        stalled = 0 if grown > size else stalled + 1
        size = grown
        if stalled >= 2:
            break
        if attempt < attempts:
            print(f"  下载中断({last}),已下 {size / 1048576:.1f} MB,接着下({attempt})…", flush=True)
    raise ScriptError(f"下载失败 {url}: {last}")


def http_get_text(url, timeout=60):
    """取回 URL 文本(版本查询用,内容不大)。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ScriptError(f"获取失败 {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise ScriptError(f"获取失败 {url}: {e.reason}") from e


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
        raise ScriptError(f"package.json 解析失败: {e}") from e
    if isinstance(obj, dict) and "content" in obj and "dependencies" not in obj:
        try:
            obj = json.loads(base64.b64decode(obj["content"]).decode("utf-8"))
        except (json.JSONDecodeError, ValueError) as e:
            raise ScriptError(f"package.json 解码失败: {e}") from e
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
            raise ScriptError(f"[{tool}] 无可用版本来源: {e}") from e
        if not ver:
            raise ScriptError(f"[{tool}] vscode {branch} 的 package.json 中没有 {TOOL_NPM[tool]}")
        hint = " (警告: main 实际是 Insiders 行,若目标是 stable 请用 --branch release/<x>)" \
            if channel == "stable" and branch == "main" else ""
        result[tool] = (ver, f"vscode {branch} devDependencies{hint}")
    return result


# 机器名 → CDN/缓存目录用的 arch 名(CDN 上只有 win32-x64, 没有 win-x64)
ARCH_BY_MACHINE = {
    "windows": {"amd64": "win32-x64", "x86_64": "win32-x64",
                "arm64": "win32-arm64", "aarch64": "win32-arm64"},
    "linux": {"x86_64": "linux-x64", "amd64": "linux-x64",
              "aarch64": "linux-arm64", "arm64": "linux-arm64"},
    "darwin": {"x86_64": "darwin-x64", "amd64": "darwin-x64",
               "arm64": "darwin-arm64", "aarch64": "darwin-arm64"},
}


def arch_of(os_name, machine):
    """(系统, 机器名) → arch; 不认识的组合返回 None。"""
    return ARCH_BY_MACHINE.get(os_name or "", {}).get((machine or "").lower())


def local_arch():
    """本机 arch(与远端探测共用 ARCH_BY_MACHINE, 保持两边口径一致)。"""
    os_name = {"win32": "windows", "darwin": "darwin", "linux": "linux"}.get(sys.platform)
    system, machine = platform.system(), platform.machine()
    arch = arch_of(os_name, machine)
    if not arch:
        raise ScriptError(f"无法识别的平台: {system}/{machine}")
    return arch


# ---------------------------------------------------------------- 远程平台探测


class Remote:
    """SSH 服务器探测结果。

    Windows 一律走 PowerShell(默认 shell 是 cmd / PowerShell / Git Bash 都能调起
    powershell.exe), 用 -EncodedCommand(base64)执行, 绕开多层引号解析;
    其余系统走 POSIX shell 命令($HOME + find/cat/tar/mkdir/mv/touch)。
    """

    def __init__(self, os_name, arch):
        """os_name: linux/darwin/windows;arch: CDN 与缓存目录用的架构名。"""
        self.os = os_name              # linux / darwin / windows
        self.arch = arch               # linux-x64 / win32-x64 / ...

    @property
    def is_windows(self):
        """Windows 服务器(决定走 PowerShell 还是 POSIX 命令)。"""
        return self.os == "windows"

    def __str__(self):
        """日志里显示的简短描述。"""
        return f"{self.os}/{self.arch}"


def _ssh_try(server, cmd):
    """探测用: 返回 (stdout, 错误文本), 不抛异常。"""
    try:
        return ssh_run(server, cmd), ""
    except ScriptError as e:
        return "", str(e)


def _parse_uname(out):
    """解析 `uname -s; uname -m` 输出, 返回 (系统, 架构);认不出返回 (None, "")。

    按关键词而不是按行取值: Windows 上常见的 coreutils uname 不认参数(实测
    10.0.12.44 上 `uname -s`/`uname -m` 都输出 `Windows_NT 10.0`),Linux 上也可能
    被换成打印整行的实现。
    """
    text = (out or "").lower()
    if any(k in text for k in ("mingw", "msys", "cygwin", "ucrt", "windows")):
        os_name = "windows"
    elif "linux" in text:
        os_name = "linux"
    elif "darwin" in text:
        os_name = "darwin"
    else:
        return None, ""
    machine = next((t for t in re.split(r"[\s/()]+", text) if t in ARCH_BY_MACHINE[os_name]), "")
    return os_name, machine


def _parse_windows_probe(out):
    """解析 `cmd /c echo %OS% %PROCESSOR_ARCHITECTURE%` 输出, 非 Windows 返回 (None, "")。"""
    parts = (out or "").split()
    if parts and parts[0].upper() == "WINDOWS_NT":
        return "windows", (parts[1].lower() if len(parts) > 1 else "")
    return None, ""


def detect_remote(server, arch_override=""):
    """探测服务器系统与架构。

    先 uname(Linux/macOS/Git Bash 一次到位);认不出系统、或 Windows 上拿不到架构时,
    再用 `cmd /c echo %OS% %PROCESSOR_ARCHITECTURE%` 兜底 —— 这条在 Windows 的
    cmd / PowerShell 默认 shell 下都成立。Windows 必须按 win32-x64/win32-arm64
    下载(CDN 上没有 win-x64)。
    """
    out, uname_err = _ssh_try(server, "uname -s; uname -m")
    os_name, machine = _parse_uname(out)
    if os_name is None or (os_name == "windows" and not arch_of(os_name, machine)):
        cmd_out, cmd_err = _ssh_try(server, "cmd /c echo %OS% %PROCESSOR_ARCHITECTURE%")
        cmd_os, cmd_machine = _parse_windows_probe(cmd_out)
        if cmd_os:
            os_name, machine = cmd_os, cmd_machine
        elif os_name is None:
            raise ScriptError("无法探测服务器平台: "
                              + (uname_err or cmd_err or "uname / cmd 均无有效输出")
                              + "(可用 --remote-arch 手动指定架构)")
    arch = arch_override or arch_of(os_name, machine)
    if not arch:
        raise ScriptError(f"无法识别的服务器架构: {os_name}/{machine}(可用 --remote-arch 指定)")
    if arch_override:
        # 手动指定架构时以架构为准推断系统: 说 win32-x64 就是要装 Windows 包,
        # 命令方言也得跟着走 PowerShell, 不能还按探测到的 Linux 发 POSIX 命令
        os_name = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(
            arch.split("-")[0], os_name)
    return Remote(os_name, arch)


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


# ---------------------------------------------------------------- 校验/解压(复用系统 tar,规避 Windows 长路径问题)

def find_tar():
    """定位 tar:Windows 优先用自带 bsdtar(支持长路径),其余平台走 PATH。"""
    if sys.platform == "win32":
        # 优先 Win10+ 自带的 bsdtar(支持超长路径);
        # Git Bash 的 "/usr/bin/tar" 无法被 CreateProcess 直接执行
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
        if cand.is_file():
            return str(cand)
    t = shutil.which("tar")
    if t:
        return t
    raise ScriptError("未找到 tar 命令(Win10+ 自带 System32\\tar.exe,Linux 默认自带)")


def run_tar(args):
    """执行 tar 并返回 stdout;非 0 退出转成用户可读错误。"""
    tar = find_tar()
    try:
        p = subprocess.run([str(tar)] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise ScriptError(f"无法运行 tar: {e}") from e
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
    except json.JSONDecodeError as e:
        raise ScriptError(f"{tgz.name} 中 {member} 不是合法 JSON") from e
    if actual != expected:
        raise ScriptError(f"版本校验失败:{tgz.name} 内 {npm} 的 version={actual},期望 {expected}")


# ---------------------------------------------------------------- 下载缓存(同一版本包在多个 channel 间复用)

class TgzCache:
    """按 (tool, version, arch) 缓存下载过的 tgz,同一版本包只下一次、跨次运行也复用。

    目录放在系统临时目录且退出时不删:这个 CDN 经常传到一半断流,留着已下部分
    下次能接着下(http_download 用 Range 续传);已下完的包也省得再下一遍。
    过期文件(默认 30 天)在启动时清掉,不至于一直堆着。
    """

    TTL_DAYS = 30

    def __init__(self, root=None):
        """缓存目录默认 <系统临时目录>/agent-sdk-tgz-cache。"""
        self._dir = Path(root) if root else Path(tempfile.gettempdir()) / "agent-sdk-tgz-cache"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._paths = {}
        self._prune()

    def _prune(self):
        """删掉过期的包(系统临时目录也可能被系统自己清理,所以删除失败无所谓)。"""
        cutoff = time.time() - self.TTL_DAYS * 86400
        for old in self._dir.glob("*.tgz"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def _state(self, tgz: Path, url: str, tool: str, version: str):
        """已存在文件的处置: done(可复用)/ resume(留着续传)/ discard(删掉重下)。"""
        if tgz.stat().st_size < http_size(url):
            return "resume"                       # 比远端小 → 上次没下完,接着下
        try:
            verify_tgz(tgz, TOOL_NPM[tool], version)
            return "done"
        except ScriptError:
            return "discard"

    def get(self, tool, version, arch):
        """取该组合的本地 tgz;没有就下载并校验,同一组合只下一次。"""
        key = (tool, version, arch)
        if key in self._paths:
            return self._paths[key]
        tgz = self._dir / f"{tool}-{version}-{arch}.tgz"
        url = CDN_URL.format(tool=tool, version=version, arch=arch)
        state = self._state(tgz, url, tool, version) if tgz.is_file() else "discard"
        if state == "done":
            print(f"  复用已下载的 {tgz.name}", flush=True)
        else:
            if state == "discard":
                tgz.unlink(missing_ok=True)
            else:
                print(f"  续传上次没下完的 {tgz.name}(已下 {tgz.stat().st_size / 1048576:.1f} MB)", flush=True)
            print(f"  下载 {url}", flush=True)
            http_download(url, tgz)
        verify_tgz(tgz, TOOL_NPM[tool], version)
        self._paths[key] = tgz
        return tgz

    def cleanup(self):
        """不需要清理: 缓存跨次运行复用(交给 TTL 与系统临时目录策略回收)。"""


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
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", check=False)
    except OSError as e:
        raise ScriptError(f"无法运行目录复制({args[0]}): {e}") from e
    if sys.platform == "win32":
        ok = p.returncode <= 7          # robocopy 0-7 均表示成功(1=有复制)
    else:
        ok = p.returncode == 0
    if not ok:
        raise ScriptError(f"目录复制失败 ({args[0]}): {p.stdout.strip() or p.stderr.strip()}")


def find_local_source(roots, tool: str, version: str, arch: str):
    """在其他通道的缓存根目录里找已完整安装的 (tool, version, arch)。

    找到就免下载直接复用(.complete 与 node_modules 都在才算完整)。
    """
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
        if src is not None:
            return "已更新(复用已有的同版本副本)"
        return "已更新(解压校验完成,.complete 已写入)"
    except BaseException:
        if tmp is not None and not target.exists():  # 失败时清理,避免半成品留在缓存
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 远程推送

def ssh_run(server: str, cmd: str):
    """执行远端命令(默认 shell 会先解析一遍命令串,所以命令要兼容 cmd/PowerShell/sh)。"""
    try:
        p = subprocess.run(["ssh", server, cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise ScriptError(f"无法运行 ssh: {e}") from e
    if p.returncode != 0:
        raise ScriptError(f"ssh {server} 失败: {p.stderr.strip() or p.stdout.strip() or str(p.returncode)}")
    return p.stdout.strip()


def _scp_once(server: str, local: Path, name: str):
    """scp 一次;失败转成 ScriptError。"""
    try:
        p = subprocess.run(["scp", str(local), f"{server}:{name}"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", check=False)
    except OSError as e:
        raise ScriptError(f"无法运行 scp: {e}") from e
    if p.returncode != 0:
        raise ScriptError(f"scp 上传 {name} 失败: {p.stderr.strip() or p.stdout.strip()}")


def scp_push(server: str, local: Path, name: str, attempts=UPLOAD_ATTEMPTS):
    """用 scp 把文件传到服务器用户目录下,失败重试。

    目标写成相对文件名: scp/sftp 相对各自家目录解析(POSIX 是 $HOME,Windows 是
    %USERPROFILE%),既避开 /tmp 与盘符差异,也是实测在 Windows 服务器上稳定的
    方式(把 tgz 灌进 ssh 的 stdin 会卡住)。远端若还留着同名文件、被残留进程
    占着,scp 会报 `dest open ... Failure` —— 调用方会先删掉它,这里再兜底重试。
    """
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            _scp_once(server, local, name)
            return
        except ScriptError as e:
            last = str(e).strip().splitlines()[-1] if str(e).strip() else "未知错误"
            if attempt < attempts:
                print(f"  上传失败({last[:70]}),重试 {attempt}/{attempts - 1}…", flush=True)
                time.sleep(attempt * 2)
    raise ScriptError(f"scp 上传 {name} 失败: {last}")


def ssh_run_ps(server: str, body: str):
    """在服务器上执行 PowerShell 脚本(base64(UTF-16LE) + -EncodedCommand)。

    直接 `ssh server powershell -Command "…"` 会先被默认 shell(cmd/PowerShell)
    解析一遍, 引号/分号/$ 都可能被吃掉;-EncodedCommand 的载荷只有 base64 字符,
    cmd / PowerShell / Git Bash 当默认 shell 都安全, 出错时脚本内 exit 1。
    """
    script = ("try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }\n"
              "$ErrorActionPreference = 'Stop'\n"
              "try {\n" + body + "\n}\n"
              "catch {\n  [Console]::Error.WriteLine($_.Exception.Message)\n  exit 1\n}\n"
              "exit 0")
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ssh_run(server, f"powershell -NoProfile -EncodedCommand {enc}")


def _ps_lit(text):
    """PowerShell 单引号字符串字面量(内部单引号翻倍)。"""
    return "'" + str(text).replace("'", "''") + "'"


def _win(rel: str):
    """PowerShell 表达式: 服务器用户目录 + 相对路径(正斜杠 Windows 也认)。"""
    return f"(Join-Path $env:USERPROFILE {_ps_lit(rel)})"


def _posix(rel: str):
    """POSIX shell 表达式: 服务器用户目录 + 相对路径。"""
    return f'"$HOME/{rel}"'


def _sdk_rel(tool: str, channel: str, version: str, remote: Remote):
    """服务器缓存目录相对用户目录的路径(<server-dir>/data/agent-host/sdk-cache/…)。"""
    return f"{CHANNELS[channel][1]}/data/agent-host/sdk-cache/{tool}/{version}/{remote.arch}"


def _remote_test(server: str, remote: Remote, posix_cond: str, ps_expr: str) -> bool:
    """在服务器上判断「路径存在」类条件, 返回布尔(posix_cond 是 [ -f x ] 之类)。"""
    if not remote.is_windows:
        return ssh_run(server, f"if {posix_cond}; then echo YES; else echo NO; fi") == "YES"
    return ssh_run_ps(server, "if (Test-Path -LiteralPath " + ps_expr + ") { 'YES' } else { 'NO' }") == "YES"


def remote_has_marker(server: str, remote: Remote, tool: str, channel: str, version: str):
    """服务器上 <tool>/<version>/<arch>/.complete 是否已存在。"""
    rel = _sdk_rel(tool, channel, version, remote) + "/.complete"
    return _remote_test(server, remote, f"[ -f {_posix(rel)} ]", _win(rel))


def remote_channel_exists(server: str, remote: Remote, channel: str) -> bool:
    """服务器上该通道的 profile 目录(~/.vscode-server[-insiders])是否存在。"""
    rel = CHANNELS[channel][1]
    return _remote_test(server, remote, f"[ -d {_posix(rel)} ]", _win(rel))


def ssh_find_products(server: str, channel: str):
    """服务器上该通道所有 server/product.json 的路径(按 mtime 从新到旧)。

    新版布局: ~/.vscode-server[-insiders]/cli/servers/<Quality>-<commit>/server/product.json;
    旧版布局: .../bin/<commit>/product.json。排除 .staging 未完成下载与 extensions。
    """
    home_dir = CHANNELS[channel][1]
    find_cmd = (
        f'find "$HOME/{home_dir}" \\( -name "*.staging" -o -name extensions \\) -prune '
        f'-o -name product.json -print 2>/dev/null'
    )
    try:
        paths = [p for p in ssh_run(server, find_cmd).splitlines() if p.strip()]
    except ScriptError:
        return []
    if not paths or len(paths) == 1:
        return paths
    quoted = " ".join(shlex.quote(p) for p in paths)
    try:
        out = ssh_run(server, f'for f in {quoted}; do echo "$(stat -c %Y "$f" 2>/dev/null '
                              f'|| stat -f %m "$f" 2>/dev/null) $f"; done | sort -rn | head -1')
    except ScriptError:
        return paths
    if " " in out:
        mtime, _, path = out.partition(" ")
        if mtime.isdigit():
            return [path.strip()]
    return paths


def _ps_find_products(channel: str):
    """PowerShell 版 product.json 探测: 只扫已知的 cli/servers、bin 布局(避开 extensions)。"""
    return ("$root = Join-Path $env:USERPROFILE " + _ps_lit(CHANNELS[channel][1]) + "\n"
            "$cands = @()\n"
            "$cli = Join-Path $root 'cli/servers'\n"
            "if (Test-Path -LiteralPath $cli) {\n"
            "  $cands += Get-ChildItem -LiteralPath $cli -Directory | ForEach-Object {\n"
            "    Join-Path $_.FullName 'server/product.json' }\n"
            "}\n"
            "$bin = Join-Path $root 'bin'\n"
            "if (Test-Path -LiteralPath $bin) {\n"
            "  $cands += Get-ChildItem -LiteralPath $bin -Directory | ForEach-Object {\n"
            "    Join-Path $_.FullName 'product.json' }\n"
            "}\n"
            "$newest = $cands | Where-Object { $_ -notmatch '\\.staging' -and (Test-Path -LiteralPath $_) } |\n"
            "  ForEach-Object { Get-Item -LiteralPath $_ } | Sort-Object LastWriteTime -Descending |\n"
            "  Select-Object -First 1\n"
            "if ($newest) { [Convert]::ToBase64String([IO.File]::ReadAllBytes($newest.FullName)) }")


def _parse_product_text(text):
    """product.json 文本 → dict(容忍 BOM),解析失败返回 None。"""
    try:
        return json.loads(text.lstrip("﻿"))
    except json.JSONDecodeError:
        return None


def remote_read_product(server: str, remote: Remote, channel: str):
    """读服务器上该通道最新的 product.json(取不到返回 None,不致命)。"""
    if not remote.is_windows:
        for path in ssh_find_products(server, channel):
            try:
                text = ssh_run(server, f'cat -- "{path}"')
            except ScriptError:
                continue
            product = _parse_product_text(text)
            if product is not None:
                return product
        return None
    try:
        out = ssh_run_ps(server, _ps_find_products(channel))
    except ScriptError:
        return None
    if not out:
        return None
    try:
        # 传 base64 而不是文本, 避免服务器控制台编码非 UTF-8 时把 JSON 弄坏
        return _parse_product_text(base64.b64decode(out).decode("utf-8-sig"))
    except ValueError:
        return None


def _posix_parent(rel: str):
    """arch 目录的父目录(version 目录)的 POSIX 表达式。"""
    return _posix(rel.rsplit("/", 1)[0])


def _posix_install_cmd(tool: str, rel: str, tgz_expr: str):
    """POSIX 端原子解压安装: mkdir → 解压到 .tmp → 校验 → 改名 → 写 .complete。"""
    tmp_dir = _posix(rel + ".tmp")
    target = _posix(rel)
    return (
        f'mkdir -p -- {_posix_parent(rel)} && '
        f'rm -rf -- {tmp_dir} && mkdir -p -- {tmp_dir} && '
        f'tar -xzf {tgz_expr} -C {tmp_dir} && '
        f'test -f {tmp_dir}/node_modules/{TOOL_NPM[tool]}/package.json && '
        f'mv -- {tmp_dir} {target} && '
        f'touch -- {target}/.complete'
    )


def _posix_copy_cmd(tool: str, rel: str, src_rel: str):
    """POSIX 端 cp -a 复用同版本已装副本(mv 前用文件存在性做校验)。"""
    tmp_dir = _posix(rel + ".tmp")
    target = _posix(rel)
    return (
        f'mkdir -p -- {_posix_parent(rel)} && '
        f'rm -rf -- {tmp_dir} && '
        f'cp -a -- {_posix(src_rel)} {tmp_dir} && '
        f'test -f {tmp_dir}/node_modules/{TOOL_NPM[tool]}/package.json && '
        f'mv -- {tmp_dir} {target} && '
        f'touch -- {target}/.complete'
    )


def _ps_prepare(rel: str):
    """PowerShell 版公共前置: 建父目录 + 清理旧的 .tmp,返回脚本片段。"""
    return (
        "New-Item -ItemType Directory -Force -Path (Split-Path -Parent " + _win(rel) + ") | Out-Null\n"
        "$tmp = " + _win(rel + ".tmp") + "\n"
        "$target = " + _win(rel) + "\n"
        "if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }\n")


def _ps_finish(tool: str, action: str):
    """PowerShell 版公共收尾: 校验 node_modules → 改名 → 写 .complete。"""
    npm = TOOL_NPM[tool]
    return (
        "if (-not (Test-Path -LiteralPath (Join-Path $tmp 'node_modules/" + npm + "/package.json'))) "
        "{ throw '" + action + "结果缺少 node_modules/" + npm + "/package.json' }\n"
        # 用 .NET 而不是 Move-Item: 短路径(8.3)下 provider 会让 Move-Item 报“对象不存在”
        "[IO.Directory]::Move($tmp, $target)\n"
        "New-Item -ItemType File -Force -Path (Join-Path $target '.complete') | Out-Null")


def _ps_install_script(tool: str, rel: str, tgz_name: str):
    """PowerShell 版安装: 解压 tgz 到 <arch>.tmp → 校验 → 改名 → 写 .complete。"""
    return ("$tar = Join-Path $env:SystemRoot 'System32/tar.exe'\n"
            "if (-not (Test-Path -LiteralPath $tar)) "
            "{ throw '服务器缺少 System32\\tar.exe(需 Windows 10 1803+ / Server 2019+)' }\n"
            + _ps_prepare(rel) +
            "New-Item -ItemType Directory -Force -Path $tmp | Out-Null\n"
            "& $tar -xzf " + _win(tgz_name) + " -C $tmp\n"
            "if ($LASTEXITCODE -ne 0) { throw 'tar 解压失败 (exit ' + $LASTEXITCODE + ')' }\n"
            + _ps_finish(tool, "解压"))


def _ps_copy_script(tool: str, rel: str, src_rel: str):
    """PowerShell 版复用: 服务器内复制同版本已装副本到另一个通道。"""
    return ("$src = " + _win(src_rel) + "\n"
            "if (-not (Test-Path -LiteralPath (Join-Path $src 'node_modules/" + TOOL_NPM[tool] + "/package.json'))) "
            "{ throw '源副本不完整: ' + $src }\n"
            + _ps_prepare(rel) +
            "Copy-Item -LiteralPath $src -Destination $tmp -Recurse -Force\n"
            + _ps_finish(tool, "复制"))


def remote_install(server: str, remote: Remote, tool: str, channel: str, version: str, tgz_name: str):
    """在服务器上安装 <tool>/<version>/<arch>(tgz_name 需已传到服务器用户目录)。"""
    rel = _sdk_rel(tool, channel, version, remote)
    if not remote.is_windows:
        ssh_run(server, _posix_install_cmd(tool, rel, _posix(tgz_name)))
    else:
        ssh_run_ps(server, _ps_install_script(tool, rel, tgz_name))


def remote_copy(server: str, remote: Remote, tool: str, dst_channel: str, src_channel: str, version: str):
    """服务器内把 src_channel 已装好的同版本副本复制到 dst_channel。"""
    rel = _sdk_rel(tool, dst_channel, version, remote)
    src_rel = _sdk_rel(tool, src_channel, version, remote)
    if not remote.is_windows:
        ssh_run(server, _posix_copy_cmd(tool, rel, src_rel))
    else:
        ssh_run_ps(server, _ps_copy_script(tool, rel, src_rel))


def remote_put(server: str, local: Path, name: str):
    """把 tgz 传到服务器用户目录下(解压脚本里用 $HOME/%USERPROFILE% 引用)。"""
    scp_push(server, local, name)


def upload_tgz(server: str, remote: Remote, tgz: Path, tool: str, ver: str):
    """上传 tgz 并返回服务器上的文件名;首选名字不可用就换个带 pid 的名字。

    远端可能留着上次中断的残留文件,若还被某个进程占着(mv/删除都会失败),
    scp 就写不进去 —— 换个名字绕过它,别让一次残留拖死整轮更新。
    """
    last = ""
    names = (f"agent-sdk-{tool}-{ver}.tgz", f"agent-sdk-{tool}-{ver}-{os.getpid()}.tgz")
    for name in names:
        try:
            remote_rm(server, remote, name)          # 先清掉同名残留(失败不致命)
        except ScriptError:
            pass
        print(f"  上传 {name}({tgz.stat().st_size / 1048576:.1f} MB)→ {server}:~/{name}", flush=True)
        try:
            remote_put(server, tgz, name)
            return name
        except ScriptError as e:
            last = str(e)
            print(f"  警告: {last.splitlines()[-1][:90]}", flush=True)
    raise ScriptError(f"上传 {tool} {ver} 失败: {last}")


def remote_rm(server: str, remote: Remote, name: str):
    """删除上传的临时 tgz(不存在不算错)。"""
    if not remote.is_windows:
        ssh_run(server, f'rm -f -- "$HOME/{name}"')
    else:
        ssh_run_ps(server, "Remove-Item -LiteralPath " + _win(name)
                   + " -Force -ErrorAction SilentlyContinue")


def push_server(server: str, tool: str, specs: dict, cache: TgzCache, remote: Remote):
    """按 channel 推送 remote.arch 包;specs = {channel: 期望版本}(只含已安装通道)。

    某通道已装好时跳过;同版本在服务器其他通道已装时直接服务器内复制,免下载;
    都没有时才把 tgz 传到服务器逐个通道解压(同版本只传一次,结束后删除)。
    """
    statuses = {}
    missing = []
    for c, ver in specs.items():
        if remote_has_marker(server, remote, tool, c, ver):
            statuses[c] = "已是最新(服务器 .complete 已存在)"
        else:
            missing.append((c, ver))
    if not missing:
        return statuses

    by_ver = {}
    for c, ver in missing:
        by_ver.setdefault(ver, []).append(c)

    # 同版本已装在其他通道 → 服务器内直接复制(复制不成则回落到下载安装)
    for ver, group in by_ver.items():
        src = next((c for c, v in specs.items() if v == ver and statuses.get(c)), None)
        if src is None:
            continue
        for c in group:
            try:
                remote_copy(server, remote, tool, c, src, ver)
            except ScriptError as e:
                print(f"  警告: 服务器内复制 {src}→{c} 失败({e}),改为下载", flush=True)
                continue
            statuses[c] = "服务器已更新(复用另一通道同版本副本,免下载)"

    rest = [(c, ver) for ver, group in by_ver.items() for c in group
            if c not in statuses]
    if not rest:
        return statuses

    by_ver = {}
    for c, ver in rest:
        by_ver.setdefault(ver, []).append(c)
    uploaded = []
    try:
        for ver, group in by_ver.items():
            tgz = cache.get(tool, ver, remote.arch)
            name = f"agent-sdk-{tool}-{ver}.tgz"
            name = upload_tgz(server, remote, tgz, tool, ver)
            uploaded.append(name)
            for c in group:
                remote_install(server, remote, tool, c, ver, name)
                statuses[c] = f"服务器已更新({ver} {remote.arch},.complete 已写入)"
    finally:
        for name in uploaded:
            try:
                remote_rm(server, remote, name)
            except ScriptError:
                pass
    return statuses


# ---------------------------------------------------------------- 主流程

def plan_local(root: Path, tool: str, version: str, arch: str, sources):
    """dry-run: 描述本机这一步会做什么。"""
    target = root / tool / version / arch
    if target.exists():
        return "已是最新(跳过)"
    src = find_local_source(sources, tool, version, arch)
    if src is not None:
        return f"将复用 {src} 的已装副本(免下载)"
    return f"将下载 {version} 并安装到 {target}"


def plan_server(server: str, tool: str, specs: dict, remote: Remote):
    """dry-run: 报告服务器每个通道将做什么;specs = {channel: 期望版本}。"""
    statuses = {}
    for c, ver in specs.items():
        if remote_has_marker(server, remote, tool, c, ver):
            statuses[c] = "已是最新(跳过)"
        elif any(v == ver and remote_has_marker(server, remote, tool, c2, v)
                 for c2, v in specs.items() if c2 != c):
            statuses[c] = "将复用服务器另一通道同版本副本(免下载)"
        else:
            statuses[c] = f"将下载并推送 {ver} {remote.arch} 到服务器"
    return statuses


def build_parser():
    """命令行参数定义。"""
    ap = argparse.ArgumentParser(
        description="更新 VS Code agent-host 的 SDK 缓存(claude/codex,stable+Insiders),"
                    "版本按通道取「实际」对应关系(已安装 VS Code product.json 的 agentSdks,"
                    "取不到时回退 vscode 仓库 release/<x> 或 main);"
                    "免 Copilot 订阅也可本地使用 claude/codex",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--server", metavar="SSH_ALIAS",
                    help="SSH 别名或 user@host;指定后同步 SDK 包到远程服务器"
                         "(架构自动探测: linux-x64 / win32-x64 / …)")
    ap.add_argument("--remote-arch", default="",
                    help="强制指定服务器架构,跳过自动探测(如 win32-x64、linux-arm64)")
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
    return ap


def collect_specs(side, channels, tools, branch, memo, is_installed, read_product, missing_hint):
    """解析一侧(本机/服务器)各通道「实际」期望的版本。

    is_installed(channel) 判断通道是否装了;read_product(channel) 取该通道的
    product.json(取不到给 None);missing_hint(channel) 用于未安装时的提示文案。
    返回 ({channel: {tool: (版本, 来源)}}, 失败数)。
    """
    specs_by_channel, failures = {}, 0
    for channel in channels:
        label = CHANNEL_LABEL[channel]
        if not is_installed(channel):
            print(f"  {side}[{label}]: 通道未安装(无 {missing_hint(channel)} 目录),跳过", flush=True)
            continue
        try:
            specs = resolve_versions(read_product(channel), channel, tools, branch, memo)
        except ScriptError as e:
            err(f"[{side} {label}] {e}")
            failures += 1
            continue
        specs_by_channel[channel] = specs
        print(f"  {side}[{label}] 期望: " + " · ".join(
            f"{t} {v}({s})" for t, (v, s) in specs.items()), flush=True)
    return specs_by_channel, failures


def update_local(specs_by_channel, tools, local_root, dry_run, cache):
    """按通道把 SDK 装到本机(或 dry-run 预览);返回失败数。"""
    failures = 0
    if not specs_by_channel:
        return failures
    arch = local_arch()
    sources = [local_root or default_local_root(c) for c in CHANNELS]
    for tool in tools:
        for channel, specs in specs_by_channel.items():
            ver, _ = specs[tool]
            root = local_root or default_local_root(channel)
            label = CHANNEL_LABEL[channel]
            try:
                status = plan_local(root, tool, ver, arch, sources) if dry_run \
                    else install_local(root, tool, ver, arch, cache, sources)
            except ScriptError as e:
                err(f"[本机 {label} {tool}] {e}")
                failures += 1
                continue
            print(f"  本机[{label}]: {status}", flush=True)
    return failures


def update_server(server, remote, specs_by_channel, tools, dry_run, cache):
    """把 SDK 推到服务器(或 dry-run 预览);返回失败数。"""
    failures = 0
    if not specs_by_channel:
        return failures
    for tool in tools:
        specs_tool = {c: specs[tool][0] for c, specs in specs_by_channel.items()}
        try:
            statuses = plan_server(server, tool, specs_tool, remote) if dry_run \
                else push_server(server, tool, specs_tool, cache, remote)
        except ScriptError as e:
            err(f"[服务器 {tool}] {e}")
            failures += 1
            continue
        for channel in specs_by_channel:
            print(f"  服务器[{CHANNEL_LABEL[channel]}]: {statuses[channel]}", flush=True)
    return failures


def main(argv=None):
    """命令行入口;返回退出码(0 全部成功,1 有失败)。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = build_parser()
    args = ap.parse_args(argv)
    if args.server_only and not args.server:
        ap.error("--server-only 需要同时指定 --server")
    if args.server_only and args.local_only:
        ap.error("--local-only 与 --server-only 不能同时指定")

    tools = ["claude", "codex"] if args.tool == "all" else [args.tool]
    channels = ["insiders", "stable"] if args.channel == "both" else [args.channel]
    do_local = not args.server_only
    do_remote = bool(args.server) and not args.local_only
    memo = {}                     # branch → {tool: 版本},避免同一分支重复请求
    failures = 0

    # ---- 1. 每个通道先解析「实际」期望版本(本机读安装 product.json,服务器 SSH 读) ----
    local_specs = {}              # channel -> {tool: (版本, 来源)}
    if do_local:
        local_specs, n_fail = collect_specs(
            "本机", channels, tools, args.branch, memo,
            lambda c: local_channel_exists(c, args.local_root),
            lambda c: find_product_json(c)[1],
            lambda c: CHANNELS[c][0])
        failures += n_fail

    server_specs = {}
    remote = None                 # 服务器平台探测结果(决定下载哪个 arch 的包)
    if do_remote:
        try:
            remote = detect_remote(args.server, args.remote_arch)
        except ScriptError as e:
            err(f"[服务器] {e}")
            failures += 1
        else:
            print(f"  服务器: {remote}", flush=True)
            server_specs, n_fail = collect_specs(
                "服务器", channels, tools, args.branch, memo,
                lambda c: remote_channel_exists(args.server, remote, c),
                lambda c: remote_read_product(args.server, remote, c),
                lambda c: CHANNELS[c][1])
            failures += n_fail

    # ---- 2. 逐个 tool 执行(每个通道用自己解析出的版本) ----
    cache = TgzCache()
    try:
        if do_local:
            failures += update_local(local_specs, tools, args.local_root, args.dry_run, cache)
        if remote is not None:
            failures += update_server(args.server, remote, server_specs, tools, args.dry_run, cache)
    finally:
        cache.cleanup()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
