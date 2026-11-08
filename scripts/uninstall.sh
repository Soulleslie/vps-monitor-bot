#!/usr/bin/env bash
# =============================================================================
#  VPS 补货监控 —— 一键卸载清理
#
#    vps-restock uninstall
#  或
#    bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/vps-monitor-bot/main/scripts/uninstall.sh)
#
#  会删除：容器、镜像、数据卷（数据库和证书）、安装目录、管理命令、安装时添加的防火墙规则。
#  默认先把数据库和配置备份到 /root。Docker 本身默认保留。
# =============================================================================
set -Euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/vps-restock-bot}"
CLI_PATH="/usr/local/bin/vps-restock"
PROJECT="vps-restock-bot"                     # docker compose 项目名（= 安装目录名）
CONTAINERS=(vps-restock-bot vps-restock-caddy)
CADDY_IMAGE="caddy:2.8.4-alpine"

ASSUME_YES=0
DO_BACKUP=1
PURGE_DOCKER=0

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

# 临时目录退出时（包括失败、Ctrl+C 中断）一定删除：备份过程中它含有数据库和 .env
WORK_DIR=""
trap '[ -n "$WORK_DIR" ] && rm -rf -- "$WORK_DIR"; true' EXIT
trap 'exit 130' INT TERM

usage() {
  cat <<EOF
用法：uninstall.sh [选项]

  --dir <目录>       安装目录，默认 /opt/vps-restock-bot
  --no-backup        不备份数据库和配置，直接删除
  --purge-docker     同时卸载 Docker（仅当 Docker 是安装脚本装的、且服务器上没有其他容器时）
  -y, --yes          免交互（默认备份、保留 Docker）
  -h, --help         显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="${2:-}"; shift 2 ;;
    --no-backup) DO_BACKUP=0; shift ;;
    --purge-docker) PURGE_DOCKER=1; shift ;;
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
PROJECT="$(basename "$INSTALL_DIR")"

STATE_FILE="$INSTALL_DIR/.install-state"
state_values() { [ -f "$STATE_FILE" ] && awk -F= -v k="$1" '$1 == k { print $2 }' "$STATE_FILE" | sort -u || true; }
UFW_ADDED="$(state_values UFW_ADDED)"
FIREWALLD_ADDED="$(state_values FIREWALLD_ADDED)"
DOCKER_BY_SCRIPT="$(state_values DOCKER_INSTALLED_BY_SCRIPT | tail -n1)"
HAS_DOCKER=0; command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && HAS_DOCKER=1

# ---------------------------------------------------------------- 确认
printf '\n%s即将卸载 VPS 补货监控，以下内容会被删除：%s\n' "$C_BLD" "$C_RST"
info "· 容器：${CONTAINERS[*]}"
info "· 数据卷：数据库（监控网址、机型、历史）和 HTTPS 证书"
info "· 本程序构建的镜像，以及未被其他容器使用的 Caddy 镜像和 Python 基础镜像"
info "· 安装目录：$INSTALL_DIR（含 .env 配置）"
info "· 管理命令：$CLI_PATH，以及网页一键更新服务（systemd: vps-restock-update）"
[ -n "$UFW_ADDED$FIREWALLD_ADDED" ] && info "· 安装时添加的防火墙规则：$(echo $UFW_ADDED $FIREWALLD_ADDED)"
info "Docker 本身默认保留。"
if [ "$ASSUME_YES" = 0 ]; then
  [ -r /dev/tty ] || die "无法交互确认。确定要卸载请加 -y 参数"
  confirm "确认卸载？" N || die "已取消"
fi

if [ "$DO_BACKUP" = 1 ] && [ "$ASSUME_YES" = 0 ]; then
  confirm "卸载前备份数据库和配置到 /root？（推荐）" Y || DO_BACKUP=0
fi

# ---------------------------------------------------------------- 备份
if [ "$DO_BACKUP" = 1 ]; then
  step "备份数据"
  ts="$(date +%Y%m%d-%H%M%S)"
  work="$(mktemp -d "${TMPDIR:-/tmp}/vps-restock.XXXXXXXX")"; WORK_DIR="$work"
  backup="/root/vps-restock-backup-$ts.tar.gz"
  got=0
  if [ "$HAS_DOCKER" = 1 ] && docker inspect vps-restock-bot >/dev/null 2>&1; then
    # 先停止程序，让 SQLite 把 WAL 日志写回主文件，保证备份完整
    docker stop -t 20 vps-restock-bot >/dev/null 2>&1 || true
    if docker cp vps-restock-bot:/app/data "$work/data" >/dev/null 2>&1; then got=1; fi
  fi
  if [ -f "$INSTALL_DIR/.env" ]; then cp -p "$INSTALL_DIR/.env" "$work/env"; got=1; fi
  if [ "$got" = 1 ]; then
    tar -czf "$backup" -C "$work" . && chmod 600 "$backup"
    ok "已备份到 $backup（含数据库和 .env，仅 root 可读）"
  else
    warn "没有找到可备份的数据"
  fi
  rm -rf -- "$work"
fi

