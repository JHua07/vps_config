#!/usr/bin/env python3
"""Clash 订阅生成服务。

把 3x-ui 的原始订阅（仅含节点列表）包装成完整 Clash 配置：
DNS / 分流规则 / 代理组 / 性能项，输出与商业机场一致。

监听 0.0.0.0:8080 对外，从 127.0.0.1:2096 拉取 3x-ui 订阅（仅内网可达）。

URL 约定（与原 3x-ui 订阅保持一致，老客户端无需改地址）：
  /<subClashPath>/<SubID>  -> 完整 Clash YAML
  /<subPath>/<SubID>       -> 3x-ui 原始 base64 直通
  /healthz                 -> 健康检查
"""

import http.server
import base64
import json
import logging
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import yaml

# ---------- 配置（环境变量可覆盖） ----------
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
INTERNAL_BASE = os.environ.get("INTERNAL_BASE", "")  # 留空则从 DB 读
CERT_FILE = os.environ.get("CERT_FILE", "/root/cert/ip/fullchain.pem")
KEY_FILE = os.environ.get("KEY_FILE", "/root/cert/ip/privkey.pem")
TEMPLATE_FILE = os.environ.get("TEMPLATE", "/opt/clash-sub/template.yaml")
XUI_DB = os.environ.get("XUI_DB", "/etc/x-ui/x-ui.db")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "120"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
# reality public-key 兜底（按端口匹配不到时用）
REALITY_PUBLIC_KEY = os.environ.get("REALITY_PUBLIC_KEY", "")
# xray 二进制：用于从私钥推导 reality 公钥（权威实现）
XRAY_BIN = os.environ.get(
    "XRAY_BIN", "/usr/local/x-ui/bin/xray-linux-amd64"
)

log = logging.getLogger("subgen")

# ---------- 从 x-ui DB 读取订阅路径与内部端口 ----------
def load_settings():
    """返回 (internal_base, base64_path, clash_path)。读不到时用保守默认值。"""
    base = INTERNAL_BASE
    b64, clash = "/sub/", "/clash/"
    try:
        c = sqlite3.connect(f"file:{XUI_DB}?mode=ro", uri=True)
        rows = dict(c.execute("select key,value from settings"))
        c.close()
        port = rows.get("subPort", "2096")
        listen = (rows.get("subListen") or "").strip() or "127.0.0.1"
        if not base:
            base = f"https://{listen}:{port}"
        b64 = rows.get("subPath") or b64
        clash = rows.get("subClashPath") or clash
    except Exception as e:  # DB 不可用时仍可启动，靠环境变量
        log.warning("read x-ui db failed: %s", e)
    if not base:
        base = "https://127.0.0.1:2096"
    return base, _norm(b64), _norm(clash)


def _norm(path):
    """统一为 /xxx/ 形式，保证 startswith 判断可靠"""
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    return path


INTERNAL_BASE, B64_PATH, CLASH_PATH = load_settings()
log.info("internal=%s base64=%s clash=%s", INTERNAL_BASE, B64_PATH, CLASH_PATH)

# ---------- 上游拉取（带缓存，按 SubID 区分） ----------
_cache = {}          # subid -> {"ts","body","headers"}
_lock = threading.Lock()
_last_err = {}       # subid -> str

INSECURE_CTX = ssl.create_default_context()
INSECURE_CTX.check_hostname = False
INSECURE_CTX.verify_mode = ssl.CERT_NONE


