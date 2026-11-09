"""安装/卸载脚本中“接入方式”部分的测试。

用替身命令（caddy、nginx、certbot、systemctl、ss、docker）在临时目录里运行
scripts/proxy.sh 和 scripts/uninstall.sh 中的相关函数，不会改动本机的任何系统文件。
重点验证：卸载后，已有程序的配置文件与安装前逐字节一致，其他站点的证书不受影响。
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROXY_SH = ROOT / "scripts" / "proxy.sh"
UNINSTALL_SH = ROOT / "scripts" / "uninstall.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")

ORIGINAL_CADDYFILE = textwrap.dedent("""\
    {
    	email admin@other.example
    }

    other.example {
    	reverse_proxy 127.0.0.1:3000
    }
""")

STUBS = {
    # validate 时：CADDY_VALIDATE_FAIL=1 模拟检查失败；其余调用只记录
    "caddy": """#!/bin/bash
echo "caddy $*" >> "$SANDBOX/calls.log"
[ "$1" = validate ] && [ "${CADDY_VALIDATE_FAIL:-0}" = 1 ] && exit 1
exit 0
""",
    "systemctl": """#!/bin/bash
echo "systemctl $*" >> "$SANDBOX/calls.log"
[ "$1" = is-active ] && exit 0
exit 0
""",
    # nginx -T 输出模拟的完整配置；nginx -t 时 NGINX_TEST_FAIL=1 模拟检查失败
    "nginx": """#!/bin/bash
