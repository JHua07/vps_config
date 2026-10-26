# vps_config

自建代理节点的部署配置与运维手册。

3x-ui（VLESS-Reality）+ nginx 反代 + fail2ban。

## 文档

**[DEPLOYMENT.md](DEPLOYMENT.md)** — 完整部署手册，包含：

- 端口规划与三条硬约束
- **踩坑记录**（10 个实际遇到的问题及解法）
- 原生部署（已实测）
- 容器化部署
- 节点与订阅配置
- 证书续期（standalone vs DNS-01）
- fail2ban 配置与后端选择
- 云厂商安全组排查
- 故障排查决策树
- 运维清单与安全注意事项

## 快速开始

### 原生安装（已验证）

```bash
apt update && apt -y full-upgrade
apt -y install curl ca-certificates

# BBR
printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n' \
  > /etc/sysctl.d/99-bbr.conf && sysctl --system

# 3x-ui
bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
```

详细步骤见 [DEPLOYMENT.md](DEPLOYMENT.md#原生部署已实测)。

### 容器化

```bash
cp .env.example .env && vim .env
chmod +x deploy.sh && ./deploy.sh
```

> 容器化配置**尚未在真实机器上验证**，语法已校验但未运行过。
> 首次部署请重点检查：镜像拉取、`webListen` 参数支持、证书权限。

## 文件

| 文件 | 说明 |
|---|---|
| `DEPLOYMENT.md` | 完整文档 |
| `.env.example` | 配置模板（复制为 `.env` 后填写） |
|  容器化部署脚本| 容器化部署脚本 |
| `docker-compose.x-ui.yml` | 面板 + Xray + 订阅 |
| `docker-compose.nginx.yml` | 反代 + 登录限流 |
| `nginx/conf.d/panel.conf` | nginx 配置 |
| `fail2ban/99-custom.conf` | fail2ban 配置（宿主机原生） |

## 三个最容易踩的坑

1. **面板和订阅只接受 HTTPS**，用 `http://` 会被直接断连（`curl` 返回 `000`，不是 404）
2. **改 ufw 前先确认 SSH 真实端口**，`ss -tlnp | grep sshd`
3. **`fail2ban-client set banip` 绕过白名单**，测试别用自己的 IP

## 安全

`.env`、`x-ui/db/`、证书私钥已在 `.gitignore` 中排除。这些文件含
AccessKey、面板账号、客户端 UUID、订阅 Sub ID，泄露后果见
[DEPLOYMENT.md 安全注意事项](DEPLOYMENT.md#安全注意事项)。

本仓库仅供自用部署参考。在境内对外提供此类服务违法。