def fetch_upstream(path):
    """从 3x-ui 订阅服务拉取。返回 (status, body, headers)。"""
    req = urllib.request.Request(
        INTERNAL_BASE + path, headers={"User-Agent": "subgen/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10, context=INSECURE_CTX) as r:
        return r.status, r.read(), dict(r.headers)


def get_clash_config(subid: str):
    """按 SubID 生成完整配置。优先缓存，上游失败时回退旧数据。"""
    now = time.time()
    ent = _cache.get(subid)
    if ent and ent["body"] and now - ent["ts"] < CACHE_TTL and subid not in _last_err:
        return ent["body"], ent["headers"]
    try:
        # 拉 Clash 路径：返回的 YAML 自带 proxies 段，直接提取节点。
        # （base64 路径返回的是编码文本，不能直接解析）
        status, body, headers = fetch_upstream(CLASH_PATH + subid)
        if status != 200:
            raise RuntimeError(f"upstream {status}")
        doc = yaml.safe_load(body.decode("utf-8", "replace"))
        proxies = (doc or {}).get("proxies")
        if not proxies:
            raise ValueError("upstream has no proxies")
        clash = build_clash(proxies)
        with _lock:
            _cache[subid] = {"ts": now, "body": clash.encode("utf-8"), "headers": headers}
            _last_err.pop(subid, None)
        return _cache[subid]["body"], _cache[subid]["headers"]
    except Exception as e:
        log.error("build failed for %s: %s", subid, e)
        with _lock:
            _last_err[subid] = str(e)
            if ent and ent["body"]:
                # 回退旧配置，保证订阅不断供
                return ent["body"], ent["headers"]
        raise


def build_clash(proxies) -> str:
    """把节点列表注入模板，生成完整配置"""
    if not proxies:
        raise ValueError("no proxies")

    # 3x-ui 生成的 reality 节点缺 public-key（新版 mihomo 强校验，缺了直接报错）。
    # 用 xray 二进制从入站私钥推导——Xray 自己是权威实现，避免自己实现 X25519
    # 与其新版本（mlkem768 等）的推导差异。
    pubkeys = load_reality_pubkeys()
    for p in proxies:
        ro = p.get("reality-opts")
        if not isinstance(ro, dict):
            continue
        if not ro.get("public-key"):
            pk = pubkeys.get(int(p.get("port", 0))) or REALITY_PUBLIC_KEY
            if pk:
                ro["public-key"] = pk
                log.info("injected public-key into %s (port %s)", p.get("name"), p.get("port"))
        # support-x25519mlkem768 是 Xray 26 客户端的握手提示：客户端据此主动
        # 提供 mlkem768 公钥。mihomo 的 Reality 实现不认这个流程，留着可能
        # 协商出与 Xray 26 服务端不兼容的握手，直接剥掉强制经典 x25519
        # （服务端已实测接受经典握手：服务器本机穿透测试 204）。
        if ro.pop("support-x25519mlkem768", None) is not None:
            log.info("stripped support-x25519mlkem768 from %s", p.get("name"))
    for p in proxies:
        if p.get("reality-opts") and not p["reality-opts"].get("public-key"):
            raise ValueError(
                f"proxy {p.get('name')} has reality-opts without public-key "
                f"and no matching reality inbound/private key"
            )

    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        tmpl = f.read()

    # 节点段：逐个 dump 再统一缩进。safe_dump(单字典) 不含列表的 "- " 标记，
    # 需要手动补在首行，否则整段会被解析成映射而不是列表
    p_lines = []
    for p in proxies:
        chunk = yaml.safe_dump(
            p, allow_unicode=True, sort_keys=False, default_flow_style=False
        ).rstrip().splitlines()
        p_lines.append("  - " + chunk[0])
        p_lines.extend("    " + line for line in chunk[1:])

    names = [str(p["name"]) for p in proxies if p.get("name")]
    n_lines = ['      - "%s"' % n.replace('"', '\\"') for n in names]

    # 只替换「整行占位符」，注释里出现相同字样不会误伤
    import re

    out = re.sub(r"^\{\{PROXIES\}\}[ \t]*$", "\n".join(p_lines), tmpl, flags=re.M)
    out = re.sub(
        r"^\{\{PROXY_NAMES\}\}[ \t]*$", "\n".join(n_lines), out, flags=re.M
    )
    if "{{PROXIES}}" in out or "{{PROXY_NAMES}}" in out:
        raise ValueError("template placeholder not replaced (standalone line missing)")

    # 校验生成结果合法，坏配置不能发出去
    check = yaml.safe_load(out)
    if not isinstance(check, dict) or not check.get("proxies"):
        raise ValueError("generated config invalid")

    log.info(
        "built config: %d proxies, %d rules, %d bytes",
        len(proxies), len(check.get("rules") or []), len(out),
    )
    return out


# ---------- 证书热更新 ----------
def cert_mtime():
    try:
        return os.stat(CERT_FILE).st_mtime, os.stat(KEY_FILE).st_mtime
    except OSError:
        return None


def watch_cert():
    """acme.sh 续期只重启 x-ui，这里自己监控证书变化并原地重启进程"""
    last = cert_mtime()
    while True:
        time.sleep(60)
        cur = cert_mtime()
        if cur != last:
            log.info("certificate changed, restarting")
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


# ---------- reality 公钥推导 ----------
_pubkey_cache = {}


def xray_derive_pubkey(priv_b64url: str) -> str:
    """调用 xray 从私钥推导 reality 公钥。

    不自己实现 X25519：Xray 新版引入 mlkem768 等变体后，"Password (PublicKey)"
    的推导由 Xray 自己定义，二进制是唯一权威。结果缓存。
    """
    if priv_b64url in _pubkey_cache:
        return _pubkey_cache[priv_b64url]
    if not os.path.exists(XRAY_BIN):
        raise FileNotFoundError(f"xray binary not found: {XRAY_BIN}")
    r = subprocess.run(
        [XRAY_BIN, "x25519", "-i", priv_b64url],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"xray x25519 failed: {r.stderr.strip()[:200]}")
    pub = None
    for line in r.stdout.splitlines():
        # 兼容 "Password (PublicKey): xxx" 与旧版 "Public key: xxx"
        for marker in ("Password (PublicKey):", "Public key:"):
            if marker in line:
                pub = line.split(":", 1)[1].strip()
                break
    if not pub:
        raise ValueError(f"cannot parse xray output: {r.stdout[:200]}")
    _pubkey_cache[priv_b64url] = pub
    log.info("derived reality pubkey from xray: %s...", pub[:8])
    return pub


def load_reality_pubkeys():
    """端口 -> reality 公钥。从 x-ui DB 的入站 streamSettings 推导。"""
    out = {}
    try:
        c = sqlite3.connect(f"file:{XUI_DB}?mode=ro", uri=True)
        for port, stream in c.execute(
            "select port, stream_settings from inbounds where protocol='vless'"
        ):
            try:
                ss = json.loads(stream)
                rs = ss.get("realitySettings") or {}
                priv = rs.get("privateKey")
                if priv:
                    out[int(port)] = xray_derive_pubkey(priv)
            except Exception as e:
                log.warning("derive pubkey for port %s failed: %s", port, e)
        c.close()
    except Exception as e:
        log.warning("read inbounds for pubkeys failed: %s", e)
    return out


# ---------- HTTP ----------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "subgen/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            self._send(200, b"ok\n")
            return

        if path.startswith(CLASH_PATH) or path.startswith(B64_PATH):
            passthrough = path.startswith(B64_PATH)
            try:
                if passthrough:
                    status, body, headers = fetch_upstream(path)
                    if status != 200:
                        self._send(502, b"upstream error")
                        return
                    extra = {
                        "Subscription-Userinfo": headers.get(
                            "Subscription-Userinfo", ""
                        ),
                        "Profile-Update-Interval": headers.get(
                            "Profile-Update-Interval", "12"
                        ),
                    }
                    self._send(200, body, "text/plain; charset=utf-8", extra)
                else:
                    subid = path[len(CLASH_PATH):].strip("/")
                    body, headers = get_clash_config(subid)
                    extra = {
                        "Subscription-Userinfo": headers.get(
                            "Subscription-Userinfo", ""
                        ),
                        "Profile-Update-Interval": headers.get(
                            "Profile-Update-Interval", "12"
                        ),
                        "Profile-Title": "base64:" + __import__("base64").b64encode(
                            "自建节点".encode()
                        ).decode(),
                    }
                    self._send(
                        200, body, "application/yaml; charset=utf-8", extra
                    )
            except Exception as e:
                self._send(503, ("subscription unavailable: %s" % e).encode())
            return

        self._send(404, b"not found\n")

    do_HEAD = do_GET


class TLSServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    threading.Thread(target=watch_cert, daemon=True).start()

    httpd = TLSServer((LISTEN_HOST, LISTEN_PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    log.info("listening on %s:%d (tls)", LISTEN_HOST, LISTEN_PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
