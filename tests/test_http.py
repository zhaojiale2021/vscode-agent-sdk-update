"""下载层: 断流后按 Range 续传、完整性校验、4xx 不重试。"""

# pylint: disable=missing-function-docstring,protected-access,unused-argument,too-few-public-methods,missing-class-docstring

import io
import urllib.error

import pytest


class FakeResponse(io.BytesIO):
    """最小可用的 urlopen 返回体(status/headers/read/上下文管理器)。"""

    def __init__(self, payload, status=200, content_length=None):
        super().__init__(payload)
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeCdn:
    """支持 Range 的假 CDN;可让前 fail_times 次请求只发 partial 字节就断。"""

    def __init__(self, payload, partial=1000, fail_times=0, ignore_range=False):
        self.payload = payload
        self.partial = partial
        self.fail_times = fail_times
        self.ignore_range = ignore_range
        self.requests = 0
        self.ranges = []

    def __call__(self, req, timeout):
        self.requests += 1
        start = 0
        rng = req.headers.get("Range")
        if rng and not self.ignore_range:
            self.ranges.append(rng)
            start = int(rng.split("=")[1].rstrip("-"))
        if self.requests <= self.fail_times:
            body = self.payload[start:start + self.partial]         # 提前断流
        else:
            body = self.payload[start:]
        status = 206 if start else 200
        total = len(self.payload) - start
        return FakeResponse(body, status=status, content_length=total)


def patch(monkeypatch, uas, cdn):
    monkeypatch.setattr(uas.urllib.request, "urlopen", cdn)
    return cdn


def test_download_ok(uas, monkeypatch, tmp_path):
    payload = b"x" * 3000
    patch(monkeypatch, uas, FakeCdn(payload))
    dest = tmp_path / "a.tgz"
    uas.http_download("http://cdn/a.tgz", dest)
    assert dest.read_bytes() == payload


def test_download_resumes_from_breakpoint(uas, monkeypatch, tmp_path):
    """第一次只发 1000 字节就断,第二次必须带 Range 从 1000 接着下。"""
    payload = bytes(range(256)) * 40          # 10240 字节,内容有区分度
    cdn = patch(monkeypatch, uas, FakeCdn(payload, partial=1000, fail_times=1))
    dest = tmp_path / "b.tgz"
    uas.http_download("http://cdn/b.tgz", dest)
    assert dest.read_bytes() == payload
    assert cdn.requests == 2 and cdn.ranges == ["bytes=1000-"]


def test_download_restarts_when_server_ignores_range(uas, monkeypatch, tmp_path):
    """服务端不支持 Range(回 200 全量)时也要能下完。"""
    payload = b"z" * 4096
    cdn = patch(monkeypatch, uas, FakeCdn(payload, partial=512, fail_times=1, ignore_range=True))
    dest = tmp_path / "c.tgz"
    uas.http_download("http://cdn/c.tgz", dest)
    assert dest.read_bytes() == payload and cdn.requests == 2


def test_download_keeps_retrying_while_progressing(uas, monkeypatch, tmp_path):
    """链路一直抖:每次都能多下一点,就该继续重试直到下完,而不是三次就放弃。"""
    payload = b"p" * 10000
    cdn = patch(monkeypatch, uas, FakeCdn(payload, partial=1000, fail_times=3))
    dest = tmp_path / "g.tgz"
    uas.http_download("http://cdn/g.tgz", dest)
    assert cdn.requests == 4 and dest.read_bytes() == payload


def test_download_gives_up_when_no_progress(uas, monkeypatch, tmp_path):
    """连续两次一点没多 → 提前放弃(别对着死链耗)。"""
    def always_timeout(req, timeout):
        raise TimeoutError("read timed out")

    patch(monkeypatch, uas, always_timeout)
    dest = tmp_path / "d.tgz"
    with pytest.raises(uas.ScriptError, match="下载失败"):
        uas.http_download("http://cdn/d.tgz", dest)


def test_download_does_not_retry_404(uas, monkeypatch, tmp_path):
    calls = []

    def not_found(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    patch(monkeypatch, uas, not_found)
    with pytest.raises(uas.ScriptError, match="404"):
        uas.http_download("http://cdn/missing.tgz", tmp_path / "e.tgz")
    assert len(calls) == 1                    # 4xx 不值得重试


def test_download_retries_5xx(uas, monkeypatch, tmp_path):
    payload = b"w" * 2000
    calls = []

    def flaky(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Busy", {}, None)
        return FakeResponse(payload, content_length=len(payload))

    patch(monkeypatch, uas, flaky)
    dest = tmp_path / "f.tgz"
    uas.http_download("http://cdn/f.tgz", dest)
    assert len(calls) == 2 and dest.read_bytes() == payload
