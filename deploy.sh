#!/usr/bin/env bash
# 部署脚本。只用于全新机器，不要在已有 3x-ui 数据与端口占用的机器上执行。
set -euo pipefail

cd "$(dirname "$0")"

die()  { echo "错误：$*" >&2; exit 1; }
info() { echo "[*] $*"; }
warn() { echo "[!] $*" >&2; }

# ---------------- 前置检查 ----------------
[[ $EUID -eq 0 ]] || die "请用 root 运行"
command -v docker >/dev/null || die "未安装 docker：curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "未安装 docker compose 插件"
[[ -f .env ]] || die "缺少 .env，先执行 cp .env.example .env 并填写"

set -a; source .env; set +a

: "${XRAY_PORT:?}" "${NGINX_PORT:?}" "${XUI_PORT:?}" "${SSH_PORT:?}"

# 443 / 8080 / SSH 不能被别的东西占着
for p in "${XRAY_PORT}" "${NGINX_PORT}" "${SSH_PORT}"; do
  if ss -tlnH "sport = :$p" 2>/dev/null | grep -q .; then
    warn "端口 $p 已被占用："
    ss -tlnp "sport = :$p" 2>/dev/null || true
    die "先释放端口 $p 再继续"
  fi
done

# ---------------- 目录 ----------------
info "创建目录"
mkdir -p x-ui/db x-ui/cert nginx/cert nginx/conf.d fail2ban
chmod 700 x-ui/cert nginx/cert

# ---------------- 证书 ----------------
if [[ ! -f nginx/cert/fullchain.pem ]]; then
  if [[ -n "${PANEL_DOMAIN:-}" && "${PANEL_DOMAIN}" != "panel.example.com" \
        && -n "${ALICLOUD_ACCESS_KEY:-}" && -n "${ALICLOUD_SECRET_KEY:-}" ]]; then
    info "用阿里云 DNS-01 签发证书（不占用 80 端口）"
    [[ -d /root/.acme.sh ]] || curl -s https://get.acme.sh | sh -s email="${ACME_EMAIL}"
    export Ali_Key="${ALICLOUD_ACCESS_KEY}" Ali_Secret="${ALICLOUD_SECRET_KEY}"
    /root/.acme.sh/acme.sh --issue --dns dns_ali \
      -d "${PANEL_DOMAIN}" ${SUB_DOMAIN:+-d "${SUB_DOMAIN}"} --force \
      || die "证书签发失败：检查 AccessKey 权限与域名解析"
    /root/.acme.sh/acme.sh --install-cert -d "${PANEL_DOMAIN}" \
      --key-file       "$PWD/nginx/cert/privkey.pem" \
      --fullchain-file "$PWD/nginx/cert/fullchain.pem" \
      --reloadcmd      "docker restart nginx"
    chmod 600 nginx/cert/privkey.pem
  else
    warn "未配置域名或 AccessKey，跳过自动签证书"
    warn "请手动放置 nginx/cert/fullchain.pem 与 privkey.pem，否则不启动 nginx"
    warn "此时面板只能通过 https://<IP>:${XUI_PORT} 访问"
  fi
fi

# ---------------- x-ui ----------------
info "启动 x-ui（host 网络）"
docker compose -f docker-compose.x-ui.yml up -d

info "等待面板初始化"
for _ in $(seq 1 30); do
  docker logs x-ui 2>&1 | grep -q "Panel Installation Complete" && break
  sleep 2
done

echo
info "============ 面板凭据（只显示一次，立刻保存）============"
docker logs x-ui 2>&1 | sed -n '/Panel Installation Complete/,/====*$/p' || true
docker exec x-ui x-ui setting -show true 2>/dev/null || true
info "======================================================="
echo

info "应用 .env 中的端口与路径"
docker exec x-ui x-ui setting -port "${XUI_PORT}" || warn "设置端口失败，手动在面板里改"
docker exec x-ui x-ui setting -webBasePath "${XUI_BASE_PATH}" || warn "设置路径失败"
# 只监听回环，由 nginx 对外。旧版本可能不支持该参数
docker exec x-ui x-ui setting -webListen 127.0.0.1 2>/dev/null \
  || warn "webListen 设置失败，请手动在面板「面板设置」里改"
docker restart x-ui >/dev/null
sleep 5

# ---------------- nginx ----------------
if [[ -f nginx/cert/fullchain.pem ]]; then
  info "校验 nginx 配置"
  docker compose -f docker-compose.nginx.yml run --rm --entrypoint nginx nginx -t \
    || die "nginx 配置有误，修正 nginx/conf.d/panel.conf 后重试"
  info "启动 nginx"
  docker compose -f docker-compose.nginx.yml up -d
else
  warn "无证书，跳过 nginx"
fi

# ---------------- fail2ban（宿主机原生，不进容器）----------------
# 理由：fail2ban 操作宿主机 nftables、读宿主机日志，容器化只增加边界不增加隔离。
info "配置 fail2ban（宿主机原生）"
if ! command -v fail2ban-client >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban >/dev/null 2>&1 || \
    warn "fail2ban 安装失败，请手动 apt install fail2ban"
fi

if command -v fail2ban-client >/dev/null; then
  # 先判断后端：无 auth.log 的镜像必须用 systemd，否则 jail 静默失效
  if [[ -f /var/log/auth.log ]]; then
    info "检测到 /var/log/auth.log，使用 file 后端"
    install -m 644 fail2ban/99-custom.conf /etc/fail2ban/jail.d/99-custom.conf
  else
    warn "没有 /var/log/auth.log，改用 systemd 后端"
    sed -e 's/^backend = file/backend = systemd/' \
        -e '/^logpath = \/var\/log\/auth.log/d' \
        fail2ban/99-custom.conf > /etc/fail2ban/jail.d/99-custom.conf
    chmod 644 /etc/fail2ban/jail.d/99-custom.conf
  fi

  # 把 SSH 端口替换成 .env 里的真实值，避免规则挂到错误端口
  sed -i "s/^port = 21080/port = ${SSH_PORT}/" /etc/fail2ban/jail.d/99-custom.conf

  if fail2ban-client -t >/dev/null 2>&1; then
    systemctl enable --now fail2ban >/dev/null 2>&1 || true
    systemctl restart fail2ban
    info "fail2ban 已启动"
  else
    warn "fail2ban 配置校验失败，请手动执行 fail2ban-client -t 查看原因"
  fi
fi

# ---------------- 验收 ----------------
echo
info "============ 验收 ============"
info "监听端口"
ss -tlnp 2>/dev/null | grep -E ":(${XRAY_PORT}|${NGINX_PORT}|${XUI_PORT}|${SSH_PORT}) " || warn "未发现预期监听"
info "容器状态"
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E "x-ui|nginx" || true
info "fail2ban"
if command -v fail2ban-client >/dev/null; then
  echo "  service : $(systemctl is-active fail2ban 2>/dev/null)"
  echo "  jails   : $(fail2ban-client status 2>/dev/null | grep -oP 'Jail list:\s*\K.*' || echo '-')"
fi
echo
info "接下来："
info "  1. 面板里创建 VLESS-Reality 入站，端口 ${XRAY_PORT}，SNI/Dest 用 www.apple.com"
info "  2. 云安全组放行：${XRAY_PORT} ${NGINX_PORT} ${SSH_PORT}"
info "  3. 确认 https://<域名>:${NGINX_PORT}/<webBasePath>/ 能打开后，再关闭安全组的 ${XUI_PORT}"
info "  4. 备份：tar czf backup-\$(date +%F).tar.gz x-ui/db x-ui/cert nginx/cert .env"
