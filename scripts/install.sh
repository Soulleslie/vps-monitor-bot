#!/usr/bin/env bash
# =============================================================================
#  VPS 补货监控 —— 一键安装（Docker + Caddy 自动申请/续期 HTTPS 证书）
#
#  交互式安装：
#    bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/vps-monitor-bot/main/scripts/install.sh)
#
#  免交互安装：
#    bash install.sh --domain bot.example.com --email you@example.com [--bot-token xxx] -y
#
#  支持：Debian 10+ / Ubuntu 20.04+ / CentOS Stream / Rocky / AlmaLinux / Fedora（需 root）
# =============================================================================
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/Soulleslie/vps-monitor-bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vps-restock-bot}"
CLI_PATH="/usr/local/bin/vps-restock"
STATE_FILE="$INSTALL_DIR/.install-state"

DOMAIN=""
EMAIL=""
BOT_TOKEN_IN=""
ASSUME_YES=0
SKIP_DNS_CHECK=0
PROXY_ARG=auto

# ---------------------------------------------------------------- 输出工具
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_RST=""
fi
step() { printf '\n%s==> %s%s\n' "$C_BLU$C_BLD" "$*" "$C_RST"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✔ %s%s\n' "$C_GRN" "$*" "$C_RST"; }
warn() { printf '    %s! %s%s\n' "$C_YLW" "$*" "$C_RST" >&2; }
die()  { printf '\n%s✘ %s%s\n' "$C_RED" "$*" "$C_RST" >&2; exit 1; }
trap 'die "第 $LINENO 行执行失败：$BASH_COMMAND"' ERR

# 临时文件统一用 vps-restock. 前缀；无论成功、失败还是 Ctrl+C 中断，退出时都会删除
TMP_PATHS=()
new_tmp() {  # new_tmp [-d]：创建临时文件/目录，把路径存入变量 REPLY
  REPLY="$(mktemp "$@" "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"
  TMP_PATHS+=("$REPLY")
}
cleanup_tmp() {
  local p
  for p in "${TMP_PATHS[@]:-}"; do
    [ -n "$p" ] && rm -rf -- "$p"
  done
  return 0
}
trap cleanup_tmp EXIT
trap 'exit 130' INT TERM

usage() {
  cat <<EOF
用法：install.sh [选项]

  --domain <域名>        已解析到本服务器的域名，例如 bot.example.com
  --email <邮箱>         Let's Encrypt 账号邮箱（证书到期提醒）
  --bot-token <token>    Telegram Bot Token（可选，也可以装好后在网页里填写）
  --dir <目录>           安装目录，默认 /opt/vps-restock-bot
  --skip-dns-check       跳过“域名是否解析到本机”的检查（使用 CDN 代理时）
  --proxy <方式>         HTTPS 接入方式：auto（默认，自动判断）/ builtin（自带 Caddy）/
                         caddy（已有 Caddy）/ nginx（已有 nginx）/ manual（只开本机端口，自行配置）
  -y, --yes              免交互，所有确认默认“是”
  -h, --help             显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --bot-token) BOT_TOKEN_IN="${2:-}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:-}"; STATE_FILE="$INSTALL_DIR/.install-state"; shift 2 ;;
    --skip-dns-check) SKIP_DNS_CHECK=1; shift ;;
    --proxy) PROXY_ARG="${2:-}"; shift 2 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "未知参数：$1" ;;
  esac
done

