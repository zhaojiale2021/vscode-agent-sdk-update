"""远端侧: 命令生成(两套 shell) + 用假传输真跑一遍安装/复用/上传/编排。"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name,unused-argument,too-few-public-methods

import json
import os

import pytest
from conftest import LocalTransport, encoded_ps, make_tgz

NPM_CLAUDE = "@anthropic-ai/claude-agent-sdk"


# ---------------------------------------------------------------- 命令生成

def test_sdk_rel_shape(uas):
    remote = uas.Remote("linux", "linux-x64")
    assert uas._sdk_rel("claude", "stable", "0.3.239", remote) == \
        ".vscode-server/data/agent-host/sdk-cache/claude/0.3.239/linux-x64"
    remote = uas.Remote("windows", "win32-x64")
    assert uas._sdk_rel("codex", "insiders", "0.153.0", remote) == \
        ".vscode-server-insiders/data/agent-host/sdk-cache/codex/0.153.0/win32-x64"


def test_posix_path_expressions(uas):
    assert uas._posix("a/b") == '"$HOME/a/b"'
    assert "Join-Path $env:USERPROFILE" in uas._win("a/b")
    assert uas._ps_lit("it's") == "'it''s'"          # 单引号要翻倍


def test_posix_install_creates_parent_not_target(uas):
    """mkdir 只能建 version 目录: 先建了 <arch> 的话 mv 会把 .tmp 塞进去。"""
    rel = uas._sdk_rel("claude", "stable", "9.9.9", uas.Remote("linux", "linux-x64"))
    cmd = uas._posix_install_cmd("claude", rel, uas._posix("pkg.tgz"))
    assert 'mkdir -p -- "$HOME/.vscode-server/data/agent-host/sdk-cache/claude/9.9.9"' in cmd
    assert 'mkdir -p -- "$HOME/.vscode-server/data/agent-host/sdk-cache/claude/9.9.9/linux-x64"' not in cmd
    tmp = '"$HOME/.vscode-server/data/agent-host/sdk-cache/claude/9.9.9/linux-x64.tmp"'
    assert 'tar -xzf "$HOME/pkg.tgz" -C ' + tmp in cmd
    assert 'mv -- "$HOME/.vscode-server/data/agent-host/sdk-cache/claude/9.9.9/linux-x64.tmp" ' \
           '"$HOME/.vscode-server/data/agent-host/sdk-cache/claude/9.9.9/linux-x64"' in cmd
    assert cmd.rstrip().endswith('.complete')


def test_ps_install_uses_dotnet_move_and_system_tar(uas):
    rel = uas._sdk_rel("claude", "stable", "9.9.9", uas.Remote("windows", "win32-x64"))
    script = uas._ps_install_script("claude", rel, "pkg.tgz")
    assert "[IO.Directory]::Move($tmp, $target)" in script      # Move-Item 在 8.3 短路径下会挂
    assert "System32/tar.exe" in script
    assert "'node_modules/" + NPM_CLAUDE + "/package.json'" in script
    assert "[Console]::OpenStandardInput" not in script


def test_ssh_run_ps_encodes_utf16_base64(uas, monkeypatch):
    seen = {}

    def fake_run(server, cmd):
        seen["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(uas, "ssh_run", fake_run)
    assert uas.ssh_run_ps("host", "$x = '中'; $x") == "ok"
    assert seen["cmd"].startswith("powershell -NoProfile -EncodedCommand ")
    script = encoded_ps(seen["cmd"])
    assert "$x = '中'" in script                      # 中文/引号原样送达
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "exit 0" in script


def test_remote_put_uses_scp_relative_name(uas, monkeypatch, tmp_path):
    """scp 目标写相对文件名: POSIX/Windows 都落在各自 home(实测 Windows 上也稳)。"""
    seen = []
    monkeypatch.setattr(uas, "scp_push", lambda server, local, name: seen.append((server, local, name)))

    payload = tmp_path / "x.tgz"
    payload.write_bytes(b"data")
    uas.remote_put("host", payload, "x.tgz")
    assert seen == [("host", payload, "x.tgz")]


def test_scp_push_reports_failure(uas, monkeypatch, tmp_path):
    payload = tmp_path / "x.tgz"
    payload.write_bytes(b"data")

    class Done:
        """假的失败 CompletedProcess。"""

        returncode = 1
        stdout = ""
        stderr = "no such file"

    monkeypatch.setattr(uas.subprocess, "run", lambda *a, **kw: Done())
    with pytest.raises(uas.ScriptError, match="scp 上传"):
        uas.scp_push("host", payload, "x.tgz")


def test_remote_rm_ignores_missing(uas, monkeypatch):
    cmds = []
    monkeypatch.setattr(uas, "ssh_run", lambda s, c: cmds.append(c) or "")
    uas.remote_rm("host", uas.Remote("linux", "linux-x64"), "x.tgz")
    uas.remote_rm("host", uas.Remote("windows", "win32-x64"), "x.tgz")
    assert cmds[0] == 'rm -f -- "$HOME/x.tgz"'
    assert "SilentlyContinue" in encoded_ps(cmds[1])


# ---------------------------------------------------------------- 真跑(假传输)

def seed_channel(sandbox, channel):
    (sandbox / (".vscode-server" if channel == "stable" else ".vscode-server-insiders") / "data").mkdir(
        parents=True, exist_ok=True)


def test_windows_transport_has_system_tar(uas, sandbox):
    """非 Windows 上用 pwsh 跑 Windows 方言时, System32\\tar.exe 由沙箱顶上。

    脚本里写的是 $env:SystemRoot\\System32\\tar.exe;真 Windows 上有,
    Linux CI(装了 pwsh)上没有, 没有就会报「Path 为 null」。
    """
    transport = LocalTransport(uas, sandbox, "windows")
    transport._shim_system_tar()          # 显式调用, 各平台都能验
    assert (transport.sysroot / "System32" / "tar.exe").is_file()


def test_channel_and_marker_roundtrip(uas, transport, remote_of, sandbox):
    remote = remote_of(transport.kind)
    assert uas.remote_channel_exists("host", remote, "stable") is False
    seed_channel(sandbox, "stable")
    assert uas.remote_channel_exists("host", remote, "stable") is True
    assert uas.remote_has_marker("host", remote, "claude", "stable", "9.9.9") is False


def test_install_copy_and_marker(uas, transport, remote_of, sandbox, tmp_path):
    remote = remote_of(transport.kind)
    seed_channel(sandbox, "stable")
    seed_channel(sandbox, "insiders")
    tgz = make_tgz(tmp_path / "pkg.tgz", NPM_CLAUDE, "9.9.9")
    uas.remote_put("host", tgz, tgz.name)          # 真实流程是先传包再解压

    uas.remote_install("host", remote, "claude", "stable", "9.9.9", tgz.name)
    rel = uas._sdk_rel("claude", "stable", "9.9.9", remote)
    assert (sandbox / rel / "node_modules" / NPM_CLAUDE / "package.json").is_file()
    assert (sandbox / rel / ".complete").is_file()
    assert not (sandbox / (rel + ".tmp")).exists()
    assert uas.remote_has_marker("host", remote, "claude", "stable", "9.9.9") is True

    # 同版本复用: 另一个通道直接复制, 不再解压
    uas.remote_copy("host", remote, "claude", "insiders", "stable", "9.9.9")
    assert uas.remote_has_marker("host", remote, "claude", "insiders", "9.9.9") is True
    assert (sandbox / uas._sdk_rel("claude", "insiders", "9.9.9", remote)
            / "node_modules" / NPM_CLAUDE / "package.json").is_file()


def test_install_failure_is_reported(uas, transport, remote_of, sandbox):
    remote = remote_of(transport.kind)
    seed_channel(sandbox, "stable")
    with pytest.raises(uas.ScriptError):
        uas.remote_install("host", remote, "claude", "stable", "9.9.9", "missing.tgz")


def test_upload_is_byte_identical(uas, transport, remote_of, sandbox, tmp_path):
    remote = remote_of(transport.kind)
    payload = tmp_path / "blob.bin"
    payload.write_bytes(os.urandom(300_000))
    uas.remote_put("host", payload, "blob.bin")
    assert (sandbox / "blob.bin").read_bytes() == payload.read_bytes()
    uas.remote_rm("host", remote, "blob.bin")
    assert not (sandbox / "blob.bin").exists()
    uas.remote_rm("host", remote, "blob.bin")         # 再删一次也不能抛


def test_read_product_picks_newest_and_skips_staging(uas, transport, remote_of, sandbox):
    remote = remote_of(transport.kind)

    def write(rel, version, mtime):
        path = sandbox / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
        os.utime(path, (mtime, mtime))

    write(".vscode-server/cli/servers/Stable-old/server/product.json", "1.100.0", 1_000_000_000)
    write(".vscode-server/cli/servers/Stable-new/server/product.json", "1.136.0", 2_000_000_000)
    write(".vscode-server/cli/servers/Stable-x.staging/server/product.json", "9.999.0", 3_000_000_000)
    write(".vscode-server/bin/abc123/product.json", "1.050.0", 500_000_000)

    product = uas.remote_read_product("host", remote, "stable")
    assert product == {"version": "1.136.0"}


class FakeCache:
    """替代 TgzCache: 不发网络, 直接用本地造好的包。"""

    def __init__(self, tgz):
        self.tgz = tgz
        self.hits = 0

    def get(self, tool, version, arch):
        self.hits += 1
        return self.tgz


@pytest.fixture
def pushable(uas, transport, remote_of, sandbox, tmp_path):
    """准备好两个通道 + 一个假 cache 的推送环境。"""
    seed_channel(sandbox, "stable")
    seed_channel(sandbox, "insiders")
    tgz = make_tgz(tmp_path / "claude.tgz", NPM_CLAUDE, "1.1.1")
    return uas, transport, remote_of(transport.kind), sandbox, FakeCache(tgz)




def test_push_uploads_once_for_two_channels(pushable):
    uas, transport, remote, sandbox, cache = pushable
    statuses = uas.push_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"},
                               cache, remote)
    assert cache.hits == 1 and transport.uploads == ["agent-sdk-claude-1.1.1.tgz"]
    assert all("已更新" in s for s in statuses.values())
    assert uas.remote_has_marker("host", remote, "claude", "stable", "1.1.1")
    assert uas.remote_has_marker("host", remote, "claude", "insiders", "1.1.1")
    assert not (sandbox / "agent-sdk-claude-1.1.1.tgz").exists()   # 临时包已清理


def test_push_second_run_skips_everything(pushable):
    uas, transport, remote, _sandbox, cache = pushable
    uas.push_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"}, cache, remote)
    before = len(transport.calls)
    statuses = uas.push_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"},
                               cache, remote)
    assert cache.hits == 1                              # 第二次没有再下载
    assert len(transport.calls) == before + 2           # 只做两次 .complete 检查
    assert all("已是最新" in s for s in statuses.values())


def test_push_reuses_same_version_from_other_channel(pushable):
    """stable 已装好同一版本 → insiders 在服务器内复制, 不再下载。"""
    uas, _transport, remote, _sandbox, cache = pushable
    uas.remote_put("host", cache.tgz, "seed.tgz")
    uas.remote_install("host", remote, "claude", "stable", "1.1.1", "seed.tgz")

    statuses = uas.push_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"},
                               cache, remote)
    assert "复用" in statuses["insiders"] and cache.hits == 0
    assert uas.remote_has_marker("host", remote, "claude", "insiders", "1.1.1")


def test_upload_falls_back_to_another_name(uas, transport, remote_of, sandbox, tmp_path, monkeypatch):
    """首选文件名被占(scp 打不开)时换名字重试,而不是整轮失败。"""
    remote = remote_of(transport.kind)
    tgz = make_tgz(tmp_path / "claude.tgz", NPM_CLAUDE, "1.1.1")
    calls = []

    def picky(server, local, name, attempts=3):
        calls.append(name)
        if name == "agent-sdk-claude-1.1.1.tgz":
            raise uas.ScriptError("scp 上传失败: dest open ... Failure")
        transport.scp(server, local, name)

    monkeypatch.setattr(uas, "scp_push", picky)
    name = uas.upload_tgz("host", remote, tgz, "claude", "1.1.1")
    assert name != "agent-sdk-claude-1.1.1.tgz" and name.startswith("agent-sdk-claude-1.1.1-")
    assert calls == ["agent-sdk-claude-1.1.1.tgz", name]
    assert (sandbox / name).is_file()


def test_push_falls_back_to_download_when_copy_fails(pushable, monkeypatch):
    uas, _transport, remote, _sandbox, cache = pushable
    uas.push_server("host", "claude", {"stable": "1.1.1"}, cache, remote)

    def boom(*_a, **_kw):
        raise uas.ScriptError("模拟复制失败")

    monkeypatch.setattr(uas, "remote_copy", boom)
    statuses = uas.push_server("host", "claude", {"insiders": "1.1.1"}, cache, remote)
    assert "已更新" in statuses["insiders"] and cache.hits == 2
    assert uas.remote_has_marker("host", remote, "claude", "insiders", "1.1.1")


def test_plan_server_messages(pushable):
    uas, _transport, remote, _sandbox, cache = pushable
    plan = uas.plan_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"}, remote)
    assert all("将下载并推送 1.1.1" in v for v in plan.values())

    uas.push_server("host", "claude", {"stable": "1.1.1"}, cache, remote)
    plan = uas.plan_server("host", "claude", {"stable": "1.1.1", "insiders": "1.1.1"}, remote)
    assert "已是最新" in plan["stable"] and "复用" in plan["insiders"]
