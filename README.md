# VPS 补货监控

监控 VPS 商家（WHMCS 等）的商品页，**按机型**推送补货通知。

- **Web 后台**：所有管理都在网页上完成，包括添加网址、选机型、设置推送对象、检查间隔、版本更新等。
- **Telegram 机器人**：只负责推送消息。
- **一键安装**：基于 Docker + Caddy，自动申请 HTTPS 证书并自动续期。另有一键卸载清理。

## 一键安装

需要：一台能访问 Telegram 和 GitHub 的 Linux 服务器（Debian / Ubuntu / CentOS / Rocky / AlmaLinux / Fedora），以及 root 权限。

**1. 准备域名**：添加一条 A 记录，把你的域名（例如 `bot.example.com`）指向服务器 IP。

**2. 放行端口**：如果是云服务器，在控制台的安全组里放行 TCP **80** 和 **443**。

**3. 运行安装脚本**：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/vps-monitor-bot/main/scripts/install.sh)
```

按提示输入域名、邮箱和 Bot Token（Token 可以先跳过，装好后在网页里填）。脚本会自动完成：

- 检查域名是否解析到本机、80/443 端口是否空闲（避免证书申请失败，或反复失败触发 Let's Encrypt 的次数限制）
- 未安装 Docker 时自动安装
- 放行本机防火墙（ufw / firewalld）
- 构建并启动，等待 HTTPS 证书签发成功
- 显示后台地址和**访问令牌**（只显示一次，请保存）
- 安装管理命令 `vps-restock`，并启用网页一键更新

也支持免交互安装：

```bash
bash install.sh --domain bot.example.com --email you@example.com --bot-token 123456:AA... -y
```

**4. 首次配置**：

1. 打开 `https://你的域名`，用访问令牌登录。
2. **设置** → 填写 Bot Token → 点“保存并连接”（从 Telegram 的 @BotFather 发送 `/newbot` 获取）。
3. 在 Telegram 里给机器人发 `/start`，或者把它拉进群、设为频道管理员。它会自动出现在 **推送对象** 页面。
4. **监控网址** → 粘贴商品分组页网址 → 进入详情页 → 添加推送对象 → 勾选机型或填写关键词 → 保存。

## 管理命令

| 命令 | 作用 |
| --- | --- |
| `vps-restock status` | 运行状态、版本、证书到期时间 |
| `vps-restock logs` | 查看程序日志（`vps-restock logs caddy` 查看证书相关日志） |
| `vps-restock update` | 更新到最新版本（也可以在网页上一键更新） |
| `vps-restock reset-token` | 忘记访问令牌时重新生成 |
| `vps-restock backup` | 备份数据库和配置到 `/root` |
| `vps-restock restart` | 重启 |
| `vps-restock uninstall` | 一键卸载清理 |

## 一键卸载

```bash
vps-restock uninstall
```

如果管理命令已经不在了，用这条：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Soulleslie/vps-monitor-bot/main/scripts/uninstall.sh)
```

卸载会：

- **先备份**：把数据库和配置打包到 `/root/vps-restock-backup-时间.tar.gz`，仅 root 可读。加 `--no-backup` 可跳过备份。
- 删除容器、镜像、数据卷（含证书）、安装目录、管理命令和网页更新服务。
- 只撤销**安装时添加**的防火墙规则；你原本就放行的端口不会被动。
- **默认保留 Docker**：只有 Docker 是安装脚本装的、且服务器上已没有其他容器时，才会询问是否一并卸载（也可用 `--purge-docker` 参数）。

## 功能

| 功能 | 说明 |
| --- | --- |
| 自定义网址 | 粘贴商品分组页网址，自动识别页面上的全部机型 |
| 按机型监控 | 每个推送对象可以分别勾选机型，也可以选“全部机型” |
| 关键词 | 例如 `HK 2C`：以后上架、名称匹配的机型也会推送 |
| 多个推送对象 | 私聊、群组、频道，各自的订阅互相独立 |
| 补货推送 | 只在 **缺货 → 有货** 时推送，附价格、库存和“直接下单”按钮 |
| **售罄自动更新** | 每个机型单独一条消息；卖完后原消息自动改成“🔴 售罄”，购买链接加删除线 |
| 推送内容自定义 | 自动识别地区、线路、配置并整理价格格式；可按网址设置商家名、名称前缀、优惠码、推广参数，按机型覆盖任意一项，网页上实时预览 |
| 自定义检查间隔 | 全局默认值和单个网址都可以设置，最少 5 秒；从每次检查开始时计时，调度精度约 1 秒 |
| 实时后台 | 概览、网址列表、详情页每 15 秒自动刷新；近 14 天补货趋势图；按名称搜索、按状态筛选 |
| **网页一键更新** | 自动提示新版本和更新内容，点击即可更新，进度实时显示 |
| 令牌管理 | 网页上可以更换或清除 Bot Token；可以更换访问令牌，旧令牌立即失效，其他设备的登录全部注销 |
| 异常提醒 | 连续抓取失败时通知一次，恢复后也会通知；失败期间自动降低检查频率 |

### 推送消息示例

补货时：

```
🟢 补货提醒

