"""版本解析: product.json / 仓库 package.json / 分支回退的优先级。"""

# pylint: disable=missing-function-docstring,protected-access,unused-argument

import base64
import json

import pytest


def test_minor_branch(uas):
    assert uas.minor_branch("1.136.1") == "1.136"
    assert uas.minor_branch("1.138.0-insider") == "1.138"
    assert uas.minor_branch("1.136") == "1.136"
    assert uas.minor_branch("insider") is None
    assert uas.minor_branch("") is None


def test_parse_pkg_text_raw_and_api(uas):
    raw = json.dumps({"devDependencies": {"@openai/codex": "^0.146.0"}})
    assert uas._parse_pkg_text(raw)["devDependencies"]["@openai/codex"] == "^0.146.0"

    api = json.dumps({"content": base64.b64encode(raw.encode()).decode()})
    assert uas._parse_pkg_text(api)["devDependencies"]["@openai/codex"] == "^0.146.0"

    with pytest.raises(uas.ScriptError):
        uas._parse_pkg_text("not json")


def test_fetch_versions_falls_back_to_api(uas, monkeypatch):
    """raw.githubusercontent 不可达时回退 api.github.com,并剥掉 ^ ~ 等前缀。"""
    seen = []

    def fake_get(url, timeout=60):
        seen.append(url)
        if "raw.githubusercontent" in url:
            raise uas.ScriptError("无法访问")
        return json.dumps({"dependencies": {"@anthropic-ai/claude-agent-sdk": "~0.3.258",
                                            "@openai/codex": "^0.153.0"}})

    monkeypatch.setattr(uas, "http_get_text", fake_get)
    assert uas.fetch_versions("main") == {"claude": "0.3.258", "codex": "0.153.0"}
    assert len(seen) == 2 and "api.github.com" in seen[1]


def test_fetch_versions_reports_both_failures(uas, monkeypatch):
    monkeypatch.setattr(uas, "http_get_text", lambda url, timeout=60: (_ for _ in ()).throw(
        uas.ScriptError("挂了")))
    with pytest.raises(uas.ScriptError, match="挂了"):
        uas.fetch_versions("main")


def test_resolve_versions_prefers_installed_product(uas):
    product = {"version": "1.136.1", "agentSdks": {"claude": {"version": "0.3.239"},
                                                   "codex": {"version": "0.146.0"}}}
    specs = uas.resolve_versions(product, "stable", ["claude", "codex"], "main", {})
    assert specs["claude"][0] == "0.3.239"
    assert specs["codex"][0] == "0.146.0"
    assert "product.json" in specs["claude"][1]


def test_resolve_versions_agent_sdks_plain_string(uas):
    """agentSdks 也可能是直接写版本字符串而不是 {"version": …}。"""
    product = {"version": "1.136.1", "agentSdks": {"claude": "0.3.239"}}
    assert uas.resolve_versions(product, "stable", ["claude"], "main", {})["claude"][0] == "0.3.239"


def test_resolve_versions_stable_uses_release_branch(uas, monkeypatch):
    """没有 agentSdks 时, stable 按已装版本推导 release/<x>。"""
    monkeypatch.setattr(uas, "branch_versions",
                        lambda memo, branch: {"claude": "0.3.100", "codex": "0.100.0"})
    specs = uas.resolve_versions({"version": "1.136.1"}, "stable", ["claude"], "main", {})
    assert specs["claude"] == ("0.3.100", "已装 VS Code 1.136.1 对应 vscode release/1.136")


def test_resolve_versions_falls_back_to_branch_with_warning(uas, monkeypatch):
    monkeypatch.setattr(uas, "branch_versions", lambda memo, branch: {"claude": "0.3.258"})
    ver, source = uas.resolve_versions(None, "stable", ["claude"], "main", {})["claude"]
    assert ver == "0.3.258" and "警告" in source

    ver, source = uas.resolve_versions(None, "insiders", ["claude"], "main", {})["claude"]
    assert ver == "0.3.258" and "警告" not in source


def test_resolve_versions_missing_tool_is_error(uas, monkeypatch):
    monkeypatch.setattr(uas, "branch_versions", lambda memo, branch: {"codex": "0.153.0"})
    with pytest.raises(uas.ScriptError, match="package.json 中没有"):
        uas.resolve_versions(None, "insiders", ["claude"], "main", {})


def test_parse_product_text_tolerates_bom(uas):
    assert uas._parse_product_text('﻿{"version": "1.2.3"}') == {"version": "1.2.3"}
    assert uas._parse_product_text("<html>") is None
