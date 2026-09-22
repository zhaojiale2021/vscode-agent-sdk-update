"""本机侧: tgz 校验、原子安装、同版本复用、路径推导。"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name
# pylint: disable=unused-argument,too-few-public-methods

import json
import os
import sys

import pytest
from conftest import make_tgz

NPM = {"claude": "@anthropic-ai/claude-agent-sdk", "codex": "@openai/codex"}


class FakeCache:
    """替代 TgzCache: 不发网络, 直接用本地造好的包。"""

    def __init__(self, tgz):
        self.tgz = tgz
        self.hits = 0

    def get(self, tool, version, arch):
        self.hits += 1
        return self.tgz


@pytest.fixture
def claude_tgz(tmp_path):
    return make_tgz(tmp_path / "claude.tgz", NPM["claude"], "0.3.239")


def test_verify_tgz_ok(uas, claude_tgz):
    uas.verify_tgz(claude_tgz, NPM["claude"], "0.3.239")


def test_verify_tgz_wrong_version(uas, claude_tgz):
    with pytest.raises(uas.ScriptError, match="版本校验失败"):
        uas.verify_tgz(claude_tgz, NPM["claude"], "0.0.1")


def test_verify_tgz_missing_npm(uas, claude_tgz):
    with pytest.raises(uas.ScriptError, match="缺少"):
        uas.verify_tgz(claude_tgz, NPM["codex"], "0.3.239")


def test_verify_tgz_rejects_foreign_root(uas, tmp_path):
    bad = make_tgz(tmp_path / "bad.tgz", NPM["claude"], "0.3.239", with_root=False)
    with pytest.raises(uas.ScriptError, match="归档根"):
        uas.verify_tgz(bad, NPM["claude"], "0.3.239")


def test_install_local_writes_complete_and_skips_second_time(uas, tmp_path, claude_tgz):
    root = tmp_path / "cache"
    cache = FakeCache(claude_tgz)
    status = uas.install_local(root, "claude", "0.3.239", "linux-x64", cache, [])
    assert "已更新" in status and cache.hits == 1

    target = root / "claude" / "0.3.239" / "linux-x64"
    assert (target / ".complete").is_file()
    assert (target / "node_modules" / NPM["claude"] / "package.json").is_file()
    assert not [p for p in (root / "claude" / "0.3.239").iterdir() if p.name.startswith(".tmp-")]

    assert "已是最新" in uas.install_local(root, "claude", "0.3.239", "linux-x64", cache, [])
    assert cache.hits == 1                       # 已存在就不下载


def test_install_local_reuses_installed_copy(uas, tmp_path, claude_tgz):
    """另一个通道已装同版本 → 直接复制,不再下载。"""
    source_root = tmp_path / "cache-stable"
    assert "已更新" in uas.install_local(source_root, "claude", "0.3.239", "linux-x64",
                                        FakeCache(claude_tgz), [])

    target_root = tmp_path / "cache-insiders"
    cache = FakeCache(claude_tgz)
    status = uas.install_local(target_root, "claude", "0.3.239", "linux-x64", cache, [source_root])
    assert "复用" in status and cache.hits == 0
    assert (target_root / "claude" / "0.3.239" / "linux-x64" / ".complete").is_file()


def test_find_local_source_requires_complete_marker(uas, tmp_path):
    root = tmp_path / "cache"
    (root / "claude" / "1.0.0" / "linux-x64" / "node_modules").mkdir(parents=True)
    assert uas.find_local_source([root], "claude", "1.0.0", "linux-x64") is None
    (root / "claude" / "1.0.0" / "linux-x64" / ".complete").touch()
    assert uas.find_local_source([root], "claude", "1.0.0", "linux-x64") is not None


def test_install_local_failure_leaves_no_partial_target(uas, tmp_path):
    class Boom:
        """下载永远失败的假 cache。"""

        def get(self, tool, version, arch):
            raise uas.ScriptError("下载失败")

    root = tmp_path / "cache"
    with pytest.raises(uas.ScriptError):
        uas.install_local(root, "claude", "1.0.0", "linux-x64", Boom(), [])
    assert not (root / "claude" / "1.0.0" / "linux-x64").exists()
    leftovers = list((root / "claude" / "1.0.0").iterdir()) if (root / "claude" / "1.0.0").exists() else []
    assert not [p for p in leftovers if p.name.startswith(".tmp-")]


def test_plan_local_messages(uas, tmp_path, claude_tgz):
    root = tmp_path / "cache"
    assert "将下载" in uas.plan_local(root, "claude", "0.3.239", "linux-x64", [])
    uas.install_local(root, "claude", "0.3.239", "linux-x64", FakeCache(claude_tgz), [])
    assert "已是最新" in uas.plan_local(root, "claude", "0.3.239", "linux-x64", [])
    assert "将复用" in uas.plan_local(tmp_path / "other", "claude", "0.3.239", "linux-x64", [root])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专用路径推导")
def test_default_local_root_windows(uas, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert uas.default_local_root("stable") == tmp_path / "Code" / "agent-host" / "sdk-cache"
    assert uas.default_local_root("insiders") == \
        tmp_path / "Code - Insiders" / "agent-host" / "sdk-cache"
    assert uas.local_channel_exists("stable", None) is False
    (tmp_path / "Code").mkdir()
    assert uas.local_channel_exists("stable", None) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 下走 ~/.vscode-server")
def test_default_local_root_posix(uas, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert uas.default_local_root("stable") == tmp_path / ".vscode-server" / "data" / "agent-host" / "sdk-cache"
    assert uas.local_channel_exists("stable", None) is False
    (tmp_path / ".vscode-server").mkdir()
    assert uas.local_channel_exists("stable", None) is True


def test_copy_tree(uas, tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("hello", encoding="utf-8")
    dst = tmp_path / "dst"
    uas.copy_tree(src, dst)
    assert (dst / "sub" / "f.txt").read_text(encoding="utf-8") == "hello"


def test_find_product_json_prefers_newest(uas, monkeypatch, tmp_path):
    """本机安装目录: <install>/<commit>/resources/app/product.json,取最新。"""
    install = tmp_path / "Code"
    for commit, version, mtime in (("aaa", "1.100.0", 1_000_000_000), ("bbb", "1.136.1", 2_000_000_000)):
        path = install / commit / "resources" / "app" / "product.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": version}), encoding="utf-8")
        os.utime(path, (mtime, mtime))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(uas, "_app_roots", lambda channel: [install])
    path, product = uas.find_product_json("stable")
    assert product["version"] == "1.136.1" and path.parent.parent.parent.name == "bbb"


def test_find_product_json_unreadable(uas, monkeypatch, tmp_path):
    root = tmp_path / "Code"
    (root / "resources" / "app").mkdir(parents=True)
    (root / "resources" / "app" / "product.json").write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(uas, "_app_roots", lambda channel: [root])
    assert uas.find_product_json("stable") == (None, None)
