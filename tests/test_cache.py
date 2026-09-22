"""下载缓存: 完整包复用、半包续传、坏包重下、过期清理。"""

# pylint: disable=missing-function-docstring,protected-access,unused-argument,too-few-public-methods

import os
import tempfile
import time
from pathlib import Path

from conftest import make_tgz

NPM = "@anthropic-ai/claude-agent-sdk"
VERSION = "0.3.239"


class FakeDownload:
    """假的下载器: 按需生成一个版本正确的包(顺便记录被调用次数)。"""

    def __init__(self, tmp_path, npm=NPM, version=VERSION):
        self.tmp_path = tmp_path
        self.npm = npm
        self.version = version
        self.calls = []

    def __call__(self, url, dest, attempts=3):
        self.calls.append(url)
        make_tgz(dest, self.npm, self.version)


def build_cache(uas, tmp_path, monkeypatch, remote_size, fake=None):
    """建缓存并把网络换掉: http_size 给定远端长度, http_download 现场造包。"""
    cache = uas.TgzCache(tmp_path / "cache")
    fake = fake or FakeDownload(tmp_path)
    monkeypatch.setattr(uas, "http_size", lambda url, timeout=30: remote_size)
    monkeypatch.setattr(uas, "http_download", fake)
    return cache, fake


def test_downloads_once_then_reuses(uas, tmp_path, monkeypatch, capsys):
    payload_len = len(make_tgz(tmp_path / "probe.tgz", NPM, VERSION).read_bytes())
    cache, fake = build_cache(uas, tmp_path, monkeypatch, payload_len)
    first = cache.get("claude", VERSION, "win32-x64")
    assert first.is_file() and len(fake.calls) == 1

    second = cache.get("claude", VERSION, "win32-x64")
    assert second == first and len(fake.calls) == 1        # 同次运行内不再下
    capsys.readouterr()

    again = uas.TgzCache(cache._dir).get("claude", VERSION, "win32-x64")
    assert again == first and len(fake.calls) == 1         # 新进程也能复用
    assert "复用已下载" in capsys.readouterr().out


def test_resumes_partial_file(uas, tmp_path, monkeypatch, capsys):
    """上次没下完(比远端小)→ 留着续传,而不是删了重下。"""
    full = make_tgz(tmp_path / "full.tgz", NPM, VERSION)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    partial = cache_dir / f"claude-{VERSION}-win32-x64.tgz"
    partial.write_bytes(full.read_bytes()[:100])           # 半包

    cache, fake = build_cache(uas, tmp_path, monkeypatch, full.stat().st_size)
    got = cache.get("claude", VERSION, "win32-x64")
    assert got == partial and len(fake.calls) == 1
    assert "续传上次没下完" in capsys.readouterr().out


def test_discards_corrupt_complete_file(uas, tmp_path, monkeypatch, capsys):
    """大小对得上但内容是坏的 → 删掉重下。"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    broken = cache_dir / f"claude-{VERSION}-win32-x64.tgz"
    broken.write_bytes(b"not a tarball at all")

    cache, fake = build_cache(uas, tmp_path, monkeypatch, broken.stat().st_size)
    got = cache.get("claude", VERSION, "win32-x64")
    assert len(fake.calls) == 1
    assert got.read_bytes() != b"not a tarball at all"
    assert "复用已下载" not in capsys.readouterr().out


def test_prunes_old_packages(uas, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    old = cache_dir / "codex-0.1.0-linux-x64.tgz"
    old.write_bytes(b"stale")
    fresh = cache_dir / "codex-0.2.0-linux-x64.tgz"
    fresh.write_bytes(b"fresh")
    stale_time = time.time() - (uas.TgzCache.TTL_DAYS + 1) * 86400
    os.utime(old, (stale_time, stale_time))

    uas.TgzCache(cache_dir)
    assert not old.exists() and fresh.exists()


def test_default_cache_dir_under_system_temp(uas):
    """默认缓存在系统临时目录下、跨实例稳定(两次运行才会复用同一个包)。"""
    assert uas.TgzCache()._dir == Path(tempfile.gettempdir()) / "agent-sdk-tgz-cache"
