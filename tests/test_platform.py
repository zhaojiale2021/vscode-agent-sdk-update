"""平台/架构探测: arch 表、uname 解析、cmd 兜底、--remote-arch。"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name
# pylint: disable=unused-argument,too-few-public-methods

import pytest

# 实测 10.0.12.44(Windows + PowerShell 默认 shell): 装的 coreutils uname
# 不认 -s/-m,两次调用都输出同一行。
REAL_WINDOWS_UNAME = "Windows_NT 10.0\nWindows_NT 10.0"


@pytest.mark.parametrize("os_name,machine,arch", [
    ("windows", "AMD64", "win32-x64"),
    ("windows", "x86_64", "win32-x64"),
    ("windows", "ARM64", "win32-arm64"),
    ("linux", "x86_64", "linux-x64"),
    ("linux", "aarch64", "linux-arm64"),
    ("darwin", "arm64", "darwin-arm64"),
    ("darwin", "x86_64", "darwin-x64"),
    ("linux", "sparc", None),
    ("plan9", "x86_64", None),
    ("windows", "", None),
])
def test_arch_of(uas, os_name, machine, arch):
    assert uas.arch_of(os_name, machine) == arch


def test_local_arch_is_known(uas):
    assert uas.local_arch() in ("win32-x64", "win32-arm64", "linux-x64",
                                "linux-arm64", "darwin-x64", "darwin-arm64")


@pytest.mark.parametrize("out,expected", [
    ("Linux\nx86_64", ("linux", "x86_64")),
    ("Linux\n5.15.0-91-generic", ("linux", "")),
    ("Darwin\narm64", ("darwin", "arm64")),
    ("MINGW64_NT-10.0-19045\nx86_64", ("windows", "x86_64")),
    ("CYGWIN_NT-10.0\naarch64", ("windows", "aarch64")),
    (REAL_WINDOWS_UNAME, ("windows", "")),
    ("SunOS\nsparc", (None, "")),
    ("", (None, "")),
])
def test_parse_uname(uas, out, expected):
    assert uas._parse_uname(out) == expected


@pytest.mark.parametrize("out,expected", [
    ("Windows_NT AMD64", ("windows", "amd64")),
    ("Windows_NT ARM64", ("windows", "arm64")),
    ("'cmd' 不是内部或外部命令", (None, "")),
    ("", (None, "")),
])
def test_parse_windows_probe(uas, out, expected):
    assert uas._parse_windows_probe(out) == expected


class FakeSsh:
    """按命令前缀返回预置输出,并记录调用顺序。"""

    def __init__(self, uas_mod, replies):
        self.uas = uas_mod
        self.replies = replies
        self.calls = []

    def __call__(self, server, cmd, stdin_path=None):
        self.calls.append(cmd)
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise self.uas.ScriptError(f"未预置的命令: {cmd}")


def patch(monkeypatch, uas, replies):
    fake = FakeSsh(uas, replies)
    monkeypatch.setattr(uas, "ssh_run", fake)
    return fake


def test_detect_real_windows_server(uas, monkeypatch):
    """10.0.12.44: uname 不认参数 → 用 cmd 拿 %PROCESSOR_ARCHITECTURE%。"""
    fake = patch(monkeypatch, uas, {
        "uname": REAL_WINDOWS_UNAME,
        "cmd /c": "Windows_NT AMD64",
    })
    remote = uas.detect_remote("10.0.12.44")
    assert (remote.os, remote.arch, remote.is_windows) == ("windows", "win32-x64", True)
    assert fake.calls == ["uname -s; uname -m", "cmd /c echo %OS% %PROCESSOR_ARCHITECTURE%"]


def test_detect_windows_without_uname(uas, monkeypatch):
    """cmd / PowerShell 默认 shell: uname 命令不存在,直接走 cmd 探测。"""
    patch(monkeypatch, uas, {
        "uname": uas.ScriptError("'uname' 不是内部或外部命令"),
        "cmd /c": "Windows_NT ARM64",
    })
    assert uas.detect_remote("host").arch == "win32-arm64"


def test_detect_linux_does_not_probe_cmd(uas, monkeypatch):
    fake = patch(monkeypatch, uas, {"uname": "Linux\nx86_64"})
    assert uas.detect_remote("host").arch == "linux-x64"
    assert len(fake.calls) == 1


def test_detect_git_bash_on_windows_takes_windows_path(uas, monkeypatch):
    """Git Bash 默认 shell: uname 报 MINGW64_NT…,仍是 Windows 包。"""
    patch(monkeypatch, uas, {"uname": "MINGW64_NT-10.0-19045\nx86_64"})
    remote = uas.detect_remote("host")
    assert (remote.os, remote.arch, remote.is_windows) == ("windows", "win32-x64", True)


def test_detect_remote_arch_override(uas, monkeypatch):
    patch(monkeypatch, uas, {"uname": "Linux\narmv7l"})
    assert uas.detect_remote("host", "linux-arm64").arch == "linux-arm64"


def test_remote_arch_override_also_sets_os(uas, monkeypatch):
    """指定 win32-x64 就要走 PowerShell 方言,不能还按探测到的 Linux 发 POSIX 命令。"""
    patch(monkeypatch, uas, {"uname": "Linux\nx86_64"})
    remote = uas.detect_remote("host", "win32-x64")
    assert (remote.os, remote.arch, remote.is_windows) == ("windows", "win32-x64", True)


def test_detect_unreachable_server_reports_ssh_error(uas, monkeypatch):
    err = uas.ScriptError("Permission denied (publickey)")
    patch(monkeypatch, uas, {"uname": err, "cmd /c": err})
    with pytest.raises(uas.ScriptError, match="Permission denied"):
        uas.detect_remote("host")
