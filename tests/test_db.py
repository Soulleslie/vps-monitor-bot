import sqlite3

from bot.db import DB, load_meta


async def test_old_database_is_migrated_without_data_loss(tmp_path):
    path = tmp_path / "old.db"
    # 模拟旧版本数据库：sites / products 没有 meta 列
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sites (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            interval_sec INTEGER, probe_mode TEXT NOT NULL DEFAULT 'auto', enabled INTEGER NOT NULL DEFAULT 1,
            last_check INTEGER, last_ok INTEGER, fail_count INTEGER NOT NULL DEFAULT 0,
            fail_alerted INTEGER NOT NULL DEFAULT 0, last_error TEXT, parse_method TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            key TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL, qty INTEGER, price TEXT, order_url TEXT,
            description TEXT, present INTEGER NOT NULL DEFAULT 1, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
            in_stock_since INTEGER, last_notified_at INTEGER, UNIQUE (site_id, key));
        INSERT INTO sites (url, name, created_at) VALUES ('https://a.example/store/x', 'A', 1);
        INSERT INTO products (site_id, key, name, status, first_seen, last_seen) VALUES (1, 'pid:1', 'Plan', 'in_stock', 1, 1);
    """)
    con.commit()
    con.close()

    db = DB(str(path))
    await db.open()
    try:
        site = await db.get_site(1)
        product = await db.get_product(1)
        assert site["name"] == "A" and product["name"] == "Plan"   # 原有数据保留
        assert load_meta(site["meta"]) == {} and load_meta(product["meta"]) == {}
        await db.set_meta("products", 1, {"location": "LAX", "lines": "  "})
        assert load_meta((await db.get_product(1))["meta"]) == {"location": "LAX"}
    finally:
        await db.close()

    # 再次打开（已是新结构）不应报错
    db2 = DB(str(path))
    await db2.open()
    await db2.close()


def test_load_meta_tolerates_bad_data():
    assert load_meta(None) == {} and load_meta("not json") == {} and load_meta("[1,2]") == {}
