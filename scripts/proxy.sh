#!/usr/bin/env bash
# =============================================================================
#  VPS 补货监控 —— HTTPS 接入方式（由 install.sh 加载，不单独运行）
#
#  安装时根据 80/443 端口的占用情况自动选择：
#    builtin  端口空闲：使用本项目自带的 Caddy（Docker 容器）
#    caddy    服务器上已有 Caddy：新增本项目的站点文件，由已有 Caddy 申请证书并转发
#    nginx    服务器上已有 nginx：新增本项目的站点文件，用 certbot 申请证书
#    manual   其他情况（宝塔面板、Apache、Docker 里的反向代理等）：只在本机开放端口，并给出配置说明
#
#  安全原则：
#    · 只新增带有标记的独立文件；必须改动已有主配置时（Caddy 的 import 行），先备份，且只加一行带标记的内容
#    · 每次改动先用程序自带的检查命令验证（caddy validate / nginx -t），失败则自动恢复原样
#    · 使用“重新加载”而不是“重启”，不中断其他网站
#    · 所有改动都记录到安装状态文件，卸载时只撤销这些改动
#
#  依赖 install.sh 中的：info ok warn die state_set install_pkgs new_tmp
# =============================================================================

# 路径可以用环境变量覆盖（自动化测试用）
CADDY_FILE="${CADDY_FILE:-/etc/caddy/Caddyfile}"
CADDY_SNIPPET="${CADDY_SNIPPET:-/etc/caddy/vps-restock.caddy}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
ACME_ROOT="${ACME_ROOT:-/var/lib/vps-restock-acme}"
LE_DIR="${LE_DIR:-/etc/letsencrypt}"
BT_PANEL_DIR="${BT_PANEL_DIR:-/www/server/panel}"
VRB_MARK="vps-restock-bot：由安装脚本添加，卸载时自动删除"
CADDY_IMPORT_LINE="import $CADDY_SNIPPET # vps-restock-bot"

# 监听某端口的进程名（没有则输出空）
port_holder() {
  ss -H -ltnp "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -n1 | cut -d'"' -f2 || true
}

# 根据 80/443 的占用情况选择接入方式；输出 builtin / caddy / nginx / manual
detect_proxy_mode() {
  local h80 h443 holder
  h80="$(port_holder 80)"; h443="$(port_holder 443)"
  if [ -z "$h80$h443" ]; then echo builtin; return; fi
  # 本项目自带的 Caddy 容器（重新安装时）
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'vps-restock-caddy' && \
     [[ "$h80$h443" =~ ^(docker-proxy)*$ ]]; then
    echo builtin; return
  fi
  holder="${h443:-$h80}"
  if [ -d "$BT_PANEL_DIR" ]; then echo manual; return; fi   # 宝塔面板：由面板管理配置，不自动改动
  case "$holder" in
    caddy) if [ -f "$CADDY_FILE" ] && command -v caddy >/dev/null 2>&1; then echo caddy; else echo manual; fi ;;
    nginx) if command -v nginx >/dev/null 2>&1 && nginx_conf_dir >/dev/null; then echo nginx; else echo manual; fi ;;
    *) echo manual ;;
  esac
}

# 选择本机端口（只监听 127.0.0.1，供已有的 Web 服务转发）
pick_host_port() {
  local p
  for p in $(seq 18080 18099); do
    [ -z "$(port_holder "$p")" ] && { echo "$p"; return 0; }
  done
  return 1
}

# 生成 docker-compose.override.yml：不启动自带的 Caddy，程序只监听本机端口
write_compose_override() {
  local dir="$1" port="$2"
  cat >"$dir/docker-compose.override.yml" <<EOF
# $VRB_MARK
# 不使用自带的 Caddy，由服务器上已有的 Web 服务转发到本机端口 $port
services:
  caddy:
    profiles: ["disabled"]
  bot:
    ports:
      - "127.0.0.1:$port:8080"
EOF
}

# 文件是否由本项目创建（含标记）
is_ours() { [ -f "$1" ] && grep -qF "$VRB_MARK" "$1"; }

# ---------------------------------------------------------------- Caddy
caddy_validate() { caddy validate --config "$CADDY_FILE" --adapter caddyfile >/dev/null 2>&1; }
caddy_reload() {
  if systemctl is-active --quiet caddy 2>/dev/null; then
    systemctl reload caddy
  else
    caddy reload --config "$CADDY_FILE" --adapter caddyfile >/dev/null 2>&1
  fi
}