# 从终端读取输入（兼容 curl | bash 方式运行）
prompt() {
  local text="$1" default="${2:-}" answer=""
  if [ "$ASSUME_YES" = 1 ] || [ ! -r /dev/tty ]; then
    printf '%s' "$default"; return
  fi
  if [ -n "$default" ]; then
    read -r -p "    $text [$default]: " answer </dev/tty || true
  else
    read -r -p "    $text: " answer </dev/tty || true
  fi
  printf '%s' "${answer:-$default}"
}
# 规范化域名/邮箱输入：先把中文输入法常见的全角符号换成半角，再删除所有非 ASCII 字节。
# 后者用于清除“输入中文后按退格只删掉一个字节”留下的残缺字符，以及零宽字符等不可见内容；
# 域名和 Let's Encrypt 邮箱本来就只能是 ASCII。
normalize_input() {
  printf '%s' "$1" \
    | LC_ALL=C sed -e 's/＠/@/g; s/．/./g; s/。/./g; s/：/:/g; s/／/\//g' \
    | LC_ALL=C tr -cd '\041-\176'
}
confirm() {
  local text="$1" default="${2:-Y}" answer
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -r /dev/tty ] || return 0
  read -r -p "    $text [$( [ "$default" = Y ] && echo Y/n || echo y/N )]: " answer </dev/tty || true
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy] ]]
}

# 写入/替换 .env 中的一项（值通过环境变量传给 awk，避免特殊字符被误解析）
set_env() {
  local key="$1" value="$2" file="$INSTALL_DIR/.env" tmp
  new_tmp; tmp="$REPLY"   # 内容含令牌：mktemp 创建的文件权限为 600，且退出时一定会被删除
  KEY="$key" VAL="$value" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"]; done = 0 }
    index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" >"$tmp"
  cat "$tmp" >"$file"
  rm -f "$tmp"
}
get_env() {
  local key="$1" file="$INSTALL_DIR/.env"
  [ -f "$file" ] || return 0
  KEY="$key" awk 'index($0, ENVIRON["KEY"] "=") == 1 { sub(/^[^=]*=/, ""); print; exit }' "$file"
}
state_set() { printf '%s=%s\n' "$1" "$2" >>"$STATE_FILE"; }
state_get() { [ -f "$STATE_FILE" ] && awk -F= -v k="$1" '$1 == k { v = $2 } END { print v }' "$STATE_FILE" || true; }

# ---------------------------------------------------------------- 1. 环境检查
step "检查系统环境"
[ "$(id -u)" -eq 0 ] || die "请使用 root 运行（或 sudo -i 后再运行）"
[ -r /etc/os-release ] || die "无法识别系统版本"
# shellcheck disable=SC1091
. /etc/os-release
if command -v apt-get >/dev/null 2>&1; then
  PKG=apt
elif command -v dnf >/dev/null 2>&1; then
  PKG=dnf
elif command -v yum >/dev/null 2>&1; then
  PKG=yum
else
  die "不支持的系统：${PRETTY_NAME:-unknown}（需要 apt / dnf / yum）"
fi
ok "${PRETTY_NAME:-$ID}，包管理器 $PKG"

case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ok "架构 $(uname -m)" ;;
  *) die "不支持的 CPU 架构：$(uname -m)" ;;
esac

mem_mb=$(awk '/MemTotal/ { printf "%d", $2 / 1024 }' /proc/meminfo)
[ "$mem_mb" -ge 400 ] || warn "内存只有 ${mem_mb}MB，构建镜像可能失败，建议至少 512MB 或添加 swap"

