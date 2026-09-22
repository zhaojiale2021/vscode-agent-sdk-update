"""CLI 层: 参数校验、dry-run 全流程(用假 SSH 传输, 不联网)。"""

# pylint: disable=missing-function-docstring,redefined-outer-name,unused-argument

import json
import sys

import pytest
from conftest import LocalTransport, make_tgz


def test_conflicting_flags(uas):
    with pytest.raises(SystemExit):
        uas.main(["--server-only"])                       # 缺 --server
    with pytest.raises(SystemExit):
        uas.main(["--local-only", "--server-only", "--server", "host"])


def test_local_dry_run_uses_installed_product(uas, monkeypatch, tmp_path, capsys):
    product = {"version": "1.136.1", "agentSdks": {"claude": {"version": "0.3.239"},
                                                   "codex": {"version": "0.146.0"}}}
    monkeypatch.setattr(uas, "local_channel_exists", lambda channel, root: channel == "stable")
    monkeypatch.setattr(uas, "find_product_json", lambda channel: (tmp_path / "product.json", product))
    assert uas.main(["--dry-run", "--local-only", "--channel", "stable",
                     "--local-root", str(tmp_path / "cache")]) == 0
    out = capsys.readouterr().out
    assert "0.3.239" in out and "将下载" in out


def test_local_run_installs_from_local_tgz(uas, monkeypatch, tmp_path, capsys):
    """走完整 main: 不联网, 下载被换成本地包。"""
    tgz = make_tgz(tmp_path / "claude.tgz", "@anthropic-ai/claude-agent-sdk", "0.3.239")

    class FakeCache:
        """替代 TgzCache: 不发网络, 直接用本地造好的包。"""

        def get(self, tool, version, arch):
            return tgz

        def cleanup(self):
            pass

    product = {"version": "1.136.1", "agentSdks": {"claude": {"version": "0.3.239"}}}
    monkeypatch.setattr(uas, "local_channel_exists", lambda channel, root: True)
    monkeypatch.setattr(uas, "find_product_json", lambda channel: (tmp_path / "p.json", product))
    monkeypatch.setattr(uas, "TgzCache", FakeCache)

    root = tmp_path / "cache"
    assert uas.main(["--local-only", "--tool", "claude", "--channel", "stable",
                     "--local-root", str(root)]) == 0
    assert (root / "claude" / "0.3.239" / uas.local_arch() / ".complete").is_file()


def test_server_dry_run_end_to_end(uas, monkeypatch, sandbox, capsys):
    """服务器 dry-run: 探测 → 读 product.json → 报告计划,全程假传输。"""
    transport = LocalTransport(uas, sandbox, "windows" if sys.platform == "win32" else "posix")
    if not transport.available:
        pytest.skip("本机没有可用的 shell/PowerShell")
    transport.install(monkeypatch)

    server_dir = ".vscode-server"
    product = sandbox / server_dir / "cli" / "servers" / "Stable-abc123" / "server" / "product.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps({"version": "1.136.1",
                                   "agentSdks": {"claude": {"version": "0.3.239"}}}),
                       encoding="utf-8")

    assert uas.main(["--server", "host", "--server-only", "--dry-run",
                     "--tool", "claude", "--channel", "stable"]) == 0
    out = capsys.readouterr().out
    assert "服务器" in out and "0.3.239" in out and "将下载并推送" in out


def test_server_detection_failure_exits_nonzero(uas, monkeypatch, capsys):
    monkeypatch.setattr(uas, "ssh_run", lambda s, c, stdin_path=None: (_ for _ in ()).throw(
        uas.ScriptError("Connection refused")))
    assert uas.main(["--server", "host", "--server-only", "--dry-run"]) == 1
    assert "Connection refused" in capsys.readouterr().err


def test_remote_arch_flag_skips_detection(uas, monkeypatch, sandbox, capsys):
    transport = LocalTransport(uas, sandbox, "posix")
    if not transport.available:
        pytest.skip("本机没有可用的 sh")
    transport.install(monkeypatch)
    monkeypatch.setattr(uas, "remote_channel_exists", lambda server, remote, channel: False)
    assert uas.main(["--server", "host", "--server-only", "--dry-run",
                     "--remote-arch", "win32-x64"]) == 0
    assert "windows/win32-x64" in capsys.readouterr().out
