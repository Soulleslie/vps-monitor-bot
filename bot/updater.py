"""版本检查与网页一键更新。

程序运行在容器里，不能（也不应该）自己重建自己。更新流程：
1. 网页点击“立即更新” → 本模块在共享目录 RUN_DIR 写入 update-request 标记文件；
2. 宿主机上由 install.sh 注册的 systemd 路径单元（vps-restock-update.path）发现该文件，
   执行 `vps-restock update --from-web`（与命令行更新完全相同的流程）；
3. 宿主机把进度写入 update-status.json、把新版本信息写入 version.json，网页轮询显示。

网页只能触发“从配置的官方仓库更新到最新版”这一个固定动作，不能执行任何其他命令。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import __version__

log = logging.getLogger(__name__)

CACHE_TTL = 600            # 10 分钟；“检查更新”按钮会立即重新检查
RECENT_LIMIT = 15          # 后台显示的最近更新记录条数
REQUEST_FILE = "update-request"
STATUS_FILE = "update-status.json"
VERSION_FILE = "version.json"
HOST_FILE = "updater.json"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def _read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _brief(c: dict) -> dict:
    return {
        "commit": c["sha"][:7],
        "subject": c["commit"]["message"].splitlines()[0][:200],
        "date": c["commit"]["committer"]["date"],
    }


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+]", v or "0")[:4])


class Updater:
    def __init__(
        self,
        repo: str,
        branch: str,
        run_dir: str,
        proxy: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if repo and not _REPO_RE.match(repo):
            raise SystemExit(f"UPDATE_REPO 格式应为 owner/name，当前：{repo!r}")
        self.repo = repo
        self.branch = branch
        self.run_dir = Path(run_dir)
        self._proxy = proxy
        self._transport = transport
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._cache_key: tuple | None = None

    # ---------- 本地状态 ----------
    def current(self) -> dict:
        info = _read_json(self.run_dir / VERSION_FILE) or {}
        commit = str(info.get("commit") or os.getenv("GIT_COMMIT") or "").strip() or None
        if commit and not re.fullmatch(r"[0-9a-f]{7,40}", commit):
            commit = None
        return {"version": __version__, "commit": commit, "date": info.get("date"), "subject": info.get("subject")}

    def host_available(self) -> bool:
        info = _read_json(self.run_dir / HOST_FILE) or {}
        return bool(info.get("enabled")) and bool(self.repo)

    def status(self) -> dict:
        st = _read_json(self.run_dir / STATUS_FILE) or {"state": "idle"}
        if (self.run_dir / REQUEST_FILE).exists() and st.get("state") not in ("running",):
            st = {**st, "state": "queued"}
        if st.get("state") == "failed":
            try:
                lines = (self.run_dir / "update.log").read_text(encoding="utf-8", errors="replace").splitlines()
                st["log_tail"] = "\n".join(lines[-25:])
            except OSError:
                pass
        return st

    def request_update(self) -> dict:
        if not self.host_available():
            raise ValueError("当前安装方式不支持网页更新，请在服务器上运行 vps-restock update")
        if self.status().get("state") in ("queued", "running"):
            raise ValueError("已经在更新中，请稍候")
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.run_dir / (REQUEST_FILE + ".tmp")
            tmp.write_text(json.dumps({"requested_at": int(time.time())}), encoding="utf-8")
            tmp.replace(self.run_dir / REQUEST_FILE)  # 原子替换，避免宿主机读到半个文件
        except OSError as exc:
            raise ValueError(f"无法写入更新请求（{exc.strerror}），请在服务器运行 vps-restock install-units 修复") from None
        log.warning("已通过 Web 后台请求更新")
        return self.status()

    # ---------- 远程检查 ----------
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            proxy=self._proxy,
            transport=self._transport,
            headers={"User-Agent": f"vps-restock-bot/{__version__}", "Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )

    async def check(self, force: bool = False) -> dict:
        if not self.repo:
            return {"error": "未配置 UPDATE_REPO", "update_available": False}
        cur = self.current()
        cache_key = (cur["commit"], cur["version"])
        # 缓存与当前运行的版本绑定：更新完成后版本变化，缓存自动失效
        if not force and self._cache and self._cache_key == cache_key and time.time() - self._cache_at < CACHE_TTL:
            return self._cache
        result: dict[str, Any] = {
            "update_available": False, "commits": [], "recent": [], "ahead_by": 0,
            "current_unknown": cur["commit"] is None, "checked_at": int(time.time()),
        }
        try:
            async with self._client() as client:
                # 最近的提交（最新在前）：用于判断是否有更新，并在后台显示更新记录
                r = await client.get(
                    f"https://api.github.com/repos/{self.repo}/commits",
                    params={"sha": self.branch, "per_page": RECENT_LIMIT},
                )
                if r.status_code == 403 and "rate limit" in r.text.lower():
                    raise RuntimeError("GitHub 访问频率受限，请稍后再试")
                if r.status_code == 404:
                    raise RuntimeError(f"找不到仓库 {self.repo}（仓库是否公开？）")
                r.raise_for_status()
                commits = r.json()
                if not isinstance(commits, list) or not commits:
                    raise RuntimeError("仓库中没有提交记录")
                head = commits[0]
                latest_sha = head["sha"]
                result["latest"] = {**_brief(head), "commit": latest_sha, "version": None}
                raw = await client.get(f"https://raw.githubusercontent.com/{self.repo}/{latest_sha}/bot/__init__.py")
                if raw.status_code == 200:
                    m = _VERSION_RE.search(raw.text)
                    result["latest"]["version"] = m.group(1) if m else None

                cur_commit = cur["commit"]
                idx = None
                if cur_commit:
                    idx = next((i for i, c in enumerate(commits)
                                if c["sha"].startswith(cur_commit) or cur_commit.startswith(c["sha"])), None)
                if cur_commit is None:
                    # 无法确定当前版本：显示更新记录，并允许一键更新到最新版
                    result["recent"] = [{**_brief(c), "installed": None} for c in commits]
                elif idx is not None:
                    result["ahead_by"] = idx
                    result["update_available"] = idx > 0
                    result["commits"] = [_brief(c) for c in commits[:idx]]
                    result["recent"] = [{**_brief(c), "installed": i >= idx} for i, c in enumerate(commits)]
                else:
                    # 当前版本比最近这些提交都旧（或本地改动过）：用 compare 接口确认
                    result["recent"] = [{**_brief(c), "installed": False} for c in commits]
                    cmp = await client.get(f"https://api.github.com/repos/{self.repo}/compare/{cur_commit}...{latest_sha}")
                    if cmp.status_code == 200:
                        data = cmp.json()
                        result["ahead_by"] = int(data.get("ahead_by", 0))
                        result["update_available"] = data.get("status") in ("ahead", "diverged")
                        result["commits"] = [_brief(c) for c in reversed(data.get("commits", [])[-30:])]
                    else:  # 当前提交不在远程仓库中（例如本地修改过），以提交不同视为可更新
                        result["update_available"] = True
                        result["commits"] = [_brief(c) for c in commits]
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
            result["error"] = f"检查更新失败：{exc}"
        self._cache = result
        self._cache_key = cache_key
        # 失败的结果只缓存 2 分钟，便于很快重试
        self._cache_at = time.time() - (CACHE_TTL - 120 if "error" in result else 0)
        return result

    def invalidate(self) -> None:
        self._cache = None
