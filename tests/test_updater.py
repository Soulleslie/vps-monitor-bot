import json

import httpx
import pytest

from bot import __version__
from bot.updater import Updater, version_tuple

OLD = "a" * 40
NEW = "b" * 40


def github(routes: dict):
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                status, body = resp
                if isinstance(body, str):
                    return httpx.Response(status, text=body)
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler), calls


def head(sha, msg="feat: new"):
    return {"sha": sha, "commit": {"message": msg + "\n\nbody", "committer": {"date": "2026-11-08T10:00:00Z"}}}


API = "https://api.github.com/repos/o/r"
RAW = "https://raw.githubusercontent.com/o/r"


def make(tmp_path, transport, commit=OLD, host=True):
    run = tmp_path / "run"
    run.mkdir()
    if commit:
        (run / "version.json").write_text(json.dumps({"commit": commit}))
    if host:
        (run / "updater.json").write_text(json.dumps({"enabled": True}))
    return Updater("o/r", "main", str(run), transport=transport)


OLDER = "0" * 40


def commits(*shas_msgs):
    return [head(sha, msg) for sha, msg in shas_msgs]


async def test_update_available_with_changelog(tmp_path):
    transport, _ = github({
        f"{API}/commits?": (200, commits((NEW, "fix: 修复解析"), ("c" * 40, "feat: 第一项"), (OLD, "旧版本"), (OLDER, "更早"))),
        f"{RAW}/{NEW}/bot/__init__.py": (200, '__version__ = "9.9.9"\n'),
    })
    u = make(tmp_path, transport)
    r = await u.check()
    assert r["update_available"] and r["ahead_by"] == 2 and r["current_unknown"] is False
    assert r["latest"]["version"] == "9.9.9" and r["latest"]["subject"] == "fix: 修复解析"
    assert [c["subject"] for c in r["commits"]] == ["fix: 修复解析", "feat: 第一项"]   # 最新在前
    assert [(c["subject"], c["installed"]) for c in r["recent"]] == [
        ("fix: 修复解析", False), ("feat: 第一项", False), ("旧版本", True), ("更早", True)]


async def test_up_to_date_still_shows_recent_changes(tmp_path):
    transport, calls = github({
        f"{API}/commits?": (200, commits((OLD, "feat: 当前版本"), (OLDER, "更早"))),
        f"{RAW}/{OLD}/bot/__init__.py": (200, f'__version__ = "{__version__}"'),
    })
    u = make(tmp_path, transport)
    r = await u.check()
    assert r["update_available"] is False and "error" not in r
    assert [(c["subject"], c["installed"]) for c in r["recent"]] == [("feat: 当前版本", True), ("更早", True)]
    assert not any("compare" in c for c in calls)
    # 缓存：再次检查不发请求
    n = len(calls)
    await u.check()
    assert len(calls) == n


async def test_current_older_than_recent_list_uses_compare(tmp_path):
    transport, _ = github({
        f"{API}/commits?": (200, commits((NEW, "最新"), ("c" * 40, "中间"))),
        f"{RAW}/{NEW}/bot/__init__.py": (200, '__version__ = "1.0.0"'),
        f"{API}/compare/{OLD}...{NEW}": (200, {"status": "ahead", "ahead_by": 40, "commits": [head(NEW, "最新")]}),
    })
    r = await make(tmp_path, transport).check()
    assert r["update_available"] and r["ahead_by"] == 40
    assert all(c["installed"] is False for c in r["recent"])


async def test_cache_invalidated_when_running_version_changes(tmp_path):
    transport, _ = github({
        f"{API}/commits?": (200, commits((NEW, "新"), (OLD, "旧"))),
        f"{RAW}/{NEW}/bot/__init__.py": (200, '__version__ = "9.9.9"'),
    })
    u = make(tmp_path, transport)
    assert (await u.check())["update_available"] is True
    # 宿主机完成更新，写入新版本
    (u.run_dir / "version.json").write_text(json.dumps({"commit": NEW}))
    assert (await u.check())["update_available"] is False  # 不能沿用更新前的缓存


async def test_unknown_current_version_shows_changes_and_flags_unknown(tmp_path):
    transport, _ = github({
        f"{API}/commits?": (200, commits((NEW, "新"))),
        f"{RAW}/{NEW}/bot/__init__.py": (200, '__version__ = "1.0.0"'),
    })
    r = await make(tmp_path, transport, commit=None).check()
    assert r["current_unknown"] is True and r["update_available"] is False
    assert r["recent"] == [{"commit": NEW[:7], "subject": "新", "date": "2026-11-08T10:00:00Z", "installed": None}]


async def test_errors_are_reported_not_raised(tmp_path):
    transport, _ = github({f"{API}/commits?": (403, "API rate limit exceeded")})
    r = await make(tmp_path, transport).check()
    assert "频率受限" in r["error"] and r["update_available"] is False
    other = tmp_path / "other"
    other.mkdir()
    transport, _ = github({})  # 所有请求都返回 404
    r = await make(other, transport).check()
    assert "找不到仓库" in r["error"]


async def test_request_update_flow(tmp_path):
    transport, _ = github({})
    u = make(tmp_path, transport)
    assert u.status()["state"] == "idle"
    st = u.request_update()
    assert st["state"] == "queued" and (u.run_dir / "update-request").exists()
    with pytest.raises(ValueError, match="已经在更新中"):
        u.request_update()
    # 宿主机开始处理：删除请求文件并写入状态
    (u.run_dir / "update-request").unlink()
    (u.run_dir / "update-status.json").write_text(json.dumps({"state": "running"}))
    with pytest.raises(ValueError):
        u.request_update()


async def test_request_update_requires_host_updater(tmp_path):
    transport, _ = github({})
    u = make(tmp_path, transport, host=False)
    with pytest.raises(ValueError, match="不支持网页更新"):
        u.request_update()


def test_invalid_repo_rejected():
    with pytest.raises(SystemExit):
        Updater("not a repo; rm -rf /", "main", "run")


def test_version_tuple():
    assert version_tuple("1.10.0") > version_tuple("1.9.9")
    assert version_tuple("2.0") > version_tuple("1.99.99")
