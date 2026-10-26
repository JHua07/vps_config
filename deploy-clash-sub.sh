#!/usr/bin/env bash
# 部署 Clash 订阅生成服务（subgen）
#
# 作用：3x-ui 的订阅只含节点列表，这个服务把它包装成完整配置
#       （DNS / 分流规则 / 代理组 / 性能项），效果对齐商业机场。
#
# 端口分工：
#   8080 -> subgen（对外，TLS）
#   2096 -> x-ui 订阅（收归 127.0.0.1，仅内网）
#
# 用法：./deploy-clash-sub.sh
set -euo pipefail

die()  { echo "错误：$*" >&2; exit 1; }
info() { echo "[*] $*"; }

[[ $EUID -eq 0 ]] || die "请用 root 运行"
cd "$(dirname "$0")/.."   # 仓库根目录

systemctl is-active --quiet x-ui || die "x-ui 未运行，先装好 3x-ui"
command -v python3 >/dev/null || die "未安装 python3"
python3 -c "import yaml" 2>/dev/null || apt install -y python3-yaml

# ---------- 安装 ----------
info "安装文件到 /opt/clash-sub"
mkdir -p /opt/clash-sub
install -m 644 clash-sub/template.yaml     /opt/clash-sub/template.yaml
install -m 755 clash-sub/subgen.py         /opt/clash-sub/subgen.py
install -m 644 clash-sub/clash-sub.service /etc/systemd/system/clash-sub.service

# ---------- x-ui 订阅收归内网 ----------
info "x-ui 订阅端口：0.0.0.0:8080 -> 127.0.0.1:2096"
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("/etc/x-ui/x-ui.db")
cur = c.cursor()
def setkey(k, v):
    row = cur.execute("select id from settings where key=?", (k,)).fetchone()
    if row:
        cur.execute("update settings set value=? where id=?", (v, row[0]))
    else:
        cur.execute("insert into settings (key,value) values (?,?)", (k, v))
setkey("subPort", "2096")
setkey("subListen", "127.0.0.1")
c.commit()
PY

systemctl restart x-ui
sleep 5
systemctl is-active --quiet x-ui || die "x-ui 重启失败"
ss -tln | grep -q "127.0.0.1:2096" || die "x-ui 订阅未监听 127.0.0.1:2096"
ss -tln | grep -q ":8080 " && die "8080 仍被占用（x-ui 未释放？）" || true

# ---------- 启动 ----------
info "启动 clash-sub"
systemctl daemon-reload
systemctl enable --now clash-sub
sleep 4
systemctl is-active --quiet clash-sub || { journalctl -u clash-sub -n 20 --no-pager; die "启动失败"; }
ss -tln | grep -q ":8080 " || die "clash-sub 未监听 8080"

# ---------- 验收 ----------
SUBID=$(python3 -c "
import sqlite3, json
c = sqlite3.connect('/etc/x-ui/x-ui.db')
row = c.execute('select settings from inbounds limit 1').fetchone()
print(json.loads(row[0])['clients'][0]['subId'])
")
CLASH_PATH=$(python3 -c "
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
print(dict(c.execute('select key,value from settings')).get('subClashPath', '/clash/'))")

CODE=$(curl -sk --max-time 15 -o /tmp/sub_check.yaml -w "%{http_code}" \
  "https://127.0.0.1:8080${CLASH_PATH}${SUBID}")
[[ "$CODE" == "200" ]] || { journalctl -u clash-sub -n 20 --no-pager; die "订阅返回 $CODE"; }

python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/sub_check.yaml', encoding='utf-8'))
assert d.get('proxies'), 'proxies 缺失'
assert len(d.get('rules', [])) > 3, 'rules 缺失'
assert d['dns']['enable'], 'dns 未启用'
print('验收通过: %d 节点 / %d 规则 / dns=%s' % (
    len(d['proxies']), len(d['rules']), d['dns']['enhanced-mode']))
"

info "订阅地址（与原 3x-ui 地址完全一致，客户端无需改动）："
IP=$(curl -s --max-time 6 https://api.ipify.org || echo "<你的IP>")
info "  Clash:  https://${IP}:8080${CLASH_PATH}${SUBID}"
