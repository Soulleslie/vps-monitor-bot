"""测试用的“假 mihomo”：读取配置，在 mixed-port 上监听，直到被结束。

环境变量 FAKE_MIHOMO_MODE：
  fail   —— 打印一行错误并立即退出（模拟节点配置错误）
  record —— 把收到的配置原样写到 FAKE_MIHOMO_RECORD 指定的文件
"""
import json
import os
import signal
import socket
import sys
import time

args = sys.argv[1:]
config_path = args[args.index("-f") + 1]
with open(config_path, encoding="utf-8") as fh:
    config = json.load(fh)

mode = os.environ.get("FAKE_MIHOMO_MODE", "")
if mode == "fail":
    print('time="2026-01-01" level=fatal msg="Parse config error: proxy 0: anytls: invalid password"', flush=True)
    sys.exit(1)
if mode == "record":
    with open(os.environ["FAKE_MIHOMO_RECORD"], "w", encoding="utf-8") as fh:
        json.dump({"config": config, "mode": oct(os.stat(config_path).st_mode & 0o777), "args": args}, fh)

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((config["bind-address"], config["mixed-port"]))
srv.listen(8)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print('time="2026-01-01" level=info msg="Mixed(http+socks) proxy listening at: 127.0.0.1"', flush=True)
srv.settimeout(0.5)
while True:
    try:
        conn, _ = srv.accept()
        conn.close()
    except socket.timeout:
        time.sleep(0.01)