setup_caddy_site() {
  local domain="$1" port="$2" backup snippet_backup=""
  if [ -f "$CADDY_SNIPPET" ] && ! is_ours "$CADDY_SNIPPET"; then
    die "$CADDY_SNIPPET 已存在且不是本程序创建的，为避免覆盖已停止"
  fi
  # 先备份；备份失败就不做任何改动
  if [ -f "$CADDY_SNIPPET" ]; then
    new_tmp; snippet_backup="$REPLY"
    { [ -n "$snippet_backup" ] && cp -p "$CADDY_SNIPPET" "$snippet_backup"; } || die "无法备份 $CADDY_SNIPPET，未做任何改动"
  fi
  new_tmp; backup="$REPLY"
  { [ -n "$backup" ] && cp -p "$CADDY_FILE" "$backup" && cmp -s "$CADDY_FILE" "$backup"; } || die "无法备份 $CADDY_FILE，未做任何改动"

  cat >"$CADDY_SNIPPET" <<EOF
# $VRB_MARK
$domain {
	encode zstd gzip
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 127.0.0.1:$port
}
EOF
  chmod 644 "$CADDY_SNIPPET"
  if ! grep -qxF "$CADDY_IMPORT_LINE" "$CADDY_FILE"; then
    printf '\n%s\n' "$CADDY_IMPORT_LINE" >>"$CADDY_FILE"
  fi

  if ! caddy_validate; then
    cp -p "$backup" "$CADDY_FILE"
    if [ -n "$snippet_backup" ]; then cp -p "$snippet_backup" "$CADDY_SNIPPET"; else rm -f -- "$CADDY_SNIPPET"; fi
    die "Caddy 配置检查未通过（可能该域名已在现有配置中使用），已恢复原配置，未做任何改动"
  fi
  if ! caddy_reload; then
    cp -p "$backup" "$CADDY_FILE"
    if [ -n "$snippet_backup" ]; then cp -p "$snippet_backup" "$CADDY_SNIPPET"; else rm -f -- "$CADDY_SNIPPET"; fi
    caddy_reload || true
    die "Caddy 重新加载失败，已恢复原配置"
  fi
  state_set PROXY_MODE caddy
  state_set CADDY_SNIPPET "$CADDY_SNIPPET"
  state_set CADDY_FILE "$CADDY_FILE"
  ok "已在现有 Caddy 中添加站点 $domain（文件 $CADDY_SNIPPET），证书由 Caddy 自动申请和续期"
}

# ---------------------------------------------------------------- nginx
# 输出用于存放本项目站点文件的目录与方式：“conf.d” 或 “sites”
nginx_conf_dir() {
  local dump
  dump="$(nginx -T 2>/dev/null)" || return 1
  if printf '%s' "$dump" | grep -Eq "include[[:space:]]+($NGINX_DIR/)?conf\.d/\*\.conf"; then
    echo conf.d
  elif printf '%s' "$dump" | grep -Eq "include[[:space:]]+($NGINX_DIR/)?sites-enabled/\*"; then
    echo sites
  else
    return 1
  fi
}

nginx_site_paths() {   # 输出：配置文件路径 [启用链接路径]
  case "$(nginx_conf_dir)" in
    conf.d) echo "$NGINX_DIR/conf.d/vps-restock.conf" ;;
    sites)  echo "$NGINX_DIR/sites-available/vps-restock.conf $NGINX_DIR/sites-enabled/vps-restock.conf" ;;
  esac
}

nginx_reload() { systemctl reload nginx 2>/dev/null || nginx -s reload; }

write_nginx_conf() {   # write_nginx_conf <文件> <域名> <端口> <http|full>
  local file="$1" domain="$2" port="$3" kind="$4" v6=0
  [ -f /proc/net/if_inet6 ] && v6=1
  {
    echo "# $VRB_MARK"
    echo "server {"
    echo "    listen 80;"
    [ "$v6" = 1 ] && echo "    listen [::]:80;"
    echo "    server_name $domain;"
    echo "    location ^~ /.well-known/acme-challenge/ { root $ACME_ROOT; }"
    echo "    location / { return 301 https://\$host\$request_uri; }"
    echo "}"
    if [ "$kind" = full ]; then
      echo "server {"
      echo "    listen 443 ssl;"
      [ "$v6" = 1 ] && echo "    listen [::]:443 ssl;"
      echo "    server_name $domain;"
      echo "    ssl_certificate $LE_DIR/live/$domain/fullchain.pem;"
      echo "    ssl_certificate_key $LE_DIR/live/$domain/privkey.pem;"
      echo "    add_header Strict-Transport-Security \"max-age=31536000\" always;"
      echo "    client_max_body_size 1m;"
      echo "    location / {"
      echo "        proxy_pass http://127.0.0.1:$port;"
      echo "        proxy_set_header Host \$host;"
      echo "        proxy_set_header X-Forwarded-For \$remote_addr;"
      echo "        proxy_set_header X-Forwarded-Proto https;"
      echo "        proxy_read_timeout 120s;"
      echo "    }"
      echo "}"
    fi
  } >"$file"
  chmod 644 "$file"
}

