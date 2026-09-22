"""pytest 公共设施: 加载被测脚本 + 假 SSH 传输(把远端命令换成本机执行)。

被测脚本是单文件命令(`python update_agent_sdk.py`),所以按路径加载成模块;
测试全程不联网: 需要版本/下载的地方一律显式注入假实现或本地文件。
"""

# pylint: disable=redefined-outer-name,unused-argument

import base64
import importlib.util
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = sys.platform == "win32"


@pytest.fixture(scope="session")
def uas():
    """按路径加载 update_agent_sdk.py(不执行 main)。"""
    spec = importlib.util.spec_from_file_location("update_agent_sdk", ROOT / "update_agent_sdk.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_agent_sdk"] = module
    spec.loader.exec_module(module)
    return module


def make_tgz(path: Path, npm: str, version: str, with_root: bool = True) -> Path:
    """造一个 tgz: with_root 时根为 node_modules/<npm>/package.json,否则根为 bundle/。"""
    build = path.parent / (path.stem + "-build")
    arc_root = "node_modules" if with_root else "bundle"
    pkg = build / arc_root / npm
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text('{"version": "%s"}' % version, encoding="utf-8")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(build / arc_root, arcname=arc_root)
    return path


def _find_powershell():
    exe = shutil.which("powershell") or shutil.which("pwsh")
    return exe or ""


class LocalTransport:
    """把 uas.ssh_run 换成本地执行。

    - Windows 远端: 解开 -EncodedCommand 的 base64 直接跑 PowerShell,
      USERPROFILE 指向沙箱(取 realpath: 8.3 短名会让 Copy-Item 之类的
      provider cmdlet 误报「对象不存在」,真实用户目录不会长这样)。
    - POSIX 远端: 生成的命令原样交给系统 shell, HOME 指向沙箱。
    """

    def __init__(self, uas_mod, root: Path, kind: str):
        self.uas = uas_mod
        self.kind = kind                     # windows / posix
        self.windows = kind == "windows"
        self.root = Path(os.path.realpath(str(root)))
        self.calls = []                      # 记录收到的命令, 供断言
        self.uploads = []                    # 记录上传过的文件名
        self.powershell = _find_powershell() if self.windows else ""
        if self.windows and sys.platform != "win32":
            self._shim_system_tar()

    @property
    def sysroot(self):
        """Windows 方言脚本里的 %SystemRoot%。"""
        return self.root / "Windows"

    def _shim_system_tar(self):
        """非 Windows 上用 pwsh 跑「Windows 那套命令」时, 造一个 System32\\tar.exe。

        被测脚本用的是 $env:SystemRoot\\System32\\tar.exe(Windows 自带), 在 Linux 上
        SystemRoot 为空会直接报「Path 为 null」; 放个转发给系统 tar 的脚本, Windows
        命令方言就能在任何平台被真执行(而不是只在 Windows 上跳过)。
        """
        bindir = self.sysroot / "System32"
        bindir.mkdir(parents=True, exist_ok=True)
        tar_exe = bindir / "tar.exe"
        if not tar_exe.exists():
            tar_exe.write_text('#!/bin/sh\nexec tar "$@"\n', encoding="utf-8")
            tar_exe.chmod(0o755)

    @property
    def available(self):
        """本机能否真的跑这套命令(没有对应 shell 的用例要跳过)。"""
        return bool(self.powershell) if self.windows else bool(shutil.which("sh"))

    def env(self):
        """子进程环境: 把用户目录指到沙箱(Windows 方言还要 SystemRoot)。"""
        if self.windows:
            env = dict(os.environ, USERPROFILE=str(self.root))
            if sys.platform != "win32":
                env["SystemRoot"] = str(self.sysroot)
            return env
        return dict(os.environ, HOME=str(self.root))

    def _run(self, argv, stdin):
        return subprocess.run(argv, stdin=stdin, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env(), check=False)

    def __call__(self, server, cmd):
        self.calls.append(cmd)
        if "powershell -NoProfile -EncodedCommand " in cmd:
            argv = [self.powershell, "-NoProfile", "-EncodedCommand", cmd.rsplit(" ", 1)[1]]
        else:
            argv = ["sh", "-c", cmd]
        proc = self._run(argv, subprocess.DEVNULL)
        if proc.returncode != 0:
            raise self.uas.ScriptError(f"{cmd.split()[0]}: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    def scp(self, server, local, name):
        """假 scp: 相对文件名落到沙箱(等价于远端 home)。"""
        self.uploads.append(name)
        shutil.copyfile(local, self.root / name)

    def install(self, monkeypatch):
        """把假传输装到被测模块上。"""
        monkeypatch.setattr(self.uas, "ssh_run", self)
        monkeypatch.setattr(self.uas, "scp_push", self.scp)
        return self


@pytest.fixture
def sandbox(tmp_path):
    """空的服务器用户目录。"""
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture(params=["windows", "posix"])
def transport(request, uas, sandbox, monkeypatch):
    """两个平台的假传输;当前系统跑不了的那种自动跳过。"""
    transport_ = LocalTransport(uas, sandbox, request.param)
    if not transport_.available:
        pytest.skip(f"本机没有可用的 {'PowerShell' if transport_.windows else 'sh'}")
    return transport_.install(monkeypatch)


@pytest.fixture
def remote_of(uas):
    """按 kind 造一个 Remote。"""
    def make(kind="posix", arch=None):
        os_name = "windows" if kind == "windows" else "linux"
        return uas.Remote(os_name, arch or ("win32-x64" if os_name == "windows" else "linux-x64"))
    return make


def encoded_ps(cmd: str):
    """从 ssh 命令串里取出 PowerShell 脚本原文(给断言用)。"""
    return base64.b64decode(cmd.rsplit(" ", 1)[1]).decode("utf-16-le")