echo "nginx $*" >> "$SANDBOX/calls.log"
if [ "$1" = -T ]; then
  echo "# configuration file /etc/nginx/nginx.conf:"
  echo "http { include $NGINX_DIR/${NGINX_STYLE:-conf.d}/*${NGINX_STYLE_SUFFIX:-.conf}; }"
  echo "# configuration file $NGINX_DIR/conf.d/other.conf:"
  echo "server { listen 80; server_name other.example www.other.example; }"
  for f in "$NGINX_DIR"/conf.d/*.conf "$NGINX_DIR"/sites-enabled/*; do
    [ -f "$f" ] && { echo "# configuration file $f:"; cat "$f"; }
  done
  exit 0
fi
[ "$1" = -t ] && [ "${NGINX_TEST_FAIL:-0}" = 1 ] && exit 1
exit 0
""",
    # certonly：CERTBOT_FAIL=1 模拟申请失败；成功时生成证书目录和续期配置
    "certbot": """#!/bin/bash
echo "certbot $*" >> "$SANDBOX/calls.log"
if [ "$1" = certonly ]; then
  [ "${CERTBOT_FAIL:-0}" = 1 ] && exit 1
  while [ $# -gt 0 ]; do case "$1" in -d) d="$2"; shift;; -w) w="$2"; shift;; esac; shift; done
  mkdir -p "$LE_DIR/live/$d" "$LE_DIR/renewal"
  touch "$LE_DIR/live/$d/fullchain.pem" "$LE_DIR/live/$d/privkey.pem"
  printf 'webroot_path = %s\\n' "$w" > "$LE_DIR/renewal/$d.conf"
fi
if [ "$1" = delete ]; then
  rm -rf "$LE_DIR/live/$3" "$LE_DIR/renewal/$3.conf"
fi
exit 0
""",
    # ss：PORT_80 / PORT_443 指定占用端口的进程名
    "ss": """#!/bin/bash
args="$*"; p="${args##*:}"; v="PORT_$p"; n="${!v:-}"
[ -n "$n" ] && echo "LISTEN 0 4096 *:$p *:* users:((\\"$n\\",pid=1,fd=3))"
exit 0
""",
    "docker": "#!/bin/bash\nexit 0\n",
}

HELPERS = """
info() { echo "INFO $*"; }
ok() { echo "OK $*"; }
warn() { echo "WARN $*"; }
step() { echo "STEP $*"; }
kept() { echo "KEPT $*"; }
die() { echo "DIE $*"; exit 1; }
state_set() { printf '%s=%s\\n' "$1" "$2" >> "$SANDBOX/state"; }
install_pkgs() { echo "install_pkgs $*" >> "$SANDBOX/calls.log"; return 1; }
new_tmp() { REPLY="$(mktemp ${1:+"$1"} "$SANDBOX/tmp.XXXXXX")"; }
"""


@pytest.fixture
def sb(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        f = bin_dir / name
        f.write_text(body)
        f.chmod(0o755)
    (tmp_path / "etc" / "caddy").mkdir(parents=True)
    (tmp_path / "etc" / "caddy" / "Caddyfile").write_text(ORIGINAL_CADDYFILE)
    for d in ("conf.d", "sites-available", "sites-enabled"):
        (tmp_path / "etc" / "nginx" / d).mkdir(parents=True)
    (tmp_path / "le").mkdir()
    return tmp_path


def env_for(sb: Path, **extra) -> dict:
    env = {
        "PATH": f"{sb / 'bin'}:/usr/bin:/bin",
        "SANDBOX": str(sb),
        "TMPDIR": str(sb),
        "CADDY_FILE": str(sb / "etc/caddy/Caddyfile"),
        "CADDY_SNIPPET": str(sb / "etc/caddy/vps-restock.caddy"),
        "CADDY_DATA_DIRS": str(sb / "caddydata"),
        "NGINX_DIR": str(sb / "etc/nginx"),
        "ACME_ROOT": str(sb / "acme"),
        "LE_DIR": str(sb / "le"),
        "BT_PANEL_DIR": str(sb / "no-bt-panel"),
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


def run_proxy(sb: Path, code: str, **env) -> subprocess.CompletedProcess:
    script = f"set -Euo pipefail\n{HELPERS}\nsource '{PROXY_SH}'\n{code}\n"
    return subprocess.run(["bash", "-c", script], env=env_for(sb, **env), capture_output=True, text=True)


def run_uninstall_block(sb: Path, code: str, **env) -> subprocess.CompletedProcess:
    text = UNINSTALL_SH.read_text(encoding="utf-8")
    block = text[text.index("# >>> 接入方式清理"):text.index("# <<< 接入方式清理")]
    script = f"set -Euo pipefail\n{HELPERS}\n{block}\n{code}\n"
    return subprocess.run(["bash", "-c", script], env=env_for(sb, **env), capture_output=True, text=True)


def calls(sb: Path) -> str:
    f = sb / "calls.log"
    return f.read_text() if f.exists() else ""


# ---------------------------------------------------------------- 自动判断接入方式
@pytest.mark.parametrize("p80,p443,expected", [
    ("", "", "builtin"),
    ("caddy", "caddy", "caddy"),
    ("nginx", "nginx", "nginx"),
    ("apache2", "apache2", "manual"),
    ("", "haproxy", "manual"),
])
def test_detect_proxy_mode(sb, p80, p443, expected):
    r = run_proxy(sb, "detect_proxy_mode", PORT_80=p80, PORT_443=p443)
    assert r.stdout.strip() == expected, r.stderr


def test_bt_panel_is_always_manual(sb):
    (sb / "bt").mkdir()
    r = run_proxy(sb, "detect_proxy_mode", PORT_80="nginx", PORT_443="nginx", BT_PANEL_DIR=sb / "bt")
    assert r.stdout.strip() == "manual"


# ---------------------------------------------------------------- 已有 Caddy
def test_caddy_install_then_uninstall_restores_original_exactly(sb):
    caddyfile = sb / "etc/caddy/Caddyfile"
    snippet = sb / "etc/caddy/vps-restock.caddy"
    r = run_proxy(sb, "setup_caddy_site bot.example.com 18080")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "bot.example.com {" in snippet.read_text() and "reverse_proxy 127.0.0.1:18080" in snippet.read_text()
    assert caddyfile.read_text() == ORIGINAL_CADDYFILE + f"\nimport {snippet} # vps-restock-bot\n"
    assert "PROXY_MODE=caddy" in (sb / "state").read_text()
    assert "systemctl reload caddy" in calls(sb)

    # 再次安装（重装/更新）不会重复添加 import 行
    assert run_proxy(sb, "setup_caddy_site bot.example.com 18080").returncode == 0
    assert caddyfile.read_text().count("vps-restock-bot") == 1

    # Caddy 为两个域名申请的证书
    for dom in ("bot.example.com", "other.example"):
        (sb / f"caddydata/certificates/acme-v02/{dom}").mkdir(parents=True)
    r = run_uninstall_block(sb, "remove_caddy_site")
    assert r.returncode == 0, r.stdout + r.stderr
    assert caddyfile.read_text() == ORIGINAL_CADDYFILE          # 与安装前逐字节一致
    assert not snippet.exists()
    assert not (sb / "caddydata/certificates/acme-v02/bot.example.com").exists()
    assert (sb / "caddydata/certificates/acme-v02/other.example").exists()   # 其他站点证书保留


def test_caddy_validate_failure_rolls_back(sb):
    caddyfile = sb / "etc/caddy/Caddyfile"
    r = run_proxy(sb, "setup_caddy_site bot.example.com 18080", CADDY_VALIDATE_FAIL=1)
    assert r.returncode != 0 and "已恢复原配置" in r.stdout
    assert caddyfile.read_text() == ORIGINAL_CADDYFILE
    assert not (sb / "etc/caddy/vps-restock.caddy").exists()
    assert "reload" not in calls(sb)


def test_caddy_backup_failure_changes_nothing(sb):
    # 模拟无法创建备份文件
    r = run_proxy(sb, 'new_tmp() { REPLY=""; }\nsetup_caddy_site bot.example.com 18080')
    assert r.returncode != 0 and "无法备份" in r.stdout
    assert (sb / "etc/caddy/Caddyfile").read_text() == ORIGINAL_CADDYFILE
    assert not (sb / "etc/caddy/vps-restock.caddy").exists()
    assert calls(sb) == ""


def test_caddy_never_overwrites_foreign_file(sb):
    foreign = sb / "etc/caddy/vps-restock.caddy"
    foreign.write_text("someone.else { respond hi }\n")
    r = run_proxy(sb, "setup_caddy_site bot.example.com 18080")
    assert r.returncode != 0 and "不是本程序创建的" in r.stdout
    assert foreign.read_text() == "someone.else { respond hi }\n"
    # 卸载也不会删除别人的同名文件
    run_uninstall_block(sb, "remove_caddy_site")
    assert foreign.exists()
    assert (sb / "etc/caddy/Caddyfile").read_text() == ORIGINAL_CADDYFILE


def test_caddy_uninstall_keeps_config_if_validation_fails(sb):
    assert run_proxy(sb, "setup_caddy_site bot.example.com 18080").returncode == 0
    installed = (sb / "etc/caddy/Caddyfile").read_text()
    r = run_uninstall_block(sb, "remove_caddy_site", CADDY_VALIDATE_FAIL=1)
    assert "KEPT" in r.stdout and "已恢复原样" in r.stdout
    assert (sb / "etc/caddy/Caddyfile").read_text() == installed
    assert (sb / "etc/caddy/vps-restock.caddy").exists()


# ---------------------------------------------------------------- 已有 nginx
def test_nginx_install_then_uninstall(sb):
    conf = sb / "etc/nginx/conf.d/vps-restock.conf"
    # 另一个网站已有的证书（不是本项目申请的）
    (sb / "le/live/other.example").mkdir(parents=True)
    (sb / "le/renewal").mkdir()
    (sb / "le/renewal/other.example.conf").write_text("webroot_path = /var/www/other\n")

    r = run_proxy(sb, "setup_nginx_site bot.example.com a@b.c 18080")
    assert r.returncode == 0, r.stdout + r.stderr
    text = conf.read_text()
    assert "server_name bot.example.com;" in text and "proxy_pass http://127.0.0.1:18080;" in text
    assert "listen 443 ssl;" in text and f"{sb}/le/live/bot.example.com/fullchain.pem" in text
    assert "X-Forwarded-Proto https" in text
    state = (sb / "state").read_text()
    assert "CERTBOT_CERT=bot.example.com" in state and "PROXY_MODE=nginx" in state
    assert "certbot certonly --webroot" in calls(sb)

    r = run_uninstall_block(sb, "remove_nginx_site")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not conf.exists()
    assert "certbot delete --cert-name bot.example.com" in calls(sb)
    assert "certbot delete --cert-name other.example" not in calls(sb)
    assert (sb / "le/live/other.example").exists()                     # 其他站点证书保留
    assert not (sb / "acme").exists()


def test_nginx_sites_enabled_style(sb):
    r = run_proxy(sb, "setup_nginx_site bot.example.com a@b.c 18080", NGINX_STYLE="sites-enabled", NGINX_STYLE_SUFFIX="")
    assert r.returncode == 0, r.stdout + r.stderr
    link = sb / "etc/nginx/sites-enabled/vps-restock.conf"
    assert link.is_symlink() and (sb / "etc/nginx/sites-available/vps-restock.conf").exists()
    run_uninstall_block(sb, "remove_nginx_site")
    assert not link.exists() and not link.is_symlink()
    assert not (sb / "etc/nginx/sites-available/vps-restock.conf").exists()


def test_nginx_refuses_domain_used_by_other_site(sb):
    r = run_proxy(sb, "setup_nginx_site www.other.example a@b.c 18080")
    assert r.returncode != 0 and "已在现有 nginx 配置中使用" in r.stdout
    assert not (sb / "etc/nginx/conf.d/vps-restock.conf").exists()
    assert "certbot" not in calls(sb)


def test_nginx_certbot_failure_rolls_back(sb):
    r = run_proxy(sb, "setup_nginx_site bot.example.com a@b.c 18080", CERTBOT_FAIL=1)
    assert r.returncode != 0 and "已恢复原配置" in r.stdout
    assert not (sb / "etc/nginx/conf.d/vps-restock.conf").exists()
    assert not (sb / "state").exists() or "CERTBOT_CERT" not in (sb / "state").read_text()


def test_nginx_config_test_failure_rolls_back(sb):
    r = run_proxy(sb, "setup_nginx_site bot.example.com a@b.c 18080", NGINX_TEST_FAIL=1)
    assert r.returncode != 0 and "已恢复原配置" in r.stdout
    assert not (sb / "etc/nginx/conf.d/vps-restock.conf").exists()
    assert "certbot" not in calls(sb)


def test_nginx_uninstall_ignores_foreign_files(sb):
    foreign = sb / "etc/nginx/conf.d/vps-restock.conf"
    foreign.write_text("server { server_name mine.example; }\n")
    r = run_uninstall_block(sb, "remove_nginx_site")
    assert r.returncode == 0 and foreign.exists()
    assert "nginx" not in calls(sb)


# ---------------------------------------------------------------- docker compose 覆盖文件
def test_compose_override_disables_builtin_caddy(sb):
    r = run_proxy(sb, f"write_compose_override '{sb}' 18081")
    assert r.returncode == 0
    text = (sb / "docker-compose.override.yml").read_text()
    assert 'profiles: ["disabled"]' in text and '"127.0.0.1:18081:8080"' in text
    assert "vps-restock-bot" in text   # 带标记，卸载/重装时可识别