pkg_installed() {
  if [ "$PKG" = apt ]; then
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
  else
    rpm -q "$1" >/dev/null 2>&1
  fi
}
# 安装软件包，并把“原本没有、由本脚本安装的”包记录到安装状态文件，卸载时据此还原
install_pkgs() {
  local p new=()
  for p in "$@"; do pkg_installed "$p" || new+=("$p"); done
  [ ${#new[@]} -gt 0 ] || return 0
  if [ "$PKG" = apt ]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${new[@]}" >/dev/null
  else
    "$PKG" install -y -q "${new[@]}" >/dev/null
  fi
  mkdir -p "$INSTALL_DIR"
  for p in "${new[@]}"; do
    pkg_installed "$p" && state_set PKG_INSTALLED "$p"
  done
}
missing=()
for bin in curl git openssl awk; do command -v "$bin" >/dev/null 2>&1 || missing+=("$bin"); done
command -v ss >/dev/null 2>&1 || missing+=("iproute2")
if [ ${#missing[@]} -gt 0 ]; then
  pkgs=()
  for m in "${missing[@]}"; do
    case "$m" in
      awk) pkgs+=(gawk) ;;
      iproute2) [ "$PKG" = apt ] && pkgs+=(iproute2) || pkgs+=(iproute) ;;
      *) pkgs+=("$m") ;;
    esac
  done
  info "安装依赖：${pkgs[*]}"
  install_pkgs ca-certificates "${pkgs[@]}"
fi
ok "基础工具已就绪"

# ---------------------------------------------------------------- 2. 收集配置
step "填写配置"
EXISTING_ENV=0
if [ -f "$INSTALL_DIR/.env" ]; then
  EXISTING_ENV=1
  info "检测到已有安装（$INSTALL_DIR），将保留原有配置和数据，仅更新你修改的项目"
  [ -n "$DOMAIN" ] || DOMAIN="$(get_env DOMAIN)"
  [ -n "$EMAIL" ] || EMAIL="$(get_env ACME_EMAIL)"
fi
[ "$DOMAIN" = "bot.example.com" ] && DOMAIN=""
[ "$EMAIL" = "you@example.com" ] && EMAIL=""

DOMAIN="$(normalize_input "$(prompt "域名（需已解析到本服务器）" "$DOMAIN")")"
DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"
DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')"
[[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$ ]] || die "域名格式不正确：'$DOMAIN'（应类似 bot.example.com，不要带 http:// 和路径）"

EMAIL="$(normalize_input "$(prompt "邮箱（用于 Let's Encrypt 证书通知）" "$EMAIL")")"
[[ "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "邮箱格式不正确：'$EMAIL'（应类似 name@example.com；请检查是否有空格或全角字符）"

if [ -z "$BOT_TOKEN_IN" ] && [ -z "$(get_env BOT_TOKEN)" ]; then
  BOT_TOKEN_IN="$(prompt "Telegram Bot Token（可直接回车跳过，装好后在网页填写）" "")"
fi
BOT_TOKEN_IN="$(normalize_input "$BOT_TOKEN_IN")"
if [ -n "$BOT_TOKEN_IN" ] && [[ ! "$BOT_TOKEN_IN" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
  die "Bot Token 格式不正确（应类似 123456789:AAH...）"
fi
ok "域名 $DOMAIN，邮箱 $EMAIL"

# ---------------------------------------------------------------- 3. 域名与端口检查
step "检查域名解析和端口"
public_ip=""
for svc in https://api.ipify.org https://ifconfig.me/ip https://ipv4.icanhazip.com; do
  public_ip="$(curl -fsS4 --max-time 6 "$svc" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "$public_ip" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && break
  public_ip=""
done
domain_ips="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{ print $1 }' | sort -u | tr '\n' ' ' || true)"

if [ "$SKIP_DNS_CHECK" = 1 ]; then
  warn "已跳过域名解析检查"
elif [ -z "$domain_ips" ]; then
  warn "域名 $DOMAIN 还没有解析记录。请先添加 A 记录指向 ${public_ip:-本服务器 IP}，生效后再安装。"
  confirm "仍然继续？（证书会申请失败，直到解析生效）" N || die "已取消"
elif [ -n "$public_ip" ] && ! printf ' %s ' "$domain_ips" | grep -q " $public_ip "; then
  warn "域名解析到 [$domain_ips]，但本机公网 IP 是 $public_ip"
  warn "如果开启了 Cloudflare 代理（橙色云朵），这是正常的，请把 SSL 模式设为 Full (strict)。"
  confirm "确认继续？" N || die "已取消。请检查域名的 A 记录。"
else
  ok "域名解析正确：$DOMAIN → ${domain_ips% }"
fi

for port in 80 443; do
  holder="$(ss -H -ltnp "sport = :$port" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -n1 | cut -d'"' -f2 || true)"
  if [ -n "$holder" ]; then
    info "端口 $port 已被 $holder 占用（稍后自动选择接入方式，不会改动它的已有站点）"
  else
    ok "端口 $port 空闲"
  fi
done

# ---------------------------------------------------------------- 4. 安装 Docker
step "安装 Docker"
mkdir -p "$INSTALL_DIR"
if command -v docker >/dev/null 2>&1; then
  ok "已安装 $(docker --version | head -n1)"
else
  info "未检测到 Docker，使用官方脚本安装（get.docker.com）…"
  new_tmp; get_docker="$REPLY"
  curl -fsSL https://get.docker.com -o "$get_docker"
  # 记录 Docker 安装脚本会新建的目录原本是否存在，卸载时只删除本次新建的
  [ -d /etc/apt/keyrings ] || state_set CREATED_DIR /etc/apt/keyrings
  sh "$get_docker" >/dev/null
  rm -f -- "$get_docker"
  [ "$(state_get DOCKER_INSTALLED_BY_SCRIPT)" = 1 ] || state_set DOCKER_INSTALLED_BY_SCRIPT 1
  ok "Docker 安装完成"
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || die "Docker 未能正常运行，请检查：systemctl status docker"
docker compose version >/dev/null 2>&1 || {
  info "安装 docker compose 插件…"
  install_pkgs docker-compose-plugin || die "无法安装 docker compose 插件"
}
ok "$(docker compose version | head -n1)"

# ---------------------------------------------------------------- 5. 下载代码
step "下载程序到 $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH" -q
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" -q
  ok "已更新到最新版本 $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
else
  if [ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name .install-state -print -quit 2>/dev/null)" ]; then
    die "$INSTALL_DIR 不是空目录，也不是本程序的安装目录，为避免覆盖文件已停止。可用 --dir 指定其他目录。"
  fi
  new_tmp -d; tmp_clone="$REPLY"
  git clone --depth 1 -b "$BRANCH" -q "$REPO_URL" "$tmp_clone/src" || die "下载失败：$REPO_URL（仓库是否公开、网络能否访问 GitHub？）"
  cp -a "$tmp_clone/src/." "$INSTALL_DIR/"
  rm -rf "$tmp_clone"
  ok "已下载 $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
fi

# ---------------------------------------------------------------- 5.5 选择 HTTPS 接入方式
step "选择 HTTPS 接入方式"
# shellcheck source=scripts/proxy.sh
. "$INSTALL_DIR/scripts/proxy.sh"
case "$PROXY_ARG" in
  auto) PROXY_MODE="$(detect_proxy_mode)" ;;
  builtin|caddy|nginx|manual) PROXY_MODE="$PROXY_ARG" ;;
  *) die "--proxy 只能是 auto / builtin / caddy / nginx / manual" ;;
esac
if [ "$PROXY_MODE" = builtin ]; then
  for port in 80 443; do
    h="$(port_holder "$port")"
    if [ -n "$h" ] && ! { [ "$h" = docker-proxy ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx vps-restock-caddy; }; then
      die "端口 $port 已被 $h 占用，无法使用自带的 Caddy。去掉 --proxy builtin 让脚本自动选择接入方式"
    fi
  done
  ok "端口空闲：使用自带的 Caddy，自动申请和续期证书"
  is_ours "$INSTALL_DIR/docker-compose.override.yml" && rm -f -- "$INSTALL_DIR/docker-compose.override.yml"
else
  # 重新安装时沿用上次的端口（仍空闲或仍由本程序的容器占用），避免无谓地改动已有 Web 服务的配置
  HOST_PORT="$(state_get BOT_HOST_PORT)"
  if [ -z "$HOST_PORT" ] || { h="$(port_holder "$HOST_PORT")"; [ -n "$h" ] && \
       ! docker port vps-restock-bot 2>/dev/null | grep -q ":$HOST_PORT\$"; }; then
    HOST_PORT="$(pick_host_port)" || die "本机 18080-18099 端口都被占用，无法启动"
  fi
  write_compose_override "$INSTALL_DIR" "$HOST_PORT"
  state_set BOT_HOST_PORT "$HOST_PORT"
  case "$PROXY_MODE" in
    caddy) ok "检测到服务器上已有 Caddy：将为 $DOMAIN 新增一个站点，不改动已有站点" ;;
    nginx) ok "检测到服务器上已有 nginx：将为 $DOMAIN 新增一个站点，不改动已有站点" ;;
    manual) warn "80/443 被其他程序占用，且无法自动配置：程序只在本机端口 $HOST_PORT 启动，稍后给出配置说明" ;;
  esac
