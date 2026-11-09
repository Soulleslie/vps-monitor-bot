FROM python:3.12-slim
# 本程序镜像的标记：一键更新后清理旧版本镜像、卸载时找出全部镜像，都靠它识别，不会误删其他程序的镜像
LABEL io.github.soulleslie.vps-restock-bot="1"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# 内置节点客户端（mihomo）：“抓取代理”里粘贴节点分享链接（anytls / vless / vmess / trojan / ss / hysteria2 / tuic）时使用。
# 从官方 GitHub 发布页下载，并校验 SHA-256；校验不一致时构建失败，不会使用来历不明的文件。
# amd64 使用兼容版（可在较老的 CPU 上运行）。其他架构跳过（节点链接功能不可用，其余功能不受影响）。
ARG MIHOMO_VERSION=v1.19.33
ARG MIHOMO_SHA256_AMD64=386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6
ARG MIHOMO_SHA256_ARM64=ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) file="mihomo-linux-amd64-compatible-${MIHOMO_VERSION}.gz"; sum="$MIHOMO_SHA256_AMD64" ;; \
      arm64) file="mihomo-linux-arm64-${MIHOMO_VERSION}.gz"; sum="$MIHOMO_SHA256_ARM64" ;; \
      *) echo "架构 $arch 没有内置节点客户端，跳过"; exit 0 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/mihomo.gz')" \
      "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/${file}"; \
    echo "${sum}  /tmp/mihomo.gz" | sha256sum -c -; \
    gzip -dc /tmp/mihomo.gz > /usr/local/bin/mihomo; \
    chmod 755 /usr/local/bin/mihomo; \
    rm -f /tmp/mihomo.gz

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
RUN useradd -r -u 10001 app && mkdir -p /app/data /app/run && chown app /app/data /app/run
USER app
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('WEB_PORT','8080'), timeout=4)"
CMD ["python", "-m", "bot"]
