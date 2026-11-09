#!/usr/bin/env bash
# =============================================================================
#  VPS 补货监控 —— 一键卸载清理（还原到安装前的状态）
#
#    vps-restock uninstall
#  或
#    bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/vps-monitor-bot/main/scripts/uninstall.sh)
#
#  默认清理：容器、镜像、数据卷（数据库、HTTPS 证书）、安装目录与配置、管理命令、网页更新服务、
#  安装时添加的防火墙规则、本程序的备份文件和临时文件，以及【安装脚本装的】Docker 和基础工具。
#  安装之前服务器上就有的软件一律不动。
# =============================================================================
set -Euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/vps-restock-bot}"
CLI_PATH="/usr/local/bin/vps-restock"
CONTAINERS=(vps-restock-bot vps-restock-caddy)
CADDY_IMAGE="caddy:2.8.4-alpine"

ASSUME_YES=0
KEEP_BACKUP=0
KEEP_DOCKER=0
FORCE_DOCKER=0
KEEP_PACKAGES=0
KEPT=()          # 未清理的项目及原因，最后汇总给用户

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'; C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_BLD=""; C_RST=""
fi
step() { printf '\n%s==> %s%s\n' "$C_BLU$C_BLD" "$*" "$C_RST"; }
ok()   { printf '    %s✔ %s%s\n' "$C_GRN" "$*" "$C_RST"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %s! %s%s\n' "$C_YLW" "$*" "$C_RST" >&2; }
die()  { printf '\n%s✘ %s%s\n' "$C_RED" "$*" "$C_RST" >&2; exit 1; }
kept() { KEPT+=("$*"); warn "保留：$*"; }

# 临时目录退出时（包括失败、Ctrl+C 中断）一定删除：备份过程中它含有数据库和 .env
WORK_DIR=""
trap '[ -n "$WORK_DIR" ] && rm -rf -- "$WORK_DIR"; true' EXIT
trap 'exit 130' INT TERM

usage() {
  cat <<EOF
用法：uninstall.sh [选项]

默认还原到安装前的状态（不留备份、卸载安装脚本装的 Docker 和基础工具）。

  --keep-backup      卸载前备份数据库和配置到 /root，并保留以前的备份文件
  --keep-docker      保留 Docker（即使它是安装脚本装的）
  --force-docker     强制卸载 Docker：即使安装前就有、或服务器上还有其他程序的容器
                     （会同时删除这些容器和它们的数据，执行前单独确认）
  --keep-packages    保留安装脚本装的基础工具（curl、git 等）
  --dir <目录>       安装目录，默认 /opt/vps-restock-bot
  -y, --yes          免交互
  -h, --help         显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --keep-backup) KEEP_BACKUP=1; shift ;;
    --keep-docker) KEEP_DOCKER=1; shift ;;
    --force-docker) FORCE_DOCKER=1; shift ;;
    --keep-packages) KEEP_PACKAGES=1; shift ;;
    --no-backup|--purge-docker) shift ;;   # 旧版本参数，现在已是默认行为
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "未知参数：$1" ;;
  esac
done

confirm() {
  local text="$1" default="${2:-N}" answer
  [ "$ASSUME_YES" = 1 ] && { [ "$default" = Y ]; return; }
  [ -r /dev/tty ] || { [ "$default" = Y ]; return; }
  read -r -p "    $text [$( [ "$default" = Y ] && echo Y/n || echo y/N )]: " answer </dev/tty || true
  answer="${answer:-$default}"
  [[ "$answer" =~ ^[Yy] ]]
}

[ "$(id -u)" -eq 0 ] || die "请使用 root 运行"

# 安装目录安全检查：绝不删除系统目录
INSTALL_DIR="${INSTALL_DIR%/}"
case "$INSTALL_DIR" in
  ""|/|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/usr/local|/var)
    die "拒绝操作：安装目录 '$INSTALL_DIR' 不安全" ;;