🏷 AndNode-美国云-标准型
💰 ¥18.00 CNY / month
📍 🇺🇸 美国 · 洛杉矶
🌐 9929/CMIN2
🎟 andnode（八折）

💻 2 Core / 2GB / 30GB / 无限流量@30Mbps

🛒 购买：https://cloud.andnode.com/cart?action=configureproduct&pid=11

#andnode #US #LAX #9929 #CMIN2 #优化线路 #补货
```

卖完后，**同一条消息**会自动改成下面这样（实际消息里购买链接带删除线）：

```
🔴 售罄

🏷 AndNode-美国云-标准型
...
🛒 购买：https://cloud.andnode.com/cart?action=configureproduct&pid=11

#andnode #US #LAX #9929 #CMIN2 #优化线路 #售罄
```

### 各项内容的来源

| 行 | 自动识别 | 在哪里修改 |
| --- | --- | --- |
| 🏷 名称 | 页面上的机型名称 | 网址设置“名称前缀” + 机型“显示名称” |
| 💰 价格 | 页面价格，统一整理为 `¥18.00 CNY / month` 格式 | — |
| 📍 地区 | 在名称、描述、名称前缀中识别城市或国家（如 洛杉矶 / LAX / Los Angeles） | 机型“地区”下拉框 |
| 🌐 线路 | 识别 CN2GIA、CN2GT、9929、CMIN2、CMI、10099、4837、CUII、BGP | 机型“线路” |
| 🎟 优惠码 | — | 网址“默认优惠码”，机型可覆盖 |
| 💻 配置 | 识别核心数、内存、硬盘、流量、带宽（如 2C2G、2 vCPU、1 TB Traffic） | 机型“配置” |
| 🛒 购买 | 页面上的下单链接 + 网址设置的“推广参数” | 机型“购买链接” |
| # 标签 | 商家名、国家/城市代码、线路；含优化线路时加 #优化线路 | 网址/机型“额外标签” |

识别不到的行不显示。某一项想强制不显示时填 `-`。所有修改都可以在“编辑推送”里实时预览，只影响以后发出的消息。

## 网页一键更新的原理

程序运行在 Docker 容器里，不能（也不应该）自己重建自己。把 Docker 的控制接口（docker.sock）挂进容器虽然也能做到，但相当于把服务器 root 权限交给了网页程序，所以没有采用。现在的做法是：

1. 点击“立即更新”后，网页在共享目录 `/opt/vps-restock-bot/run/` 里写入一个更新请求文件。
2. 宿主机上的 systemd 单元 `vps-restock-update.path` 发现这个文件，执行 `vps-restock update`（与命令行更新完全相同）。
3. 更新进度写回共享目录，网页实时显示；完成后页面自动刷新。

网页只能触发“从官方仓库更新到最新版”这一个固定动作，无法执行任何其他命令。

## HTTPS 证书

- 由 Caddy 自动向 Let's Encrypt 申请，**到期前约 30 天自动续期**，不需要 certbot，也不需要定时任务。可以用 `vps-restock status` 查看到期时间。
- 证书保存在 Docker 数据卷 `caddy_data` 里。不要手动删除这个卷，频繁重新申请可能触发 Let's Encrypt 的次数限制。
- 自动把 HTTP 跳转到 HTTPS，并启用 HSTS。
- 如果域名开启了 Cloudflare 代理（橙色云朵），请把 Cloudflare 的 SSL 模式设为 **Full (strict)**；安装时加 `--skip-dns-check` 跳过解析检查。
- 证书申请失败的常见原因：域名没有解析到本机；云服务器安全组没有放行 80/443；80/443 端口被 nginx 等程序占用。

## 安全设计

- 网页后台只能用访问令牌登录。同一 IP 15 分钟内失败 10 次会被锁定。数据库里只保存令牌的哈希值。
- 会话 Cookie 带有 HttpOnly、Secure、SameSite=Strict 属性；所有修改操作都要求带自定义请求头，防止跨站请求伪造（CSRF）；页面启用了 CSP 等安全响应头。
- 网页上的 Bot Token 只显示打码后的版本。
- 拒绝监控内网/本机地址，防止服务器被利用来探测内网（SSRF）。
- 来自商家网站的文本全部转义后再显示。
- 程序在容器里以普通用户运行；8080 端口不对外开放，只有 Caddy 能访问。
- `.env` 的权限为 600（仅 root 可读）。

## 库存识别逻辑

1. WHMCS 官方模板（six / twenty-one 及衍生主题）：根据 `#productN` 容器和 `.qty`（如“5 Available” / “5 可用”）判断。
2. 第三方主题（如 Lagom）：以下单链接（`cart.php?a=add&pid=` 或 `/store/分组/机型`）为锚点，定位每个商品卡片。
3. 缺货判断依据：库存为 0、缺货关键字（Out of Stock / Sold Out / 缺货 / 售罄 等）、缺货样式、下单按钮被禁用。
4. 商家隐藏缺货商品时，商品从页面消失记为缺货，重新出现记为补货。
5. 页面不显示库存的站点，会访问对应机型的购物车链接确认（可在网址详情页关闭）。
6. 防误报：判断不出的状态不会触发通知；首次添加只记录不推送；同一机型在冷却时间内重复补货只推送一次。