setup_nginx_site() {
  local domain="$1" email="$2" port="$3" paths conf link="" had_cert=0
  paths="$(nginx_site_paths)" || die "无法确定 nginx 站点目录"
  conf="${paths%% *}"; [ "$paths" != "$conf" ] && link="${paths##* }"
  if [ -f "$conf" ] && ! is_ours "$conf"; then die "$conf 已存在且不是本程序创建的，为避免覆盖已停止"; fi
  # 域名已被其他站点使用时停止，避免抢走别的网站的流量（跳过本项目自己的文件）
  local domain_re="${domain//./\\.}"
  if nginx -T 2>/dev/null | awk -v f="$conf:" '/^# configuration file /{ skip = ($4 == f) } !skip' | \
       grep -Eq "server_name[^;]*[[:space:]]${domain_re}([[:space:];]|$)"; then
    die "域名 $domain 已在现有 nginx 配置中使用，为避免影响其他网站已停止"
  fi

  rollback_nginx() {
    rm -f -- "$conf"; [ -n "$link" ] && [ -L "$link" ] && rm -f -- "$link"
    nginx -t >/dev/null 2>&1 && nginx_reload >/dev/null 2>&1 || true
  }

  # 1) 先启用只含 HTTP 的站点，用于证书验证
  mkdir -p "$ACME_ROOT"
  write_nginx_conf "$conf" "$domain" "$port" http
  [ -n "$link" ] && ln -sfn "$conf" "$link"
  if ! nginx -t >/dev/null 2>&1; then rollback_nginx; die "nginx 配置检查未通过，已恢复原配置"; fi
  nginx_reload

  # 2) 申请证书（certbot 不存在时安装；已有证书则沿用）
  if ! command -v certbot >/dev/null 2>&1; then
    if ! install_pkgs certbot >/dev/null 2>&1 || ! command -v certbot >/dev/null 2>&1; then
      rollback_nginx; die "无法安装 certbot（CentOS/Rocky 需要先启用 EPEL 源），已恢复原配置"
    fi
  fi
  [ -f "$LE_DIR/live/$domain/fullchain.pem" ] && had_cert=1
  if [ "$had_cert" = 0 ]; then
    if ! certbot certonly --webroot -w "$ACME_ROOT" -d "$domain" --email "$email" --agree-tos -n \
         --deploy-hook "systemctl reload nginx" >/dev/null 2>&1; then
      rollback_nginx; die "证书申请失败（请确认域名已解析到本机、80 端口可从外网访问），已恢复原配置"
    fi
    state_set CERTBOT_CERT "$domain"
  fi

  # 3) 启用完整站点（HTTPS + 转发）
  write_nginx_conf "$conf" "$domain" "$port" full
  if ! nginx -t >/dev/null 2>&1; then rollback_nginx; die "nginx 配置检查未通过，已恢复原配置"; fi
  nginx_reload
  state_set PROXY_MODE nginx
  state_set NGINX_CONF "$conf"
  [ -n "$link" ] && state_set NGINX_LINK "$link"
  ok "已在现有 nginx 中添加站点 $domain（文件 $conf），证书由 certbot 自动续期"
}

# ---------------------------------------------------------------- 手动模式
manual_instructions() {
  local domain="$1" port="$2"
  state_set PROXY_MODE manual
  warn "服务器上已有其他程序占用 80/443 端口，脚本不会自动改动它的配置。"
  info "本程序已在本机启动：http://127.0.0.1:$port（只能从服务器本机访问）"
  info "请在你现有的面板或 Web 服务中，为域名 $domain 添加反向代理："
  info "  · 目标地址：http://127.0.0.1:$port"
  info "  · 需要传递的请求头：Host、X-Forwarded-For（客户端 IP）、X-Forwarded-Proto（https）"
  info "  · 为该域名申请 SSL 证书并开启 HTTPS"
  info "宝塔面板：网站 → 添加站点（填 $domain）→ 设置 → 反向代理 → 目标 URL 填 http://127.0.0.1:$port → SSL → Let's Encrypt 申请"
  info "卸载本程序后，请在面板中删除该站点和证书。"
}