esac
[[ "$INSTALL_DIR" == /* ]] || die "安装目录必须是绝对路径：$INSTALL_DIR"
if [ -d "$INSTALL_DIR" ] && [ ! -f "$INSTALL_DIR/docker-compose.yml" ] && [ ! -f "$INSTALL_DIR/.install-state" ]; then
  die "$INSTALL_DIR 看起来不是本程序的安装目录，已停止（防止误删）"
fi
PROJECT="$(basename "$INSTALL_DIR")"   # docker compose 项目名 = 安装目录名

# ---------------------------------------------------------------- 读取安装记录（删除安装目录前）
STATE_FILE="$INSTALL_DIR/.install-state"
HAS_STATE=0; [ -f "$STATE_FILE" ] && HAS_STATE=1
state_values() { [ -f "$STATE_FILE" ] && awk -F= -v k="$1" '$1 == k { print $2 }' "$STATE_FILE" | sort -u || true; }
UFW_ADDED="$(state_values UFW_ADDED)"
FIREWALLD_ADDED="$(state_values FIREWALLD_ADDED)"
DOCKER_BY_SCRIPT="$(state_values DOCKER_INSTALLED_BY_SCRIPT | tail -n1)"
mapfile -t PKGS_BY_SCRIPT < <(state_values PKG_INSTALLED)
mapfile -t DIRS_BY_SCRIPT < <(state_values CREATED_DIR)
BASE_IMAGE="$(awk 'toupper($1) == "FROM" { print $2; exit }' "$INSTALL_DIR/Dockerfile" 2>/dev/null || true)"
PROXY_MODE_RECORDED="$(state_values PROXY_MODE | tail -n1)"
DOMAIN_RECORDED="$(awk -F= '$1 == "DOMAIN" { print $2; exit }' "$INSTALL_DIR/.env" 2>/dev/null || true)"
HAS_DOCKER=0; command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && HAS_DOCKER=1
IS_APT=0; command -v dpkg-query >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1 && IS_APT=1
shopt -s nullglob
OLD_BACKUPS=(/root/vps-restock-backup-*.tar.gz)   # 本程序（vps-restock backup / 旧版卸载）生成的备份
shopt -u nullglob

# 卸载前已经处于“可自动删除”状态的依赖包：最后只清理本次卸载新产生的，不动原有的
apt_orphans() { apt-get -s autoremove 2>/dev/null | awk '/^Remv /{ print $2 }' | sort -u; }
ORPHANS_BEFORE=""
[ "$IS_APT" = 1 ] && ORPHANS_BEFORE="$(apt_orphans)"

# ---------------------------------------------------------------- 确认
printf '\n%s即将卸载 VPS 补货监控，并还原到安装前的状态。以下内容会被删除：%s\n' "$C_BLD" "$C_RST"
info "· 容器、本程序的镜像、数据卷（数据库：监控网址/机型/历史；HTTPS 证书）"
info "· 安装目录 $INSTALL_DIR（含 .env 配置和令牌）"
info "· 管理命令 $CLI_PATH、网页一键更新服务（systemd）"
case "$PROXY_MODE_RECORDED" in
  caddy) info "· 已有 Caddy 中本项目的站点和证书（其他站点不受影响）" ;;
  nginx) info "· 已有 nginx 中本项目的站点和 certbot 证书（其他站点不受影响）" ;;
esac
[ -n "$UFW_ADDED$FIREWALLD_ADDED" ] && info "· 安装时添加的防火墙规则：$(echo $UFW_ADDED $FIREWALLD_ADDED)"
if [ "$KEEP_BACKUP" = 0 ] && [ ${#OLD_BACKUPS[@]} -gt 0 ]; then
  info "· 本程序的备份文件（${#OLD_BACKUPS[@]} 个）：${OLD_BACKUPS[*]}"
fi
if [ "$KEEP_DOCKER" = 0 ] && [ "$FORCE_DOCKER" = 1 ] && command -v docker >/dev/null 2>&1; then
  info "· Docker（强制卸载）及其全部镜像、数据、配置、软件源，包括其他程序的容器"
elif [ "$KEEP_DOCKER" = 0 ] && [ "$DOCKER_BY_SCRIPT" = 1 ]; then
  info "· Docker（安装脚本装的）及其全部镜像、数据、配置、软件源"
fi
if [ "$KEEP_PACKAGES" = 0 ] && [ ${#PKGS_BY_SCRIPT[@]} -gt 0 ]; then
  info "· 安装脚本装的基础工具：${PKGS_BY_SCRIPT[*]}"
fi
info "· 本程序在 /tmp 中的临时文件"
[ "$KEEP_BACKUP" = 1 ] && info "（会先把数据库和配置备份到 /root）"
[ "$HAS_STATE" = 0 ] && warn "没有找到安装记录（$STATE_FILE），无法判断哪些软件是安装脚本装的，Docker 和基础工具将保留"
if [ "$ASSUME_YES" = 0 ]; then
  [ -r /dev/tty ] || die "无法交互确认。确定要卸载请加 -y 参数"
  confirm "确认卸载？此操作不可恢复" N || die "已取消"
fi

# ---------------------------------------------------------------- 备份（仅 --keep-backup）
backup=""
if [ "$KEEP_BACKUP" = 1 ]; then
  step "备份数据"
  work="$(mktemp -d "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"; WORK_DIR="$work"
  backup="/root/vps-restock-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  got=0
  if [ "$HAS_DOCKER" = 1 ] && docker inspect vps-restock-bot >/dev/null 2>&1; then
    docker stop -t 20 vps-restock-bot >/dev/null 2>&1 || true   # 先停止，让 SQLite 把 WAL 写回主文件
    docker cp vps-restock-bot:/app/data "$work/data" >/dev/null 2>&1 && got=1
  fi
  [ -f "$INSTALL_DIR/.env" ] && cp -p "$INSTALL_DIR/.env" "$work/env" && got=1
  if [ "$got" = 1 ]; then
    tar -czf "$backup" -C "$work" . && chmod 600 "$backup"
    ok "已备份到 $backup（含数据库和 .env，仅 root 可读）"
  else
    warn "没有找到可备份的数据"; backup=""
  fi
  rm -rf -- "$work"
fi

# ---------------------------------------------------------------- 容器、镜像、数据卷
step "删除容器、镜像和数据卷"
if [ "$HAS_DOCKER" = 1 ]; then
  if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    (cd "$INSTALL_DIR" && docker compose down --volumes --rmi local --remove-orphans) >/dev/null 2>&1 \
      && ok "已停止并删除本程序的容器" || warn "docker compose down 未完全成功，继续按名称清理"
  fi
  for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 && ok "已删除容器 $c"; done
  for v in "${PROJECT}_bot_data" "${PROJECT}_caddy_data" "${PROJECT}_caddy_config"; do
    docker volume rm "$v" >/dev/null 2>&1 && ok "已删除数据卷 $v"
  done
  docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
  docker image rm "${PROJECT}-bot" >/dev/null 2>&1 && ok "已删除镜像 ${PROJECT}-bot"
  # docker 会拒绝删除仍被其他容器使用的镜像，因此不会影响服务器上的其他程序
  for img in "$CADDY_IMAGE" "${BASE_IMAGE:-python:3.12-slim}"; do
    if docker image rm "$img" >/dev/null 2>&1; then
      ok "已删除镜像 $img"
    elif docker image inspect "$img" >/dev/null 2>&1; then
      kept "镜像 $img —— 仍被服务器上其他容器使用"
    fi
  done
else
  info "未检测到运行中的 Docker，跳过"
fi

# ---------------------------------------------------------------- 防火墙
if [ -n "$UFW_ADDED" ] && command -v ufw >/dev/null 2>&1; then
  step "撤销安装时添加的 ufw 规则"
  for port in $UFW_ADDED; do
    ufw delete allow "$port/tcp" >/dev/null 2>&1 && ok "已撤销 $port/tcp" || kept "ufw 规则 $port/tcp —— 撤销失败，请检查 ufw status"
  done
fi
if [ -n "$FIREWALLD_ADDED" ] && command -v firewall-cmd >/dev/null 2>&1; then
  step "撤销安装时添加的 firewalld 规则"
  for svc in $FIREWALLD_ADDED; do
    firewall-cmd --permanent --remove-service="$svc" >/dev/null 2>&1 && ok "已撤销 $svc" || kept "firewalld 服务 $svc —— 撤销失败"
  done
  firewall-cmd --reload >/dev/null 2>&1 || true
fi

# >>> 接入方式清理（tests/test_scripts.py 会单独加载这一段做测试）
# 只删除带有本项目标记的文件和行；改动已有程序的配置前先备份，检查不通过就恢复原样并保留。
# 不依赖安装记录：即使安装目录已被删除，也能按标记找到并清理。
CADDY_FILE="${CADDY_FILE:-/etc/caddy/Caddyfile}"
CADDY_SNIPPET="${CADDY_SNIPPET:-/etc/caddy/vps-restock.caddy}"
CADDY_DATA_DIRS="${CADDY_DATA_DIRS:-/var/lib/caddy/.local/share/caddy /root/.local/share/caddy}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
ACME_ROOT="${ACME_ROOT:-/var/lib/vps-restock-acme}"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"
VRB_MARK="vps-restock-bot：由安装脚本添加，卸载时自动删除"
CADDY_IMPORT_LINE="import $CADDY_SNIPPET # vps-restock-bot"

is_ours() { [ -f "$1" ] && grep -qF "$VRB_MARK" "$1"; }

remove_caddy_site() {
  local has_line=0 domain="" bk_file bk_snip
  [ -f "$CADDY_FILE" ] && grep -qxF "$CADDY_IMPORT_LINE" "$CADDY_FILE" && has_line=1
  is_ours "$CADDY_SNIPPET" || [ "$has_line" = 1 ] || return 0
  step "从已有的 Caddy 中移除本项目的站点"
  if is_ours "$CADDY_SNIPPET"; then
    domain="$(awk '/^[^#[:space:]].*\{[[:space:]]*$/ { print $1; exit }' "$CADDY_SNIPPET")"
  fi
  bk_file="$(mktemp "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"; bk_snip="$(mktemp "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"
  [ -f "$CADDY_FILE" ] && cp -p "$CADDY_FILE" "$bk_file"
  is_ours "$CADDY_SNIPPET" && cp -p "$CADDY_SNIPPET" "$bk_snip"
  if [ "$has_line" = 1 ]; then
    # 删除 import 行，以及安装时在它前面加的那个空行
    awk -v line="$CADDY_IMPORT_LINE" '
      { buf[NR] = $0 }
      END { for (i = 1; i <= NR; i++) {
              if (buf[i] == line) continue
              if (buf[i] == "" && (i + 1) <= NR && buf[i + 1] == line) continue
              print buf[i] } }' "$bk_file" >"$CADDY_FILE"
  fi
  is_ours "$CADDY_SNIPPET" && rm -f -- "$CADDY_SNIPPET"
  if command -v caddy >/dev/null 2>&1 && ! caddy validate --config "$CADDY_FILE" --adapter caddyfile >/dev/null 2>&1; then
    cp -p "$bk_file" "$CADDY_FILE"; [ -s "$bk_snip" ] && cp -p "$bk_snip" "$CADDY_SNIPPET"
    rm -f -- "$bk_file" "$bk_snip"
    kept "Caddy 站点 —— 移除后配置检查未通过，已恢复原样，请手动检查 $CADDY_FILE"
    return 0
  fi
  rm -f -- "$bk_file" "$bk_snip"
  if systemctl is-active --quiet caddy 2>/dev/null; then systemctl reload caddy >/dev/null 2>&1 || true
  elif command -v caddy >/dev/null 2>&1; then caddy reload --config "$CADDY_FILE" --adapter caddyfile >/dev/null 2>&1 || true; fi
  ok "已移除 Caddy 中的本项目站点${domain:+ $domain}，其他站点不受影响"
  # 删除 Caddy 为本项目域名申请的证书（只删这个域名的目录）
  if [ -n "$domain" ]; then
    local d c
    for d in $CADDY_DATA_DIRS; do
      [ -d "$d/certificates" ] || continue
      for c in "$d"/certificates/*/"$domain"; do
        [ -d "$c" ] && rm -rf -- "$c" && ok "已删除 $domain 的证书（$c）"
      done
    done
  fi
}

remove_nginx_site() {
  local files=() f domain="" bk
  for f in "$NGINX_DIR/conf.d/vps-restock.conf" "$NGINX_DIR/sites-available/vps-restock.conf"; do
    is_ours "$f" && files+=("$f")
  done
  [ ${#files[@]} -gt 0 ] || return 0
  step "从已有的 nginx 中移除本项目的站点"
  domain="$(awk '$1 == "server_name" { gsub(";", "", $2); print $2; exit }' "${files[0]}")"
  bk="$(mktemp -d "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"
  for f in "${files[@]}"; do cp -p "$f" "$bk/$(echo "$f" | tr '/' '_')"; rm -f -- "$f"; done
  local link="$NGINX_DIR/sites-enabled/vps-restock.conf"
  [ -L "$link" ] && rm -f -- "$link"
  if command -v nginx >/dev/null 2>&1 && ! nginx -t >/dev/null 2>&1; then
    for f in "${files[@]}"; do cp -p "$bk/$(echo "$f" | tr '/' '_')" "$f"; done
    [ -f "$NGINX_DIR/sites-available/vps-restock.conf" ] && [ -d "$NGINX_DIR/sites-enabled" ] && \
      ln -sfn "$NGINX_DIR/sites-available/vps-restock.conf" "$link"
    rm -rf -- "$bk"
    kept "nginx 站点 —— 移除后配置检查未通过，已恢复原样，请手动检查 nginx -t"
    return 0
  fi
  rm -rf -- "$bk"
  systemctl reload nginx >/dev/null 2>&1 || nginx -s reload >/dev/null 2>&1 || true
  ok "已移除 nginx 中的本项目站点${domain:+ $domain}，其他站点不受影响"
  # 只删除本项目申请的证书：其续期配置里使用的是本项目的验证目录
  if [ -n "$domain" ] && [ -f "$LE_DIR/renewal/$domain.conf" ] && grep -qF "$ACME_ROOT" "$LE_DIR/renewal/$domain.conf"; then
    if command -v certbot >/dev/null 2>&1 && certbot delete --cert-name "$domain" --non-interactive >/dev/null 2>&1; then
      ok "已删除 $domain 的证书和自动续期配置"
    else
      kept "$domain 的证书 —— certbot 删除失败，可手动运行 certbot delete --cert-name $domain"
    fi
  fi
  [ -d "$ACME_ROOT" ] && rm -rf -- "$ACME_ROOT" && ok "已删除证书验证目录 $ACME_ROOT"
}
# <<< 接入方式清理

if [ "$PROXY_MODE_RECORDED" = manual ] && [ -n "$DOMAIN_RECORDED" ]; then
  kept "面板中的反向代理站点 $DOMAIN_RECORDED —— 安装时是手动配置的，请在你的面板中删除该站点和证书"
fi
remove_caddy_site
remove_nginx_site

# ---------------------------------------------------------------- 网页一键更新的 systemd 单元
if [ -f /etc/systemd/system/vps-restock-update.path ] || [ -f /etc/systemd/system/vps-restock-update.service ]; then
  step "删除网页一键更新服务"
  systemctl disable --now vps-restock-update.path >/dev/null 2>&1 || true
  systemctl stop vps-restock-update.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vps-restock-update.path /etc/systemd/system/vps-restock-update.service
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed vps-restock-update.service vps-restock-update.path >/dev/null 2>&1 || true
  ok "已删除 vps-restock-update.path / .service"
fi

# ---------------------------------------------------------------- 程序文件与备份
step "删除程序文件"
[ -e "$CLI_PATH" ] && rm -f -- "$CLI_PATH" && ok "已删除 $CLI_PATH"
[ -d "$INSTALL_DIR" ] && rm -rf -- "$INSTALL_DIR" && ok "已删除 $INSTALL_DIR"
if [ "$KEEP_BACKUP" = 0 ]; then
  for f in "${OLD_BACKUPS[@]}"; do rm -f -- "$f" && ok "已删除备份 $f"; done
fi

# ---------------------------------------------------------------- Docker 本身
remove_docker() {
  systemctl disable --now docker.service docker.socket containerd.service >/dev/null 2>&1 || true
  local pkgs=()
  # 按系统中实际安装的包卸载（get.docker.com 不同版本装的包不同）
  if [ "$IS_APT" = 1 ]; then
    mapfile -t pkgs < <(dpkg-query -W -f='${Status} ${Package}\n' 2>/dev/null \
      | awk '($3 == "installed" || $3 == "config-files") && ($4 ~ /^docker-/ || $4 == "containerd.io") { print $4 }')
    [ ${#pkgs[@]} -gt 0 ] && { info "卸载软件包：${pkgs[*]}"; DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "${pkgs[@]}" >/dev/null 2>&1 || warn "部分软件包卸载失败"; }
    rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.sources \
          /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg
  else
    local pm; pm="$(command -v dnf || command -v yum)"
    mapfile -t pkgs < <(rpm -qa --qf '%{NAME}\n' 2>/dev/null | grep -E '^(docker-|containerd\.io$)' | sort -u)
    [ ${#pkgs[@]} -gt 0 ] && { info "卸载软件包：${pkgs[*]}"; "$pm" remove -y -q "${pkgs[@]}" >/dev/null 2>&1 || warn "部分软件包卸载失败"; }
    rm -f /etc/yum.repos.d/docker-ce.repo
  fi
  rm -rf -- /var/lib/docker /var/lib/containerd /etc/docker /root/.docker /run/docker /run/containerd
  getent group docker >/dev/null 2>&1 && groupdel docker >/dev/null 2>&1
  systemctl daemon-reload >/dev/null 2>&1 || true
  if command -v docker >/dev/null 2>&1; then
    kept "Docker —— 未能完全卸载（$(command -v docker) 仍存在），请手动检查"
  else
    ok "Docker 已卸载（软件包、镜像和数据、配置、软件源、用户组均已清理）"
  fi
}

other_containers=""
[ "$HAS_DOCKER" = 1 ] && other_containers="$(docker ps -a --format '{{.Names}}' 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')"
if [ "$KEEP_DOCKER" = 1 ]; then
  command -v docker >/dev/null 2>&1 && kept "Docker —— 使用了 --keep-docker"
elif [ "$FORCE_DOCKER" = 1 ] && command -v docker >/dev/null 2>&1; then
  go=1
  if [ -n "$other_containers" ]; then
    warn "服务器上还有其他程序的容器：$other_containers"
    warn "强制卸载 Docker 会同时删除这些容器，以及它们的镜像和数据卷，无法恢复"
    if [ "$ASSUME_YES" = 0 ] && ! confirm "确定强制卸载 Docker？" N; then
      go=0; kept "Docker —— 取消了强制卸载"
    fi
  fi
  if [ "$go" = 1 ]; then
    step "卸载 Docker（强制）"
    remove_docker
  fi
elif [ "$DOCKER_BY_SCRIPT" = 1 ]; then
  if [ -n "$other_containers" ]; then
    kept "Docker —— 服务器上还有其他程序的容器（$other_containers），卸载会影响它们；确定要卸载请加 --force-docker"
  else
    step "卸载 Docker（安装脚本装的）"
    remove_docker
  fi
elif command -v docker >/dev/null 2>&1; then
  kept "Docker —— 安装本程序之前就已存在；确定要卸载请加 --force-docker"
fi

# ---------------------------------------------------------------- 安装脚本装的基础工具
# 先模拟卸载：如果会连带删除其他软件（说明之后有别的软件依赖它），就跳过
if [ ${#PKGS_BY_SCRIPT[@]} -gt 0 ]; then
  if [ "$KEEP_PACKAGES" = 1 ]; then
    kept "基础工具 ${PKGS_BY_SCRIPT[*]} —— 使用了 --keep-packages"
  else
    step "卸载安装脚本装的基础工具"
    for p in "${PKGS_BY_SCRIPT[@]}"; do
      [ -n "$p" ] || continue
      if [ "$IS_APT" = 1 ]; then
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "installed" || { info "$p 已不存在"; continue; }
        n="$(apt-get -s purge "$p" 2>/dev/null | grep -cE '^(Purg|Remv) ')"
        if [ "$n" -gt 1 ]; then
          kept "$p —— 其他软件依赖它，卸载会连带删除 $((n - 1)) 个其他软件包"
        elif DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "$p" >/dev/null 2>&1; then
          ok "已卸载 $p"
        else
          kept "$p —— 卸载失败"
        fi
      else
        rpm -q "$p" >/dev/null 2>&1 || { info "$p 已不存在"; continue; }
        if ! rpm -e --test "$p" >/dev/null 2>&1; then
          kept "$p —— 其他软件依赖它"
        elif "$(command -v dnf || command -v yum)" remove -y -q "$p" >/dev/null 2>&1; then
          ok "已卸载 $p"
        else
          kept "$p —— 卸载失败"
        fi
      fi
    done
  fi
fi

# 只清理本次卸载新产生的无用依赖包（卸载前就已存在的不动）
if [ "$IS_APT" = 1 ]; then
  mapfile -t new_orphans < <(comm -13 <(printf '%s\n' "$ORPHANS_BEFORE") <(apt_orphans) | sed '/^$/d')
  if [ ${#new_orphans[@]} -gt 0 ]; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq "${new_orphans[@]}" >/dev/null 2>&1 \
      && ok "已清理随之不再需要的依赖包 ${#new_orphans[@]} 个" || warn "部分依赖包清理失败"
  fi
fi

# 安装时新建、现在为空的目录
for d in "${DIRS_BY_SCRIPT[@]}"; do
  [ -n "$d" ] && [ -d "$d" ] && rmdir -- "$d" 2>/dev/null && ok "已删除安装时新建的空目录 $d"
done

# ---------------------------------------------------------------- 临时文件残留
# 只清理本程序前缀为 vps-restock. 的临时文件，以及旧版本用过的 /tmp/get-docker.sh；跳过正在运行的自身副本
step "清理临时文件"
self="$(readlink -f "$0" 2>/dev/null || echo "$0")"
leftovers=0
for tmpdir in "${TMPDIR:-/tmp}" /tmp; do
  [ -d "$tmpdir" ] || continue
  while IFS= read -r -d '' p; do
    [ "$(readlink -f "$p")" = "$self" ] && continue
    rm -rf -- "$p" && leftovers=$((leftovers + 1))
  done < <(find "$tmpdir" -maxdepth 1 -name 'vps-restock.*' -user root -print0 2>/dev/null)
done
[ -f /tmp/get-docker.sh ] && rm -f /tmp/get-docker.sh && leftovers=$((leftovers + 1))
if [ "$leftovers" -gt 0 ]; then ok "已清理 $leftovers 个临时文件"; else ok "没有残留的临时文件"; fi

# ---------------------------------------------------------------- 汇总
if [ ${#KEPT[@]} -eq 0 ] && [ -z "$backup" ]; then
  printf '\n%s卸载完成，已还原到安装前的状态。%s\n' "$C_GRN$C_BLD" "$C_RST"
else
  printf '\n%s卸载完成。以下内容保留：%s\n' "$C_GRN$C_BLD" "$C_RST"
  for k in "${KEPT[@]}"; do printf '  · %s\n' "$k"; done
  [ -n "$backup" ] && printf '  · 备份文件 %s（--keep-backup），恢复方法见 README 的“备份与恢复”\n' "$backup"
fi
printf '\n服务器之外的内容需要你自己处理：\n'
printf '  · DNS 服务商处的域名解析记录\n'
printf '  · 云服务器控制台安全组里放行 80/443 的规则（如果是你手动添加的）\n'
printf '  · 系统日志（journald）中的运行记录会按系统策略自动轮转清理\n'
exit 0
