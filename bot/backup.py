"""配置导出 / 导入：换服务器时一键迁移监控网址、推送对象、订阅和设置。

导出内容不含库存历史和登录信息。Bot Token 和推送渠道密钥默认不导出，勾选后才包含。
导入是“合并”：已有的网址和推送对象会被更新，没有的会新增，不会删除现有的任何内容。
新导入的网址第一次检查只记录当前库存，不会推送。
"""
from __future__ import annotations

import json
from typing import Callable

from .channels import CHANNEL_ID_BASE, CHANNELS, ChannelError, parse_config
from .config import Config
from .db import DB, load_meta, now
from .fetcher import UnsafeURLError, normalize_url

FORMAT = "vps-restock-backup"
VERSION = 1
SETTING_KEYS = ("check_interval", "notify_cooldown", "fail_alert_threshold", "buy_button")
CHAT_FLAGS = ("paused", "notify_soldout", "notify_new", "notify_price")
MAX_SITES = 500
MAX_CHATS = 500
MAX_SUBS = 20000

MetaCleaner = Callable[[dict], dict]


async def export_config(db: DB, cfg: Config, include_secrets: bool) -> dict:
    sites = []
    url_of: dict[int, str] = {}
    for s in await db.fetchall("SELECT * FROM sites ORDER BY id"):
        url_of[s["id"]] = s["url"]
        products = [
            {"key": p["key"], "name": p["name"], "meta": load_meta(p["meta"])}
            for p in await db.site_products(s["id"]) if load_meta(p["meta"])
        ]
        sites.append({
            "url": s["url"], "name": s["name"], "interval_sec": s["interval_sec"], "probe_mode": s["probe_mode"],
            "enabled": bool(s["enabled"]), "meta": load_meta(s["meta"]), "products": products,
        })
    chats = []
    for c in await db.fetchall("SELECT * FROM chats ORDER BY created_at"):
        item = {
            "chat_id": c["chat_id"], "title": c["title"], "chat_type": c["chat_type"], "channel": c["channel"],
            **{f: bool(c[f]) for f in CHAT_FLAGS},
        }
        if c["channel"] != "telegram" and include_secrets:
            item["config"] = load_meta(c["config"])
        chats.append(item)
    subs = [
        {"chat_id": r["chat_id"], "site_url": url_of[r["site_id"]], "kind": r["kind"], "value": r["value"]}
        for r in await db.fetchall("SELECT * FROM subscriptions ORDER BY id") if r["site_id"] in url_of
    ]
    attached = [
        {"chat_id": r["chat_id"], "site_url": url_of[r["site_id"]]}
        for r in await db.fetchall("SELECT * FROM chat_sites") if r["site_id"] in url_of
    ]
    settings = {k: getattr(cfg, k) for k in SETTING_KEYS}
    if include_secrets and cfg.bot_token:
        settings["bot_token"] = cfg.bot_token
    return {
        "format": FORMAT, "version": VERSION, "exported_at": now(), "includes_secrets": include_secrets,
        "settings": settings, "sites": sites, "chats": chats, "subscriptions": subs, "chat_sites": attached,
    }


def _list(data: dict, key: str, limit: int) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"备份文件格式错误：{key} 应为列表")
    if len(value) > limit:
        raise ValueError(f"备份文件中的 {key} 太多（最多 {limit} 个）")
    if not all(isinstance(v, dict) for v in value):
        raise ValueError(f"备份文件格式错误：{key}")
    return value