fi

# ---------------------------------------------------------------- 6. 写入配置
step "写入配置"
if [ "$EXISTING_ENV" = 0 ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
fi
chmod 600 "$INSTALL_DIR/.env"
set_env DOMAIN "$DOMAIN"
set_env ACME_EMAIL "$EMAIL"
[ -n "$BOT_TOKEN_IN" ] && set_env BOT_TOKEN "$BOT_TOKEN_IN"
NEW_WEB_TOKEN=""
if [ -z "$(get_env WEB_ADMIN_TOKEN)" ]; then
  NEW_WEB_TOKEN="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 32)"
  set_env WEB_ADMIN_TOKEN "$NEW_WEB_TOKEN"
fi
ok "配置已保存到 $INSTALL_DIR/.env（权限 600，仅 root 可读）"

# ---------------------------------------------------------------- 7. 防火墙
step "放行防火墙端口 80 / 443"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  for port in 80 443; do
    if ufw status | grep -qE "^${port}(/tcp)?[[:space:]]+ALLOW"; then
      ok "ufw 已放行 $port"
    else
      ufw allow "$port/tcp" >/dev/null
      state_set UFW_ADDED "$port"
      ok "ufw 已放行 $port（卸载时会撤销）"
    fi
  done
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  for svc in http https; do
    if firewall-cmd --permanent --query-service="$svc" >/dev/null 2>&1; then
      ok "firewalld 已放行 $svc"
    else
      firewall-cmd --permanent --add-service="$svc" >/dev/null
      state_set FIREWALLD_ADDED "$svc"
      ok "firewalld 已放行 $svc（卸载时会撤销）"
    fi
  done
  firewall-cmd --reload >/dev/null
else
  info "未检测到启用中的 ufw / firewalld"
fi
warn "如果使用云服务器，请确认控制台的“安全组/防火墙”也放行了 TCP 80 和 443"

# ---------------------------------------------------------------- 8. 启动
step "构建并启动（首次需要几分钟）"
cd "$INSTALL_DIR"
# 与容器共享的目录（网页一键更新用）：必须在启动前创建并交给容器内用户（uid 10001），
# 否则 Docker 会自动创建一个 root 所有的目录，容器无法写入
mkdir -p "$INSTALL_DIR/run"
chown 10001:10001 "$INSTALL_DIR/run"
chmod 750 "$INSTALL_DIR/run"
docker compose up -d --build --remove-orphans
ok "容器已启动"

info "等待程序就绪…"
healthy=0
for _ in $(seq 1 45); do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' vps-restock-bot 2>/dev/null || true)"
  if [ "$status" = healthy ]; then healthy=1; break; fi
  sleep 2
done
if [ "$healthy" = 1 ]; then
  ok "程序运行正常"
else
  docker compose logs --tail 30 bot || true
  die "程序未能正常启动，请查看上面的日志"
fi

case "$PROXY_MODE" in
  builtin) state_set PROXY_MODE builtin ;;
  caddy) step "配置已有的 Caddy"; setup_caddy_site "$DOMAIN" "$HOST_PORT" ;;
  nginx) step "配置已有的 nginx 并申请证书"; setup_nginx_site "$DOMAIN" "$EMAIL" "$HOST_PORT" ;;
  manual) step "需要你手动配置反向代理"; manual_instructions "$DOMAIN" "$HOST_PORT" ;;
