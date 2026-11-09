"""配置：环境变量提供初始值，Web 后台保存的设置（存于数据库）优先。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"环境变量 {name} 必须是整数，当前值: {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 读取：已存在的环境变量优先，不覆盖。"""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)


# 检查间隔的技术下限（秒）：一次抓取本身需要 1～3 秒，再短只会连续不停地请求，没有意义
INTERVAL_FLOOR = 5

# 可以在 Web 后台修改的设置项：名称 -> 类型
EDITABLE = {
    "bot_token": str,
    "check_interval": int,
    "notify_cooldown": int,
    "fail_alert_threshold": int,
    "buy_button": int,           # 推送消息下方附带“立即购买”按钮：1 开启 / 0 关闭
}


@dataclass
class Config:
    bot_token: str = ""
    check_interval: int = 60
    concurrency: int = 5
    host_min_gap: float = 3.0
    notify_cooldown: int = 120
    fail_alert_threshold: int = 5
    buy_button: int = 1
    request_timeout: float = 20.0
    fetch_proxy: str | None = None
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )
    db_path: str = "data/bot.db"
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Shanghai"))
    log_level: str = "INFO"
    # Web 后台
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_admin_token: str = ""     # 为空时首次启动自动生成
    trust_proxy: bool = False     # 是否信任 X-Forwarded-For（仅在反向代理之后开启）
    # 版本更新
    update_repo: str = "Soulleslie/vps-monitor-bot"
    update_branch: str = "main"
    run_dir: str = "run"          # 与宿主机共享的目录（更新请求与状态）

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if token.startswith("123456:"):
            token = ""  # 示例值视为未填写
        cfg = cls(
            bot_token=token,
            check_interval=max(INTERVAL_FLOOR, _int("CHECK_INTERVAL", 60)),
            concurrency=max(1, _int("CONCURRENCY", 5)),
            host_min_gap=float(max(0, _int("HOST_MIN_GAP", 3))),
            notify_cooldown=max(0, _int("NOTIFY_COOLDOWN", 120)),
            fail_alert_threshold=max(1, _int("FAIL_ALERT_THRESHOLD", 5)),
            request_timeout=float(max(5, _int("REQUEST_TIMEOUT", 20))),
            fetch_proxy=os.getenv("FETCH_PROXY", "").strip() or None,
            user_agent=os.getenv("USER_AGENT", "").strip() or cls.user_agent,
            db_path=os.getenv("DB_PATH", "data/bot.db").strip(),
            tz=ZoneInfo(os.getenv("TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            web_enabled=_bool("WEB_ENABLED", True),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip(),
            web_port=_int("WEB_PORT", 8080),
            web_admin_token=os.getenv("WEB_ADMIN_TOKEN", "").strip(),
            trust_proxy=_bool("TRUST_PROXY", False),
            update_repo=os.getenv("UPDATE_REPO", cls.update_repo).strip(),
            update_branch=os.getenv("UPDATE_BRANCH", "main").strip() or "main",
            run_dir=os.getenv("RUN_DIR", "run").strip() or "run",
        )
        if not cfg.bot_token and not cfg.web_enabled:
            raise SystemExit("未设置 BOT_TOKEN，且 Web 后台已关闭，无法配置机器人")
        return cfg

    def apply_overrides(self, values: dict[str, str]) -> None:
        """应用数据库中保存的设置（字符串形式）。"""
        for key, raw in values.items():
            kind = EDITABLE.get(key)
            if kind is None:
                continue
            if kind is int:
                setattr(self, key, int(raw))
            else:
                setattr(self, key, raw)
        self.check_interval = max(INTERVAL_FLOOR, self.check_interval)

    def validate_update(self, data: dict) -> dict[str, str]:
        """校验 Web 提交的设置，返回要保存的字符串值；有错误抛 ValueError。"""
        out: dict[str, str] = {}
        merged = {k: getattr(self, k) for k in EDITABLE}
        for key, value in data.items():
            if key not in EDITABLE:
                raise ValueError(f"未知设置项：{key}")
            kind = EDITABLE[key]
            if kind is int:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是整数") from None
                if value < 0:
                    raise ValueError(f"{key} 不能为负数")
                merged[key] = value
                out[key] = str(value)
            else:
                value = str(value).strip()
                if key == "bot_token" and value and ":" not in value:
                    raise ValueError("Bot Token 格式不正确")
                merged[key] = value
                out[key] = value
        if merged["check_interval"] < INTERVAL_FLOOR:
            raise ValueError(f"检查间隔不能小于 {INTERVAL_FLOOR} 秒")
        if merged["buy_button"] not in (0, 1):
            raise ValueError("buy_button 只能是 0 或 1")
        if merged["fail_alert_threshold"] < 1:
            raise ValueError("失败提醒阈值至少为 1")
        return out