async def import_config(
    db: DB, cfg: Config, data: dict, clean_site_meta: MetaCleaner, clean_product_meta: MetaCleaner,
) -> dict:
    """导入备份，返回统计结果。先完整校验，任何一项不合格都不做改动。"""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("这不是本程序导出的备份文件")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ValueError("备份文件来自更新的版本，请先把本程序更新到最新版本")

    # ---------- 1. 校验 ----------
    raw_settings = data.get("settings") or {}
    if not isinstance(raw_settings, dict):
        raise ValueError("备份文件格式错误：settings")
    settings = {k: raw_settings[k] for k in (*SETTING_KEYS, "bot_token") if k in raw_settings}
    if not settings.get("bot_token"):
        settings.pop("bot_token", None)
    settings = cfg.validate_update(settings)

    sites = []
    for s in _list(data, "sites", MAX_SITES):
        try:
            url = normalize_url(str(s.get("url", "")))
        except UnsafeURLError as exc:
            raise ValueError(f"网址无效：{s.get('url')}（{exc}）") from None
        interval = s.get("interval_sec")
        if interval is not None and (not isinstance(interval, int) or interval < 5):
            raise ValueError(f"检查间隔无效：{url}")
        if s.get("probe_mode", "auto") not in ("auto", "off"):
            raise ValueError(f"probe_mode 无效：{url}")
        products = []
        for p in s.get("products") or []:
            if not isinstance(p, dict) or not isinstance(p.get("key"), str) or not p["key"] or len(p["key"]) > 300:
                raise ValueError(f"机型数据无效：{url}")
            products.append({"key": p["key"], "name": str(p.get("name") or p["key"])[:200],
                             "meta": clean_product_meta(p.get("meta") or {})})
        sites.append({
            "url": url, "name": (str(s.get("name") or "").strip() or url)[:60], "interval_sec": interval,
            "probe_mode": s.get("probe_mode", "auto"), "enabled": 1 if s.get("enabled", True) else 0,
            "meta": clean_site_meta(s.get("meta") or {}), "products": products,
        })

    chats = []
    skipped_channels = 0
    for c in _list(data, "chats", MAX_CHATS):
        if not isinstance(c.get("chat_id"), int):
            raise ValueError("推送对象数据无效：chat_id")
        channel = c.get("channel", "telegram")
        if channel not in CHANNELS:
            raise ValueError(f"未知的推送渠道：{channel}")
        item = {
            "old_id": c["chat_id"], "channel": channel, "title": str(c.get("title") or "")[:100] or None,
            "chat_type": str(c.get("chat_type") or "")[:20] or None,
            "flags": {f: 1 if c.get(f) else 0 for f in CHAT_FLAGS if f in c},
        }
        if channel == "telegram":
            if c["chat_id"] >= CHANNEL_ID_BASE:
                raise ValueError("推送对象数据无效：chat_id")
        else:
            config = c.get("config")
            if not isinstance(config, dict):
                skipped_channels += 1      # 导出时没有勾选包含密钥：无法恢复这个渠道
                continue
            try:
                # 用与网页添加时相同的规则重新校验
                raw = {"bark": lambda x: f"{x.get('server', '')}/{x.get('key', '')}",
                       "wecom": lambda x: x.get("url", ""), "webhook": lambda x: x.get("url", ""),
                       "pushplus": lambda x: x.get("token", "")}[channel](config)
                item["config"] = parse_config(channel, str(raw))
            except (ChannelError, AttributeError) as exc:
                raise ValueError(f"推送渠道配置无效：{exc}") from None
        chats.append(item)

    subs = _list(data, "subscriptions", MAX_SUBS)
    attached = _list(data, "chat_sites", MAX_SUBS)
    for r in subs:
        if r.get("kind") not in ("product", "keyword", "all") or not isinstance(r.get("value", ""), str):
            raise ValueError("订阅数据无效")

    # ---------- 2. 写入 ----------
    ts = now()
    site_ids: dict[str, int] = {}
    added_sites = 0
    for s in sites:
        row = await db.fetchone("SELECT id FROM sites WHERE url = ?", (s["url"],))
        meta = json.dumps(s["meta"], ensure_ascii=False)
        if row:
            sid = row["id"]
            await db.conn.execute(
                "UPDATE sites SET name = ?, interval_sec = ?, probe_mode = ?, enabled = ?, meta = ? WHERE id = ?",
                (s["name"], s["interval_sec"], s["probe_mode"], s["enabled"], meta, sid),
            )
        else:
            cur = await db.conn.execute(
                "INSERT INTO sites (url, name, interval_sec, probe_mode, enabled, meta, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (s["url"], s["name"], s["interval_sec"], s["probe_mode"], s["enabled"], meta, ts),
            )
            sid = cur.lastrowid
            added_sites += 1
        site_ids[s["url"]] = sid
        for p in s["products"]:
            pm = json.dumps(p["meta"], ensure_ascii=False)
            # 机型还没检查到时先占位（状态未知、不在页面上），第一次检查后自动更新，不会触发推送
            await db.conn.execute(
                "INSERT INTO products (site_id, key, name, status, present, first_seen, last_seen, meta) "
                "VALUES (?, ?, ?, 'unknown', 0, ?, ?, ?) ON CONFLICT(site_id, key) DO UPDATE SET meta = excluded.meta",
                (sid, p["key"], p["name"], ts, ts, pm),
            )

    chat_ids: dict[int, int] = {}
    added_chats = 0
    for c in chats:
        if c["channel"] == "telegram":
            new_id = c["old_id"]
            exists = await db.get_chat(new_id)
            await db.conn.execute(
                "INSERT INTO chats (chat_id, title, chat_type, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = COALESCE(excluded.title, title), "
                "chat_type = COALESCE(excluded.chat_type, chat_type)",
                (new_id, c["title"] or str(new_id), c["chat_type"], ts),
            )
        else:
            config = json.dumps(c["config"], ensure_ascii=False)
            same = await db.fetchone("SELECT chat_id FROM chats WHERE channel = ? AND config = ?", (c["channel"], config))
            exists = same
            if same:
                new_id = same["chat_id"]
            else:
                top = await db.fetchone("SELECT MAX(chat_id) AS m FROM chats WHERE chat_id >= ?", (CHANNEL_ID_BASE,))
                new_id = top["m"] + 1 if top and top["m"] else CHANNEL_ID_BASE + 1
                await db.conn.execute(
                    "INSERT INTO chats (chat_id, title, chat_type, channel, config, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id, c["title"] or CHANNELS[c["channel"]], c["channel"], c["channel"], config, ts),
                )
        if not exists:
            added_chats += 1
        for flag, value in c["flags"].items():
            await db.conn.execute(f"UPDATE chats SET {flag} = ? WHERE chat_id = ?", (value, new_id))
        chat_ids[c["old_id"]] = new_id

    sub_count = 0
    for r in [*attached, *subs]:
        chat_id, site_id = chat_ids.get(r.get("chat_id")), site_ids.get(r.get("site_url"))
        if chat_id is None or site_id is None:
            continue
        await db.conn.execute(
            "INSERT OR IGNORE INTO chat_sites (chat_id, site_id, created_at) VALUES (?, ?, ?)", (chat_id, site_id, ts)
        )
        if "kind" in r:
            value = r.get("value", "")[:300]
            cur = await db.conn.execute(
                "INSERT OR IGNORE INTO subscriptions (chat_id, site_id, kind, value, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, site_id, r["kind"], value, ts),
            )
            sub_count += cur.rowcount
    await db.conn.commit()

    if settings:
        await db.save_settings(settings)
    return {
        "sites": len(sites), "added_sites": added_sites, "chats": len(chats), "added_chats": added_chats,
        "subscriptions": sub_count, "skipped_channels": skipped_channels, "settings": settings,
    }
