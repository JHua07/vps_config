# VPS 代理节点部署手册

3x-ui（VLESS-Reality）+ nginx 反代 + fail2ban 的完整部署配置与踩坑记录。

**适用**：Debian 12 / Ubuntu 22.04+，KVM 虚拟化，1 核 1G 起。
**已验证环境**：Ubuntu 24.04.5，Kernel 6.8，Xray 26.10.21，3x-ui 3.11.0（东京 VPS，原生安装）。

> 本文档的容器化配置**尚未在真实机器上跑通过**（编写时本机 Docker daemon 未运行），
> 语法已校验但未经运行验证。原生安装部分是实际验证过的，见「原生部署」章节。

---

## 目录

- [快速开始](#快速开始)
- [端口规划](#端口规划)
- [踩坑记录（重要）](#踩坑记录重要)
- [原生部署（已实测）](#原生部署已实测)
- [容器化部署](#容器化部署)
- [节点配置](#节点配置)
- [订阅配置](#订阅配置)
- [证书与续期](#证书与续期)
- [fail2ban](#fail2ban)
- [云厂商安全组](#云厂商安全组)
- [故障排查](#故障排查)
- [运维清单](#运维清单)
- [安全注意事项](#安全注意事项)

---

## 快速开始

### 最短路径（原生安装，已验证）

```bash
# 1. 系统准备
apt update && apt -y full-upgrade
apt -y install curl ca-certificates vim htop

# 2. 开 BBR（跨境线路吞吐提升明显）
cat > /etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
sysctl --system && sysctl net.ipv4.tcp_congestion_control   # 必须输出 bbr

# 3. 装 3x-ui
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
# 安装时全部改成随机值：面板端口、webBasePath、用户名密码

# 4. 验证
systemctl is-active x-ui
ss -tlnp | grep x-ui
```

安装输出里的 **API Token 务必保存**，它是绕过面板 UI 直接操作配置的凭证：

```bash
curl -sk -H "Authorization: Bearer <Token>" \
  "https://IP:端口/webBasePath/panel/api/inbounds/list"
```

### 容器化路径

见 [容器化部署](#容器化部署)。

---

## 端口规划

这套配置的端口分工是踩过坑之后定下来的，不要随意改：

| 端口 | 用途 | 监听地址 | 是否对公网 |
|---|---|---|---|
| `443` | VLESS-Reality 节点 | `0.0.0.0` | 是（安全组必开） |
| `8080` | nginx 反代（面板 + 订阅） | `0.0.0.0` | 是（安全组必开） |
| `SSH_PORT` | SSH，随机高位 | `0.0.0.0` | 是（安全组必开） |
| `11297` | x-ui 面板本体 | **`127.0.0.1`** | **否**（由 nginx 反代） |
| `80` | ACME standalone 续期 | 临时占用 | 设备用证书时需要 |

### 三条硬约束

**1. 443 只给 Reality，不要让 nginx 在 443 上做 SNI 分流。**
Reality 对 TLS 指纹敏感，nginx 在前面做 TLS 终止会破坏伪装特征，配错一次节点直接不可用。
收益不抵风险。

**2. 订阅端口不能等于面板端口。**
两者相同会导致 x-ui 启动时报：

```
Error starting sub server: listen tcp :11297: bind: address already in use
```

然后 systemd 反复重启，`systemctl is-active` 显示 `activating` → `failed`，面板整体不可用。
这个故障很隐蔽，因为服务看起来"在启动"。

**3. 80 端口必须保持空闲（如果用了 standalone 证书）。**
acme.sh 的 standalone 模式续期时要独占 80。被订阅服务或 nginx 占用会导致续期失败，
证书到期后面板和订阅全部报证书错误。
**改用 DNS-01 签证书可以彻底解除这个约束。**

---

## 踩坑记录（重要）

以下都是实际遇到并解决的问题，按出现频率排序。

### 1. `http://` 打不开面板

面板和订阅服务**只接受 HTTPS**。如果安装时配了证书，访问 `http://IP:端口/路径` 会被直接断连，
`curl` 返回 `000`（不是 404、不是 301，是连接被重置）。

```bash
# 错
curl http://IP:11297/path/     # code=000

# 对
curl -k https://IP:11297/path/ # code=200
```

判断方法：`curl -v` 看是否 `Connection reset`。这极易被误判为"服务挂了"或"防火墙挡了"。

### 2. 改防火墙后 SSH 连不上

**症状**：`ssh: connect to host X port N: Connection timed out`

**原因**：`ufw default deny incoming` + `ufw allow 22/tcp`，但 SSH 实际不在 22。
丢包（timeout）说明被防火墙 DROP；`Connection refused` 说明端口没监听。

**正确顺序**：

```bash
# 先确认真实端口！
ss -tlnp | grep sshd
systemctl cat ssh.socket | grep ListenStream   # Ubuntu 22.04+ socket 激活时以此为准

# 再放行，最后才 enable
ufw allow <真实端口>/tcp
ufw enable
# 保持旧连接开着，新开终端验证能进，再关闭旧的
```

**恢复**：走云厂商 VNC / 控制台。**本地 tty 登录走 PAM，不受 sshd 的
`PasswordAuthentication no` 影响**，所以即使做了 SSH 加固这里也能登。

### 3. 两层防火墙

**云厂商安全组**和**系统内 ufw/iptables** 是独立的两层，任何一层拦了都不通。

排查时先看系统内：

```bash
ufw status verbose
iptables -S | head -20
nft list ruleset | head -30
```

如果系统内全是 ACCEPT / inactive，问题一定在云安全组。

**探测端口状态的方法**（区分 DROP 和 RST）：

```bash
for p in 80 443 8080 11297; do
  rc=$(timeout 5 bash -c "exec 3<>/dev/tcp/IP/$p" 2>/dev/null; echo $?)
  case $rc in
    0)   echo "$p OPEN" ;;
    124) echo "$p DROPPED (防火墙丢包)" ;;
    *)   echo "$p RST (端口可达但无监听)" ;;
  esac
done
```

这个判断非常关键：
- **DROPPED** = 有防火墙规则在丢包（安全组 / ufw / nftables）
- **RST** = 包到达了主机，但没有程序在监听

### 4. `fail2ban-client` 在非交互 SSH 下挂起

`ssh host "fail2ban-client status"` 会卡住不返回。**必须用交互式登录**执行，
或者确认 stdin 已关闭。

```bash
# 会卡住
ssh host "fail2ban-client status sshd"

# 可用
ssh host
fail2ban-client status sshd
```

### 5. `fail2ban-client set banip` 绕过白名单

这是我实际造成的一次事故：

```bash
fail2ban-client set sshd banip <你的出口IP>   # 本意是测白名单
```

**`set banip` 是强制命令，设计上绕过 `ignoreip` 检查**，白名单只在自动检测时生效。
结果把自己的出口 IP 封了一小时。

**教训**：
- 永远不要拿自己的 IP 测白名单
- 要测就用 `198.51.100.x`（RFC5737 测试网段）
- `bantime` 设短（10m）比设长安全，误封代价小

**解封**（如果 SSH 也被封，需要从另一台机器或 VNC 进）：

```bash
fail2ban-client set sshd unbanip <你的IP>
```

### 6. 3x-ui 自带的 `3x-ipl` jail 会封掉节点端口

它安装时自动创建，ban 动作是：

```
iptables -I f2b-3x-ipl -s <ip> -p tcp -m multiport ! --dports 21080,11297 -j REJECT
```

即**封禁除 SSH 和面板端口之外的所有端口**。一旦触发，443 节点和 8080 订阅一起挂。

而它的触发条件是客户端并发 IP 超限，`limitIp=0` 时永远不成立。
**建议直接禁用**（配置里已禁用）：

```ini
[3x-ipl]
enabled = false
```

### 7. 订阅地址里的 `localhost`

3x-ui 生成订阅时，如果请求的 Host 不在信任列表里，会用 `localhost` 或本机地址。
生成的链接会是 `vless://uuid@localhost:443?...`，客户端无法使用。

**解决**：在面板「面板设置 → 订阅」里设置 **订阅域名 / URI**，
或者确保客户端用真实域名访问订阅。

### 8. 面板能开但订阅 404

订阅有**两套路径**，Clash 和 base64 格式不同：

```
subPath       -> base64 列表（v2rayN / Shadowrocket）
subClashPath  -> Clash YAML（Clash Verge / mihomo）
subJsonPath   -> sing-box JSON
```

用错路径就 404。路径在面板数据库里：

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
for k, v in c.execute(\"select key,value from settings where key like '%Path%' or key like '%path%'\"):
    print(k, '=', v)
"
```

### 9. 格式协商失败返回 base64

客户端带 `User-Agent: clash` 却拿到 base64，说明 **Clash 订阅没启用**：

```bash
# 必须为 true
python3 -c "
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
print(list(c.execute(\"select key,value from settings where key='subClashEnable'\")))
"
```

改完 `systemctl restart x-ui` 生效。

### 10. Reality 的 dest 选择

Xray 启动时会警告：

```
WARNING - XRAY: infra/conf: REALITY: Choosing "www.microsoft.com" as the target
will increase the likelihood of your server's IP being blocked by the GFW
```

选 dest 的要求：

- 支持 **TLS 1.3**（必须）
- 支持 **HTTP/2 / ALPN h2**（必须）
- 证书链可信、非自签
- 地理上离 VPS 近（降低客户端到目标站的延迟，让伪装更自然）
- **不要用明显被针对的域名**

验证方法：

```bash
openssl s_client -connect HOST:443 -servername HOST -alpn h2 -tls1_3 </dev/null 2>/dev/null \
  | grep -E "Protocol|ALPN|Verify return code"
```

推荐（东京/日本机器实测可用）：`www.apple.com`、`www.cloudflare.com`、`www.bing.com`。

---

## 原生部署（已实测）

完整流程，按顺序执行。

### 1. 系统准备

```bash
apt update && apt -y full-upgrade && apt -y install curl ca-certificates vim htop

# 内存 ≤ 2G 建议加 swap
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
echo 'vm.swappiness = 10' > /etc/sysctl.d/99-swap.conf && sysctl -p /etc/sysctl.d/99-swap.conf

timedatectl set-timezone Asia/Shanghai
timedatectl   # System clock synchronized 必须是 yes，Reality 对时间敏感
```

### 2. BBR

```bash
cat > /etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF
sysctl --system
sysctl -n net.ipv4.tcp_congestion_control   # 必须输出 bbr
```

### 3. 防火墙

```bash
# 先确认 SSH 真实端口再操作！
ufw default deny incoming
ufw default allow outgoing
ufw allow <SSH真实端口>/tcp
ufw allow 443/tcp
ufw allow 80/tcp        # ACME standalone 续期需要
ufw allow 8080/tcp      # 订阅/反代
ufw enable
ufw status verbose
```

### 4. SSH 加固

**先确认密钥登录可用，再改配置。**

```bash
# 本地
ssh-keygen -t ed25519 -C "vps"
ssh-copy-id -p PORT root@IP
ssh -p PORT root@IP        # 确认免密能进
```

服务器上（注意 Ubuntu 24.04 云镜像的 `50-cloud-init.conf` 优先级问题，
必须用**编号更大**的文件）：

```bash
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
EOF
sshd -t && sshd -T | grep -i passwordauth
```

**注意**：某些镜像在 `/etc/ssh/sshd_config` 里硬编码了 `PubkeyAuthentication no`
（不只是注释掉）。这会完全禁用公钥认证，`ssh-copy-id` 之后仍然连不上。
检查并修正：

```bash
grep -n "PubkeyAuthentication" /etc/ssh/sshd_config
sed -i 's/^[[:space:]]*PubkeyAuthentication[[:space:]]\+no/PubkeyAuthentication yes/' /etc/ssh/sshd_config
sshd -t && systemctl reload ssh
```

### 5. 安装 3x-ui

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
```

安装过程中全部改成随机值。**立刻保存输出的**：面板地址、用户名、密码、**API Token**。

```bash
systemctl is-active x-ui
x-ui settings          # 显示当前配置
ss -tlnp | grep x-ui
```

日志级别设为 `warning`、关闭访问日志（小内存机器必做，否则磁盘会被占满）。

### 6. 建入站

面板「入站列表 → 添加」，或直接走 API（更方便自动化）：

```bash
TOKEN="<API Token>"
BASE="https://IP:端口/webBasePath"

curl -sk -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @inbound.json \
  "$BASE/panel/api/inbounds/add"
```

`inbound.json` 结构参考 [示例](#入站配置示例)。

### 7. 备份

```bash
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak-$(date +%F)
tar czf /root/vps-backup-$(date +%F).tar.gz /etc/x-ui/x-ui.db /root/cert
```

---

## 容器化部署

### 为什么 fail2ban 不容器化

fail2ban 天生要操作宿主机 nftables、读宿主机日志。装进容器需要
`NET_ADMIN` + `network_mode: host` + 挂载 `/var/log`，**边界全打开但零隔离收益**。
反而可能和宿主机已有实例争抢同一个 `f2b-table`，导致规则互相覆盖。

**结论**：只容器化 x-ui 和 nginx，fail2ban 用 `apt install`。

### 目录结构

```
vps_config/
├── .env.example                  配置模板
├── deploy.sh                     部署脚本
├── docker-compose.x-ui.yml       面板 + Xray + 订阅
├── docker-compose.nginx.yml      反代 + 登录限流
├── x-ui/
│   ├── db/                       SQLite（备份这个）
│   └── cert/
├── nginx/
│   ├── conf.d/panel.conf         反代与限流
│   └── cert/
└── fail2ban/
    └── 99-custom.conf            宿主原生配置
```

### 部署

```bash
apt install -y docker.io docker-compose-plugin   # 或用 get.docker.com 脚本

git clone https://github.com/JHua07/vps_config.git && cd vps_config
cp .env.example .env && vim .env
chmod +x deploy.sh && ./deploy.sh
```

`deploy.sh` 会做：端口占用检查 → 证书 → 起 x-ui → 打印凭据 →
`webListen 127.0.0.1` → 校验并起 nginx → 装 fail2ban → 验收输出。

### 为什么用 host 网络

`network_mode: host`，不用端口映射。原因：

1. Reality 需要在 443 上直接处理 TLS 指纹，桥接网络多一层 NAT 会干扰
2. 桥接网络下容器拿不到客户端真实 IP，**fail2ban 和限流全部失效**

代价：不能用 `ports` 段，端口直接在宿主机上。

### 容器化未验证的风险

以下都**没有实测**，首次部署时重点检查：

- 镜像能否拉取（`ghcr.io/mhsanaei/3x-ui`，支持 amd64/arm64/arm/386）
- `docker exec x-ui x-ui setting -webListen 127.0.0.1` 参数是否被当前版本支持
- nginx 容器能否读取 `chmod 600` 的证书私钥
- host 网络下 443 是否被正确占用

---

## 节点配置

### VLESS-Reality 推荐参数

| 项 | 值 | 说明 |
|---|---|---|
| 协议 | `vless` | |
| 端口 | `443` | |
| 传输 | `tcp` (RAW) | 不要用 ws/grpc，Reality 配 tcp |
| 安全 | `reality` | |
| Flow | `xtls-rprx-vision` | 必须配 tcp，配 ws 会失败 |
| Dest | `www.apple.com:443` | 见「踩坑记录 10」 |
| SNI / ServerNames | `www.apple.com` | 必须与 Dest 一致 |
| 私钥 | 面板「获取」自动生成 | |
| ShortIds | 面板「生成」 | 8 或 16 位十六进制 |

### 入站配置示例

```json
{
  "up": 0, "down": 0, "total": 0,
  "remark": "vps-443",
  "enable": true,
  "expiryTime": 0,
  "listen": "",
  "port": 443,
  "protocol": "vless",
  "settings": "{\"clients\":[{\"id\":\"<UUID>\",\"flow\":\"xtls-rprx-vision\",\"email\":\"client-1\",\"limitIp\":0,\"totalGB\":0,\"expiryTime\":0,\"enable\":true,\"subId\":\"<SUBID>\"}],\"decryption\":\"none\",\"fallbacks\":[]}",
  "streamSettings": "{\"network\":\"tcp\",\"security\":\"reality\",\"realitySettings\":{\"dest\":\"www.apple.com:443\",\"serverNames\":[\"www.apple.com\"],\"privateKey\":\"<PRIVKEY>\",\"shortIds\":[\"<SHORTID>\"]},\"tcpSettings\":{\"acceptProxyProtocol\":false,\"header\":{\"type\":\"none\"}}}",
  "sniffing": "{\"enabled\":true,\"destOverride\":[\"http\",\"tls\",\"quic\"],\"metadataOnly\":false,\"routeOnly\":false}"
}
```

注意 `settings` / `streamSettings` / `sniffing` 是 **JSON 字符串**，不是嵌套对象。

### 生成密钥

```bash
/usr/local/x-ui/bin/xray-linux-amd64 x25519
# PrivateKey: xxx
# Password (PublicKey): yyy    <- 这个值对应客户端的 pbkdf/reality public-key
```

### 端到端验证

在服务器上跑一个临时 Xray 客户端，穿过节点访问外网：

```json
{
  "log": {"loglevel": "warning"},
  "inbounds": [{"listen":"127.0.0.1","port":10808,"protocol":"socks","settings":{"udp":true,"auth":"noauth"}}],
  "outbounds": [{
    "protocol": "vless",
    "settings": {"vnext": [{"address":"<IP>","port":443,"users":[{"id":"<UUID>","encryption":"none","flow":"xtls-rprx-vision"}]}]},
    "streamSettings": {
      "network": "tcp", "security": "reality",
      "realitySettings": {"serverName":"www.apple.com","fingerprint":"chrome","publicKey":"<PUBKEY>","shortId":"<SHORTID>","spiderX":"/"}
    }
  }]
}
```

```bash
nohup /usr/local/x-ui/bin/xray-linux-amd64 run -c /tmp/c.json > /tmp/c.log 2>&1 &
sleep 4
curl -s --socks5-hostname 127.0.0.1:10808 https://api.ipify.org   # 应返回 VPS IP
curl -s --socks5-hostname 127.0.0.1:10808 https://www.gstatic.com/generate_204 -o /dev/null -w "%{http_code}\n"  # 应 204
pkill -f xray   # 清理
```

### 验证 Reality 伪装

从外部连 443 并带 SNI，应返回**目标站的真实证书**（不是自签证书）：

```bash
openssl s_client -connect IP:443 -servername www.apple.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer
# subject=... O=Apple Inc., CN=www.apple.com
```

如果返回自签证书或握手失败，说明 Reality 没生效。

---

## 订阅配置

### 让订阅返回完整配置（subgen）

3x-ui 的订阅**只含节点列表**：Clash 路径返回的 YAML 只有 20 多行，
`rules` 仅有 `MATCH,PROXY`，没有 DNS 段、代理组、性能项。
这与商业机场差距最大的部分。

3x-ui 自带的 `subClashRules` 注入**只能补 rules，补不了 DNS 和 sniffer**。

解决方案是 `clash-sub/` 里的 **subgen** 服务：

```
Clash 客户端
    │ https://IP:8080/<subClashPath>/<SubID>     （地址与原 3x-ui 完全一致）
    ▼
subgen (:8080, 对外, TLS)
    │ 拉取 https://127.0.0.1:2096/<subClashPath>/<SubID>   （仅内网）
    │ 提取 proxies，注入 DNS / proxy-groups / rules / 性能项
    ▼
返回完整 Clash YAML（约 190 行）
```

**部署**：

```bash
./deploy-clash-sub.sh
```

脚本会自动：把 x-ui 订阅收归 `127.0.0.1:2096`（对外端口 8080 让给 subgen，
**不需要改云安全组**）→ 安装 systemd 服务 → 用真实 SubID 验收。

**subgen 提供的配置**（见 `clash-sub/template.yaml`）：

- DNS：fake-ip 模式，国内外域名分流解析（`nameserver-policy` 按 `geosite` 区分），
  `proxy-server-nameserver` 单独指定避免解析死循环
- 分流规则：GEOSITE/GEOIP 内置规则——局域网直连、广告拦截、国内直连、国外走代理
- 代理组：手动选择 / 自动选择（url-test）/ 故障转移（fallback）
- 性能项：`unified-delay`、`tcp-concurrent`、`global-client-fingerprint`
- sniffer：域名嗅探，配合 fake-ip 修正分流依据
- GEO 数据自动更新（每 168 小时）

**两种格式的行为**：

| 路径 | 行为 |
|---|---|
| `<subClashPath>/<SubID>` | 完整 Clash YAML |
| `<subPath>/<SubID>` | 3x-ui 原始 base64 直通（v2rayN 等用） |

地址与原 3x-ui 完全一致，已导入的客户端无需改任何东西。

**证书更新**：acme.sh 续期后 subgen 通过监控证书文件 mtime 自动原地重启（`os.execv`），
不需要额外配置 reloadcmd。

**调试**：`journalctl -u clash-sub -n 50 --no-pager`

**踩过的坑**（写代码时实际犯的错，改模板时注意）：

1. 模板**注释里不要出现 `{{PROXIES}}` 字样**——`str.replace` 会把注释里的
   占位符也替换掉，注入的节点块从注释中间展开，YAML 直接解析失败。
   subgen 已改为只替换「整行占位符」。
2. `yaml.safe_dump(单个字典)` 不带列表的 `- ` 标记，逐节点 dump 后要手动在
   首行补 `  - `，否则 `proxies` 会被解析成映射而不是列表。
3. 上游要拉 **Clash 路径**（返回 YAML），不是 base64 路径（返回编码文本）。

### 数据库中的关键字段

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
for k, v in c.execute(\"select key,value from settings order by key\"):
    if 'sub' in k.lower() or 'web' in k.lower():
        print(k, '=', v)
"
```

| 字段 | 作用 |
|---|---|
| `subEnable` | 总开关 |
| `subPort` | 订阅端口，**不能等于 webPort** |
| `subPath` | base64 格式路径 |
| `subClashEnable` | Clash 格式开关，false 时返回 base64 |
| `subClashPath` | Clash 格式路径 |
| `subJsonEnable` / `subJsonPath` | sing-box 格式 |
| `subURI` / `subDomain` | 订阅对外地址，影响生成的链接 |

### 订阅地址

```
https://域名或IP:端口<子路径>/<SubID>
```

**验证**：

```bash
# Clash 格式，应返回 YAML（proxies: / proxy-groups: / rules:）
curl -k -H "User-Agent: clash-verge/v2.0.0" "https://IP:PORT<clashPath>/<SubID>"

# base64 格式，解码后是 vless:// 链接
curl -k -H "User-Agent: v2rayN/6.0" "https://IP:PORT<subPath>/<SubID>" | base64 -d
```

### 客户端

| 平台 | 客户端 |
|---|---|
| Windows / macOS | Clash Verge Rev（内核 mihomo，原生支持 Reality） |
| Android | FlClash / ClashMetaForAndroid |
| iOS | Stash / Shadowrocket（需外区 Apple ID） |

订阅链接等同于密码，泄露即节点泄露。

---

## 证书与续期

### 三种方案对比

| 方案 | 需要域名 | 续航期 | 是否占用 80 端口 |
|---|---|---|---|
| standalone（HTTP-01） | 否（可签 IP） | 6 天（短周期） | **是** |
| webroot（HTTP-01） | 是 | 90 天 | 是 |
| **DNS-01** | 是 | 90 天 | **否** |

### 裸 IP 证书（standalone）

3x-ui 安装时可选自动申请，签出来的是 Let's Encrypt **shortlived 短周期证书（6 天）**。

```bash
# 查看
/root/.acme.sh/acme.sh --list

# 续期配置在
cat /root/.acme.sh/<IP>_ecc/<IP>.conf
# Le_RenewalDays='6'
# Le_NextRenewTimeStr=...
# Le_ReloadCmd=<base64编码的重启命令>
```

**关键约束：续期时 standalone 模式独占 80 端口。** 所以：

- 80 端口必须保持空闲，不能被 nginx / 订阅服务占用
- 用 `ufw allow 80/tcp` 放行
- 云安全组也要放行 80

**手动续期测试**（验证续期链路是否正常）：

```bash
/root/.acme.sh/acme.sh --renew -d <IP> --force
```

### 域名证书（DNS-01，推荐）

前提：域名托管在支持 DNS API 的服务商（阿里云 / Cloudflare / DNSPod 等）。

**优势**：
- 不需要开放任何端口，续期永远不会因端口冲突失败
- 90 天证书，替代 6 天短周期
- 可以签给多个子域名

阿里云 DNS 示例：

```bash
export Ali_Key="<AccessKeyId>"
export Ali_Secret="<AccessKeySecret>"

/root/.acme.sh/acme.sh --issue --dns dns_ali \
  -d panel.example.com -d sub.example.com

/root/.acme.sh/acme.sh --install-cert -d panel.example.com \
  --key-file       /root/cert/domain/privkey.pem \
  --fullchain-file /root/cert/domain/fullchain.pem \
  --reloadcmd      "systemctl restart x-ui && docker restart nginx"
```

写进 `~/.bashrc` 或 acme.sh 的 `account.conf` 里持久化凭据，
其它机器的 `account.conf` **不要提交到 git**。

**AccessKey 权限**：只需要 `AliyunDNSFullAccess`（或者更小的自定义策略，仅允许
`AddDomainRecord` / `DeleteDomainRecord` / `DescribeDomainRecords`）。

### 证书续期后的服务重启

`--reloadcmd` 必须配置，否则证书更新了服务还在用旧的（x-ui 启动时读入内存）。

```bash
# 验证 reloadcmd 已配置
grep Le_ReloadCmd /root/.acme.sh/<domain>/<domain>.conf
```

---

## fail2ban

### Ubuntu 24.04 的后端选择（关键）

**这一步搞错不会报错，只是 jail 永远匹配不到日志、静默失效。**

```bash
ls /var/log/auth.log
```

- **存在** → `backend = file`，且 `logpath = /var/log/auth.log`
- **不存在** → `backend = systemd`，且**删掉 logpath**

Ubuntu 24.04 云镜像多数有 `auth.log`（装了 rsyslog），部分精简镜像只有 journal。

### 完整配置

见 [`fail2ban/99-custom.conf`](fail2ban/99-custom.conf)。核心项：

```ini
[DEFAULT]
backend = file              # 或 systemd，见上
banaction = nftables
bantime.increment = true
bantime.factor = 2
bantime.maxtime = 1d
bantime = 10m               # 误封代价低，不建议设太长
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1

[sshd]
enabled = true
logpath = /var/log/auth.log
port = 21080                # 必须写真实 SSH 端口！
mode = aggressive
maxretry = 3
bantime = 10m

[3x-ipl]
enabled = false             # 必须禁用，见踩坑记录 6
```

### 验证是否生效

```bash
fail2ban-client status                  # 看 jail 列表
fail2ban-client status sshd             # 看当前封禁
fail2ban-client -d | grep ignoreip      # 看白名单是否加载

# 验证封禁规则只作用于 SSH 端口
nft list table inet f2b-table
# 应看到：
#   tcp dport 21080 ip saddr @addr-set-sshd reject
```

**注意**：`fail2ban-client get sshd ignoreip` 只显示前 2 项，是显示 bug，
用 `fail2ban-client -d | grep ignoreip` 看完整列表。

### 测试封禁（安全方式）

```bash
# 用 RFC5737 测试网段，绝不拿自己的 IP
fail2ban-client set sshd banip 198.51.100.99
nft list set inet f2b-table addr-set-sshd     # 应出现该 IP
fail2ban-client set sshd unbanip 198.51.100.99
```

### 验证 filter 正则能匹配真实日志

```bash
journalctl -u ssh -q --no-pager --since "10 min ago" \
  | grep -a "sshd\[" > /tmp/s.log

fail2ban-regex /tmp/s.log /etc/fail2ban/filter.d/sshd.conf
# 看 "Failregex: N matched" 是否 > 0
```

注意 `grep -a`：`auth.log` 里可能混有二进制内容（异常登录产生的），
不加 `-a` 会被当作 binary file 跳过。

### 面板端口的防护

**x-ui 3.11 不记录登录失败日志**（只记录 CSRF 校验失败），
**也没有内置登录限流**。这意味着基于日志的 fail2ban 对面板爆破完全无效——
攻击者带着有效 CSRF token 爆破密码，日志里一条痕迹都不会留。

**有效方案**（按效果排序）：

1. **面板只监听 127.0.0.1 + SSH 隧道访问**（最安全，攻击面归零）
   ```bash
   x-ui setting -webListen 127.0.0.1 && x-ui restart
   # 本地：ssh -L 8080:127.0.0.1:11297 -p SSH端口 root@IP
   ```

2. **nginx 反代 + `limit_req` 限流**（见 `nginx/conf.d/panel.conf`）
   只对 `/login` 限流，**不能对 `location /` 限流**——
   面板是 SPA，打开页面会并发几十个 API 请求，全局限流会卡死正常使用。

3. **改用 Bearer Token 调 API**，关掉密码登录

---

## 云厂商安全组

### 与系统防火墙的关系

**两层独立，都要放行**：

```
客户端 → [云安全组] → [系统 ufw/iptables] → 服务
```

系统内 `ufw status` 显示 inactive 也不代表端口是通的——安全组可能仍在丢包。

### 排查流程

```bash
# 1. 服务端确认在监听
ss -tlnp | grep -E ":(443|8080|11297) "

# 2. 系统防火墙
ufw status verbose

# 3. 从外部探测（区分 DROP / RST）
#    DROPPED → 防火墙丢包（安全组或 ufw）
#    RST     → 包到了，但无程序监听
```

### 各厂商入口

| 厂商 | 位置 |
|---|---|
| 阿里云 | ECS 实例 → 安全组 → 配置规则 → 入方向 |
| 腾讯云 | 轻量应用服务器 → 防火墙 / CVM → 安全组 |
| Vultr / DO / Linode | 一般无独立安全组，系统内 ufw 即全部 |
| Oracle Cloud | 安全列表 + **镜像自带 iptables 规则**（额外坑） |
| AWS | EC2 → Security Groups |

**Oracle Cloud 特别注意**：Ubuntu 镜像自带 iptables REJECT 规则，
即使安全列表放行了也会被系统内规则挡掉：

```bash
iptables -L INPUT -n --line-numbers
# 如果有 REJECT 规则，需要删掉或插入 ACCEPT 到它前面
```

### 国内机器备案要求

中国大陆地域的云服务器，80/443 端口需 **ICP 备案**才能对外提供 Web 服务。
未备案会被运营商拦截。想不备案就用境外地域。

---

## 故障排查

### 决策树

**面板打不开**
```
curl -k https://IP:端口/路径  返回什么？
├─ 000        → 用了 http？或防火墙丢包？或服务没起
├─ 404        → 路径错（webBasePath 记错了）
├─ 200 但空白 → 浏览器缓存 / 资源加载失败，看 DevTools
└─ 超时       → 安全组或 ufw 拦截
```

**节点连不上**
```
1. 443 从外部能连吗？（bash /dev/tcp 测试）
   ├─ DROPPED → 安全组没放行
   └─ OPEN    → 继续
2. Reality 伪装对吗？
   openssl s_client -connect IP:443 -servername <SNI>
   └─ 应返回目标站真实证书
3. 服务器时间同步吗？
   timedatectl | grep synchronized   → 必须 yes
4. Flow 和传输匹配吗？
   xtls-rprx-vision 必须配 tcp，配 ws 会失败
5. ShortId 匹配吗？
   客户端和服务端必须完全一致
```

**订阅拉不到**
```
curl 返回什么？
├─ 404 → 路径错（Clash 用 clashPath，base64 用 subPath）
├─ 000 → http 而非 https
└─ 200 但内容不对
   ├─ base64 但想要 YAML → subClashEnable 未启用
   └─ 链接里是 localhost  → 未设置订阅域名
```

### 常用命令

```bash
# 服务状态
systemctl is-active x-ui xray
journalctl -u x-ui -n 50 --no-pager | tail -30

# 端口监听
ss -tlnp | grep -E ":(443|80|8080|11297|21080) "

# 数据库查询
python3 -c "
import sqlite3
c = sqlite3.connect('/etc/x-ui/x-ui.db')
print(list(c.execute('select id,remark,port,protocol,enable from inbounds')))
"

# x-ui 管理
x-ui              # 交互菜单
x-ui restart
x-ui settings     # 显示当前配置
x-ui log

# 备份
cp /etc/x-ui/x-ui.db /root/x-ui.db.bak-$(date +%F)
```

### 症状对照表

| 症状 | 原因 |
|---|---|
| `curl` 返回 000 | 用了 http 而非 https（面板/订阅只接受 HTTPS） |
| `x-ui` 反复重启，`bind: address already in use` | `subPort` 等于 `webPort` |
| 订阅返回 base64 而非 YAML | `subClashEnable` 为 false |
| 订阅链接里是 `localhost` | 未设置 `subURI` / `subDomain` |
| Reality 返回自签证书 | Reality 未生效，检查 dest/SNI/私钥 |
| 证书到期后全部报错 | acme.sh 续期失败，检查 80 端口是否被占 |
| SSH 被锁在外面 | fail2ban 误封或 ufw 规则错，走 VNC |
| `fail2ban-client` 命令挂起 | 非交互 SSH 会话，改用交互式登录 |
| jail 有但永远不封禁 | `backend` 选错（有 auth.log 却用 systemd） |

---

## 运维清单

### 部署后立即做

- [ ] 保存面板凭据和 **API Token**
- [ ] 备份 `/etc/x-ui/x-ui.db`
- [ ] 验证 Reality 伪装（`openssl s_client` 返回真实证书）
- [ ] 验证节点端到端可用（临时 Xray 客户端 + socks5 访问 ipify）
- [ ] 验证两种订阅格式都能拉
- [ ] 云安全组只放行必要端口
- [ ] 确认 `timedatectl` 显示 synchronized yes
- [ ] 确认 BBR 已启用
- [ ] 日志级别设为 warning，关闭访问日志

### 定期检查

| 周期 | 检查项 |
|---|---|
| 每周 | `df -h`（日志占满磁盘是最常见故障） |
| 每周 | `free -h`（内存 + swap 使用） |
| 每月 | 证书剩余时间 `openssl x509 -enddate` |
| 每月 | `fail2ban-client status sshd` 看封禁情况 |
| 每季 | 备份 DB 到本地 |
| 按需 | 清理 `/var/log/x-ui/3xui.log` |

### 磁盘占满的预防

1G 小机器最常见故障是日志占满磁盘。预防：

```bash
# 面板日志级别设 warning，关闭访问日志（面板设置里）
# 系统日志限制
journalctl --vacuum-size=200M

# 检查大文件
du -sh /var/log/* | sort -rh | head -10
du -sh /var/lib/docker/* 2>/dev/null | sort -rh | head -5
```

### 迁移到新机器

```bash
# 旧机器
systemctl stop x-ui
tar czf /root/migrate.tar.gz /etc/x-ui/x-ui.db /root/cert

# 新机器：装好 3x-ui 后
scp /root/migrate.tar.gz root@新IP:/root/
systemctl stop x-ui
tar xzf /root/migrate.tar.gz -C /
chown -R root:root /etc/x-ui /root/cert
systemctl start x-ui
```

DB 里有入站配置、客户端 UUID、订阅 Sub ID，迁移后客户端**不需要改订阅地址**。

---

## 安全注意事项

### 必须做

1. **面板不要裸奔在公网**
   最安全是 `webListen=127.0.0.1` + SSH 隧道；次之是 nginx 反代 + 登录限流。
   x-ui 3.11 无内置限流、不记录登录失败，公网裸奔等于没有防护。

2. **不要在境内售卖或大规模分享**
   自用没问题，对外提供服务在境内是明确违法的。

3. **订阅链接当密码对待**
   拿到订阅链接就等于拿到节点。不要往群、图床、公开仓库贴。

4. **`.env` 里的 AccessKey 绝不入库**
   `.gitignore` 已挡掉，但粘贴到对话 / Issue 里同样等于泄露。

5. **SSH 用密钥，禁用密码**
   先确认密钥能登录再关密码，否则会被锁在外面。

### 易犯错

1. **`fail2ban-client set banip` 绕过白名单**
   测试白名单别用自己的 IP。

2. **改 ufw 前不确认 SSH 端口**
   `ufw enable` 后才发现 22 不是真实的 SSH 端口 = 被锁在外面。

3. **`bantime` 设太长**
   1 小时太长，10 分钟足够。误封时等 10 分钟 vs 1 小时，体验差别很大。

4. **3x-ui 的 `3x-ipl` jail 不禁用**
   它会封掉 443 和 8080，比它防的东西更危险。

5. **Reality 用 `www.microsoft.com` 作 dest**
   Xray 会警告该目标增加被封概率。

### 敏感文件清单

| 文件 | 含什么 | 泄露后果 |
|---|---|---|
| `/etc/x-ui/x-ui.db` | 面板账号、客户端 UUID、Sub ID | 节点被白嫖 |
| `~/.acme.sh/account.conf` | DNS API 凭据 | 域名被劫持 |
| `.env` | AccessKey | 云账号被控 |
| `nginx/cert/privkey.pem` | 私钥 | 可伪造 HTTPS |
| 订阅 URL | Sub ID | 节点被白嫖 |

---

## 附录

### A. 一键系统检查

```bash
#!/bin/bash
echo "=== 系统 ==="
. /etc/os-release; echo "$PRETTY_NAME / $(uname -r) / $(uname -m)"
systemd-detect-virt

echo "=== 资源 ==="
free -h | head -2
df -h / | tail -1
nproc

echo "=== 时间 ==="
timedatectl | grep -E "synchronized|Time zone"

echo "=== BBR ==="
sysctl -n net.ipv4.tcp_congestion_control

echo "=== 服务 ==="
for s in x-ui fail2ban docker; do
  printf "%-12s %s\n" "$s" "$(systemctl is-active $s 2>/dev/null || echo '-')"
done

echo "=== 端口 ==="
ss -tlnp | grep -E ":(22|80|443|8080|11297) " || echo "none"

echo "=== 防火墙 ==="
ufw status 2>/dev/null | head -3
iptables -S INPUT 2>/dev/null | head -5

echo "=== 证书 ==="
for f in /root/cert/*/fullchain.pem /etc/letsencrypt/live/*/fullchain.pem; do
  [ -f "$f" ] && echo "$f: $(openssl x509 -in $f -noout -enddate 2>/dev/null)"
done
```

### B. 端口可达性探测

```bash
#!/bin/bash
# 区分 DROP（防火墙丢包）和 RST（无监听）
IP="$1"; shift
for p in "$@"; do
  rc=$(timeout 5 bash -c "exec 3<>/dev/tcp/$IP/$p" 2>/dev/null; echo $?)
  case $rc in
    0)   echo "$p OPEN" ;;
    124) echo "$p DROPPED (防火墙拦截)" ;;
    *)   echo "$p RST (可达但无监听)" ;;
  esac
done
```

用法：`./probe.sh 1.2.3.4 22 80 443 8080`

### C. 参考链接

- 3x-ui: https://github.com/mhsanaei/3x-ui
- Xray-core: https://github.com/XTLS/Xray-core
- acme.sh: https://github.com/acmesh-official/acme.sh
- Clash Verge Rev: https://github.com/clash-verge-rev/clash-verge-rev
- REALITY 协议说明: https://github.com/XTLS/REALITY