esac

cert_ok=0
if [ "$PROXY_MODE" != manual ]; then
  info "等待 HTTPS 证书签发（通常 10～60 秒）…"
  for _ in $(seq 1 40); do
    if curl -fsS --max-time 5 -o /dev/null "https://$DOMAIN/api/health" 2>/dev/null; then cert_ok=1; break; fi
    sleep 3
  done
  if [ "$cert_ok" = 1 ]; then
    expiry="$(echo | openssl s_client -servername "$DOMAIN" -connect "$DOMAIN:443" 2>/dev/null | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || true)"
    ok "HTTPS 证书已生效${expiry:+，到期时间 $expiry（到期前约 30 天自动续期）}"
  else
    warn "暂时无法通过 https://$DOMAIN 访问，证书可能还在申请中。"
    case "$PROXY_MODE" in
      builtin) docker compose logs --tail 20 caddy || true ;;
      caddy) warn "可查看 Caddy 日志：journalctl -u caddy -n 50" ;;
      nginx) warn "可查看 nginx 日志：journalctl -u nginx -n 50，以及 /var/log/letsencrypt/letsencrypt.log" ;;
    esac
    warn "常见原因：域名未解析到本机、云服务器安全组未放行 80/443。"
  fi
fi

# ---------------------------------------------------------------- 9. 管理命令
install -m 755 "$INSTALL_DIR/scripts/vps-restock" "$CLI_PATH"
ok "已安装管理命令：vps-restock"
if command -v systemctl >/dev/null 2>&1 && "$CLI_PATH" install-units >/dev/null; then
  ok "已启用网页一键更新（systemd: vps-restock-update.path）"