# ---------------------------------------------------------------- 容器、镜像、数据卷
step "删除容器、镜像和数据卷"
if [ "$HAS_DOCKER" = 1 ]; then
  if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    (cd "$INSTALL_DIR" && docker compose down --volumes --rmi local --remove-orphans) >/dev/null 2>&1 \
      && ok "已执行 docker compose down" || warn "docker compose down 未完全成功，继续按名称清理"
  fi
  for c in "${CONTAINERS[@]}"; do
    docker rm -f "$c" >/dev/null 2>&1 && ok "已删除容器 $c" || true
  done
  for v in "${PROJECT}_bot_data" "${PROJECT}_caddy_data" "${PROJECT}_caddy_config"; do
    docker volume rm "$v" >/dev/null 2>&1 && ok "已删除数据卷 $v" || true
  done
  docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
  docker image rm "${PROJECT}-bot" >/dev/null 2>&1 && ok "已删除镜像 ${PROJECT}-bot" || true
  # Caddy 镜像，以及构建程序时下载的基础镜像（从 Dockerfile 读取，默认 python:3.12-slim）。
  # docker 会拒绝删除仍被其他容器使用的镜像，因此不会影响服务器上的其他程序。
  base_image="$(awk 'toupper($1) == "FROM" { print $2; exit }' "$INSTALL_DIR/Dockerfile" 2>/dev/null || true)"
  for img in "$CADDY_IMAGE" "${base_image:-python:3.12-slim}"; do
    if docker image rm "$img" >/dev/null 2>&1; then
      ok "已删除镜像 $img"
    elif docker image inspect "$img" >/dev/null 2>&1; then
      info "镜像 $img 仍被其他容器使用，已保留"
    fi
  done
  ok "Docker 资源清理完成"
  info "提示：Docker 构建缓存是所有程序共用的，未自动清理；确认不需要时可运行 docker builder prune"
else
  info "未检测到运行中的 Docker，跳过"
fi

# ---------------------------------------------------------------- 防火墙
if [ -n "$UFW_ADDED" ] && command -v ufw >/dev/null 2>&1; then
  step "撤销安装时添加的 ufw 规则"
  for port in $UFW_ADDED; do
    ufw delete allow "$port/tcp" >/dev/null 2>&1 && ok "已撤销 $port/tcp" || warn "撤销 $port/tcp 失败，请手动检查 ufw status"
  done
fi
if [ -n "$FIREWALLD_ADDED" ] && command -v firewall-cmd >/dev/null 2>&1; then
  step "撤销安装时添加的 firewalld 规则"
  for svc in $FIREWALLD_ADDED; do
    firewall-cmd --permanent --remove-service="$svc" >/dev/null 2>&1 && ok "已撤销 $svc" || warn "撤销 $svc 失败"
  done
  firewall-cmd --reload >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- 网页一键更新的 systemd 单元
if [ -f /etc/systemd/system/vps-restock-update.path ] || [ -f /etc/systemd/system/vps-restock-update.service ]; then
  step "删除网页一键更新服务"
  systemctl disable --now vps-restock-update.path >/dev/null 2>&1 || true
  systemctl stop vps-restock-update.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/vps-restock-update.path /etc/systemd/system/vps-restock-update.service
  systemctl daemon-reload >/dev/null 2>&1 || true
  ok "已删除 vps-restock-update.path / .service"
fi

# ---------------------------------------------------------------- 文件
step "删除程序文件"
rm -f -- "$CLI_PATH" && ok "已删除 $CLI_PATH"
if [ -d "$INSTALL_DIR" ]; then
  rm -rf -- "$INSTALL_DIR" && ok "已删除 $INSTALL_DIR"
fi

# ---------------------------------------------------------------- Docker 本身
if [ "$HAS_DOCKER" = 1 ] && [ "$DOCKER_BY_SCRIPT" = 1 ]; then
  others="$(docker ps -aq 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$others" != 0 ]; then
    info "服务器上还有 $others 个其他容器，保留 Docker"
  elif [ "$PURGE_DOCKER" = 1 ] || { [ "$ASSUME_YES" = 0 ] && confirm "Docker 是安装时自动装的，且已没有其他容器。是否一并卸载 Docker？" N; }; then
    step "卸载 Docker"
    systemctl disable --now docker docker.socket containerd >/dev/null 2>&1 || true
    if command -v apt-get >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras >/dev/null 2>&1 || true
      DEBIAN_FRONTEND=noninteractive apt-get autoremove -y -qq >/dev/null 2>&1 || true
      rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg
    else
      pm="$(command -v dnf || command -v yum)"
      "$pm" remove -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
        docker-compose-plugin docker-ce-rootless-extras >/dev/null 2>&1 || true
      rm -f /etc/yum.repos.d/docker-ce.repo
    fi
    rm -rf -- /var/lib/docker /var/lib/containerd
    ok "Docker 已卸载"
  fi
fi

# ---------------------------------------------------------------- 临时文件残留
# 只清理本程序前缀为 vps-restock. 的临时文件（异常中断时可能残留），以及旧版本用过的 /tmp/get-docker.sh；
# 跳过正在运行的卸载脚本副本本身
step "清理临时文件残留"
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

printf '\n%s卸载完成。%s\n' "$C_GRN$C_BLD" "$C_RST"
[ "$DO_BACKUP" = 1 ] && [ -n "${backup:-}" ] && [ -f "${backup:-}" ] && \
  printf '备份文件：%s\n恢复方法见 README 的“备份与恢复”。\n' "$backup"
printf '如果不再使用这个域名，可以到 DNS 服务商删除对应的解析记录。\n'
exit 0
