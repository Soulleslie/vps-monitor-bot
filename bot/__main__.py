"""入口。

  python -m bot                    启动（机器人 + 监控 + Web 后台）
  python -m bot reset-web-token    重新生成 Web 后台访问令牌
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .auth import WebAuth
from .channels import Notifier
from .config import Config, load_dotenv
from .db import DB
from .fetcher import Fetcher
from .monitor import Monitor
from .runner import BotRunner

log = logging.getLogger("bot")


async def reset_web_token(cfg: Config) -> None:
    db = DB(cfg.db_path)
    await db.open()
    try:
        auth = WebAuth(cfg, db)
        await auth.init()  # 先同步 .env 状态，避免新令牌在下次启动时被 .env 覆盖
        token = await auth.reset_admin_token()
    finally:
        await db.close()
    print(f"新的 Web 后台访问令牌：\n  {token}\n旧令牌已失效，所有已登录的浏览器都需要重新登录。")
    print("如果程序正在运行，请重启使其生效（Docker：docker compose restart bot）。")


async def serve(cfg: Config) -> None:
    db = DB(cfg.db_path)
    await db.open()
    cfg.apply_overrides(await db.get_settings())

    fetcher = Fetcher(cfg.user_agent, cfg.request_timeout, cfg.fetch_proxy, cfg.host_min_gap)
    runner = BotRunner(cfg, db)
    notifier = Notifier(db, runner.send, runner.edit)   # Telegram 及 Bark / 企业微信 / PushPlus / Webhook
    monitor = Monitor(cfg, db, fetcher, notifier.send, notifier.edit)
    auth = WebAuth(cfg, db)
    await auth.init()
    runner.shared.update(cfg=cfg, db=db, monitor=monitor)

    if cfg.bot_token:
        await runner.start()
    else:
        log.warning("尚未设置 Bot Token，请登录 Web 后台在“设置”中填写")

    monitor.start()

    web_runner = None
    if cfg.web_enabled:
        from .web import create_app, start_web

        web_runner = await start_web(create_app(cfg, db, monitor, runner, auth, notifier=notifier), cfg.web_host, cfg.web_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await stop.wait()
    finally:
        log.info("正在退出…")
        if web_runner:
            await web_runner.cleanup()
        await monitor.stop()
        await runner.stop()
        await fetcher.close()
        await notifier.close()
        await db.close()


def main() -> None:
    load_dotenv()
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 避免日志中打印带 token 的请求地址
    if len(sys.argv) > 1 and sys.argv[1] == "reset-web-token":
        asyncio.run(reset_web_token(cfg))
        return
    if len(sys.argv) > 1:
        raise SystemExit(f"未知命令：{sys.argv[1]}\n用法：python -m bot [reset-web-token]")
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