else
  warn "未能启用网页一键更新，可以在服务器上用 vps-restock update 手动更新"
fi

# ---------------------------------------------------------------- 完成
printf '\n%s============================================================%s\n' "$C_GRN" "$C_RST"
printf '%s  安装完成%s\n\n' "$C_GRN$C_BLD" "$C_RST"
printf '  后台地址：%shttps://%s%s\n' "$C_BLD" "$DOMAIN" "$C_RST"
case "$PROXY_MODE" in
  builtin) printf '  HTTPS：自带的 Caddy（自动申请、续期证书）\n' ;;
  caddy) printf '  HTTPS：服务器上已有的 Caddy（新增站点文件 %s）\n' "$CADDY_SNIPPET" ;;
  nginx) printf '  HTTPS：服务器上已有的 nginx + certbot（新增站点文件，证书自动续期）\n' ;;
  manual) printf '  %sHTTPS：需要你在面板/现有 Web 服务中配置反向代理到 http://127.0.0.1:%s（见上方说明），配置好之后后台地址才能访问%s\n' "$C_YLW" "$HOST_PORT" "$C_RST" ;;
esac
if [ -n "$NEW_WEB_TOKEN" ]; then
  printf '  访问令牌：%s%s%s\n' "$C_BLD" "$NEW_WEB_TOKEN" "$C_RST"
  printf '  （只显示这一次，请妥善保存。可在后台“设置”里更换；忘记了运行 vps-restock reset-token 重新生成）\n'
else
  printf '  访问令牌：沿用原令牌（忘记了运行 vps-restock reset-token 重新生成）\n'
fi
printf '\n  下一步：\n'
[ -z "$(get_env BOT_TOKEN)" ] && printf '   1. 登录后台 → 设置 → 填写 Bot Token\n' || printf '   1. 登录后台确认机器人已连接\n'
printf '   2. 在 Telegram 给机器人发 /start（或拉进群），它会出现在“推送对象”里\n'
printf '   3. 监控网址 → 添加网址 → 选择推送对象和机型\n'
printf '\n  常用命令：vps-restock status | logs | update | reset-token | backup | uninstall\n'
printf '%s============================================================%s\n' "$C_GRN" "$C_RST"