## 注意事项

- 检查间隔越短，越容易被商家封 IP，低于 30 秒时网页上会显示提醒。同一域名下的多个网址，请求之间至少间隔 3 秒（`HOST_MIN_GAP`）。
- 开启了 Cloudflare 人机验证的商家无法抓取，网页上会显示“被 Cloudflare 拦截”。可以换一台 IP 信誉更好的服务器，或者设置 `FETCH_PROXY`。
- 目前只支持商品分组/列表页，不支持单个商品的下单链接。
- 网页上保存的设置（包括 Bot Token）优先于 `.env`。

## 备份与恢复

```bash
vps-restock backup
```

恢复步骤（先用一键安装脚本装好程序；以下命令都在 `/opt/vps-restock-bot` 目录中执行，替换数据库前必须先停止程序）：

1. 解压备份：
   ```bash
   mkdir -p /root/restore && tar -xzf /root/vps-restock-backup-时间.tar.gz -C /root/restore
   ```
2. 停止程序：
   ```bash
   docker compose stop bot
   ```
3. 放回数据库（用临时容器完成复制，设置正确的文件所有者，并清除旧的 WAL 日志）：
   ```bash
   docker compose run --rm --no-deps -u 0 -v /root/restore/data:/restore:ro bot sh -c "cp /restore/bot.db /app/data/bot.db && rm -f /app/data/bot.db-wal /app/data/bot.db-shm && chown 10001:10001 /app/data/bot.db"
   ```
4. 放回配置：
   ```bash
   cp /root/restore/env .env && chmod 600 .env
   ```
5. 启动：
   ```bash
   docker compose up -d
   ```

## 不用 Docker 部署（高级）

需要 Python 3.11 及以上：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env && .venv/bin/python -m bot
```

长期运行可参考 `deploy/vps-restock-bot.service`（systemd）。HTTPS 参考 `deploy/Caddyfile.host`。这种部署方式不支持网页一键更新。

## 开发

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```
