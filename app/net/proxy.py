"""代理池：从文件加载、按地区挑、健康检查、自动摘除。

支持的代理文件格式（一行一条，``#`` 后面是可选地区标签）：

    host:port:user:pass              # 代理商最常见的四段冒号格式
    host:port:user:pass#us           # 带地区标签
    user:pass@host:port
    http://user:pass@1.2.3.4:8080#us
    socks5://user:pass@5.6.7.8:1080#jp
    http://9.9.9.9:3128#uk
    1.2.3.4:8080                     # 无认证

地区推断优先级：``#tag`` > 账号串里的 ``-country-xx`` / ``_country-xx`` > 无。
粘性会话与 TTL 也从账号串里解析：``-session-abc123`` / ``-sessiontime-5`` / ``_time-30``。

为什么必须认四段格式：代理商（mooproxy / ipdeep 等）导出的清单就是
``host:port:user:pass``，而 ``urlparse("http://h:1:u:p")`` 会抛
``ValueError: Port could not be cast to integer`` —— 一行错，整批丢。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

# 账号串里的地区 / 会话 / 时长标记
_COUNTRY_RE = re.compile(
    r"[-_](?:country|region|res-country)[-_]?([A-Za-z]{2})(?![A-Za-z])")
_SESSION_RE = re.compile(r"[-_]session[-_]([A-Za-z0-9]+)", re.I)
_TTL_RE = re.compile(r"[-_](?:sessiontime|time|ttl)[-_](\d+)", re.I)
_CRED_HOST_RE = re.compile(r"^(?P<cred>[^@/]+)@(?P<host>[^@/]+)$")


@dataclasses.dataclass(slots=True)
class Proxy:
    raw: str
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    region: str | None = None
    session: str | None = None          # 粘性会话 id（代理商 session-xxx）
    sticky_minutes: int | None = None   # 会话保持时长（time-30 → 30 分钟）
    health: str = "ok"
    use_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0

    def requests_proxies(self) -> dict[str, str]:
        return {"http": self.raw, "https": self.raw}

    def playwright_proxy(self) -> dict[str, str]:
        out = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    def bitbrowser_proxy(self) -> dict:
        """转成比特浏览器 ``/browser/update`` 的代理字段（延迟导入避免环）。"""
        from app.register.bitbrowser import build_proxy_config

        return build_proxy_config(self)

    @property
    def exit_ip_hint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def label(self) -> str:
        bits = [self.host, str(self.port)]
        if self.region:
            bits.append(self.region)
        if self.session:
            bits.append(f"s={self.session[:8]}")
        return ":".join(bits[:2]) + (f" ({', '.join(bits[2:])})" if len(bits) > 2 else "")


def _norm_host(host: str) -> str:
    host = host.strip().strip("/")
    if host.startswith("[") and "]" in host:      # IPv6
        return host
    return host.split("/")[0]


def _build(scheme, host, port, user, pwd, region, session, sticky) -> Proxy | None:
    host = _norm_host(host)
    try:
        port_i = int(str(port).strip())
    except (TypeError, ValueError):
        return None
    if not host or not (0 < port_i < 65536):
        return None
    scheme = (scheme or "http").lower()
    if user:
        raw = (f"{scheme}://{quote(str(user), safe='')}:"
               f"{quote(str(pwd or ''), safe='')}@{host}:{port_i}")
    else:
        raw = f"{scheme}://{host}:{port_i}"
    return Proxy(raw=raw, scheme=scheme, host=host, port=port_i,
                 username=user or None, password=pwd or None,
                 region=region, session=session, sticky_minutes=sticky)


def parse_line(line: str) -> Proxy | None:
    """解析一行代理。任何格式不认识都返回 None（绝不抛异常）。"""
    text = (line or "").strip()
    if not text or text.startswith(("//", ";")):
        return None

    region = None
    if "#" in text:
        text, _, tag = text.partition("#")
        region = (tag.strip().lower() or None)
        text = text.strip()
    if not text:
        return None

    scheme = "http"
    if "://" in text:
        scheme, _, text = text.partition("://")
    if not text:
        return None

    user = pwd = ""
    # a) user:pass@host:port
    m = _CRED_HOST_RE.match(text)
    if m:
        cred, hostpart = m.group("cred"), m.group("host")
        if ":" in cred:
            user, _, pwd = cred.partition(":")
        else:
            user = cred
        host, _, port = hostpart.rpartition(":")
        if not host:                       # hostpart 里没有冒号
            return None
    else:
        parts = text.split(":")
        if len(parts) == 2:                # host:port
            host, port = parts
        elif len(parts) >= 4:              # host:port:user:pass（密码可能含冒号）
            host, port, user = parts[0], parts[1], parts[2]
            pwd = ":".join(parts[3:])
        elif len(parts) == 3:              # host:port:user
            host, port, user = parts
        else:
            return None

    session = sticky = None
    if user or pwd:
        # 参数可能挂在用户名上，也可能整串都在密码里（mooproxy 就是后者）
        blob = f"{user}:{pwd}"
        cm = _COUNTRY_RE.search(blob)
        if cm and not region:
            region = cm.group(1).lower()
        sm = _SESSION_RE.search(blob)
        if sm:
            session = sm.group(1)
        tm = _TTL_RE.search(blob)
        if tm:
            try:
                sticky = int(tm.group(1))
            except ValueError:
                sticky = None
    return _build(scheme, host, port, user, pwd, region, session, sticky)


class ProxyPool:
    CHECK_URL = "https://api.ipify.org?format=json"

    def __init__(self, proxies: list[Proxy], skipped: int = 0) -> None:
        self.proxies = proxies
        self.skipped = skipped
        self._by_region: dict[str, list[Proxy]] = {}
        for p in proxies:
            self._by_region.setdefault(p.region or "any", []).append(p)

    @classmethod
    def from_file(cls, path: str | Path) -> "ProxyPool":
        p = Path(path)
        if not p.exists():
            return cls([])
        out: list[Proxy] = []
        bad = 0
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("//", ";", "#")):
                continue
            try:
                px = parse_line(line)
            except Exception:      # 单行脏数据不许炸整批
                px = None
            if px:
                out.append(px)
            else:
                bad += 1
        return cls(out, skipped=bad)

    # ---- 分配 -------------------------------------------------------------
    def acquire(self, region: str | None = None, exclude: set[str] | None = None) -> Proxy | None:
        exclude = exclude or set()
        pool = [
            p
            for p in (self._by_region.get(region, []) if region else self.proxies)
            if p.health == "ok" and p.raw not in exclude
        ]
        if not pool:
            pool = [p for p in self.proxies if p.health == "ok"]
        if not pool:
            return None
        # 优先用最久没被用过的，降低同一 IP 复用
        proxy = min(pool, key=lambda p: p.last_used)
        proxy.last_used = time.time()
        proxy.use_count += 1
        return proxy

    # ---- 健康检查 ---------------------------------------------------------
    def check(self, proxy: Proxy, timeout: int = 10) -> bool:
        try:
            r = requests.get(self.CHECK_URL, proxies=proxy.requests_proxies(), timeout=timeout)
            ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            proxy.health = "ok"
        else:
            proxy.fail_count += 1
            if proxy.fail_count >= 3:
                proxy.health = "banned"
        return ok

    def mark_banned(self, proxy: Proxy) -> None:
        proxy.health = "banned"

    def summary(self) -> str:
        total = len(self.proxies)
        ok = sum(1 for p in self.proxies if p.health == "ok")
        regions = sorted({p.region for p in self.proxies if p.region})
        sticky = sum(1 for p in self.proxies if p.session)
        extra = f"，粘性会话 {sticky}" if sticky else ""
        bad = f"，格式跳过 {self.skipped}" if self.skipped else ""
        return (f"代理 {total} 个，可用 {ok}{extra}，"
                f"地区 {','.join(regions) or '未标注'}{bad}")


def random_identity_for_region(region: str | None) -> dict[str, str]:
    """让浏览器时区/语言与代理地区对齐，避免明显失配。"""
    table = {
        "us": {"timezone": "America/New_York", "locale": "en-US"},
        "uk": {"timezone": "Europe/London", "locale": "en-GB"},
        "jp": {"timezone": "Asia/Tokyo", "locale": "ja-JP"},
        "sg": {"timezone": "Asia/Singapore", "locale": "en-SG"},
        "de": {"timezone": "Europe/Berlin", "locale": "de-DE"},
        "hk": {"timezone": "Asia/Hong_Kong", "locale": "zh-HK"},
    }
    return table.get((region or "us").lower(), table["us"])

# ============================================================ 一条一用账本
LEDGER_DEFAULT = Path("data/.proxy_state.json")
_SESSION_ALPHABET = "0123456789abcdef"


class ProxyLedger:
    """记录每条代理被哪一轮用过，用完即作废（one-shot）。

    为什么必须落盘：进程重启后 ``ProxyPool`` 会重建，``last_used`` 归零，
    于是每次都挑“最久没用过的”第 0 条——ipdeep 的会话只有 5 分钟粘性，
    第二次拿到同一会话时出口 IP 已经失效，浏览器连页面都打不开。

    并发安全靠一个 O_EXCL 锁文件；持锁进程崩掉后 60 秒自动回收。
    """

    def __init__(self, path: str | Path = LEDGER_DEFAULT,
                 lock_timeout: float = 15.0, stale_lock: float = 60.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout = lock_timeout
        self.stale_lock = stale_lock
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 锁
    @contextlib.contextmanager
    def _locked(self):
        deadline = time.time() + self.lock_timeout
        fd = None
        while time.time() < deadline:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > self.stale_lock:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.05)
        if fd is None:
            raise RuntimeError(f"代理账本加锁超时：{self.lock_path}")
        try:
            yield
        finally:
            try:
                os.close(fd)
            finally:
                self.lock_path.unlink(missing_ok=True)

    # ---------------------------------------------------------- 读写
    def _read(self) -> dict:
        if not self.path.exists():
            return {"entries": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"entries": {}}
        if not isinstance(data, dict) or "entries" not in data:
            return {"entries": {}}
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------------------------------------------------- 查询
    def is_used(self, raw: str) -> bool:
        return raw in self._read()["entries"]

    def remaining(self, raws: list[str]) -> int:
        used = self._read()["entries"]
        return sum(1 for r in raws if r not in used)

    def stats(self) -> dict:
        entries = self._read()["entries"]
        by_note: dict[str, int] = {}
        for e in entries.values():
            note = str(e.get("note") or "unknown")
            by_note[note] = by_note.get(note, 0) + 1
        return {"total": len(entries), "by_note": by_note}

    # ---------------------------------------------------------- 写入
    def reserve(self, proxy: Proxy, env: str = "", note: str = "reserved") -> None:
        """开窗口前先占位，防止两个 worker 抢到同一条。"""
        with self._locked():
            data = self._read()
            data["entries"][proxy.raw] = {
                "used_at": time.time(),
                "label": proxy.label,
                "env": env,
                "ip": "",
                "note": note,
            }
            self._write(data)

    def mark(self, proxy: Proxy, env: str = "", ip: str = "",
             note: str = "ok") -> None:
        with self._locked():
            data = self._read()
            entry = data["entries"].get(proxy.raw) or {
                "used_at": time.time(), "label": proxy.label,
            }
            entry.update({"env": env or entry.get("env", ""),
                          "ip": ip or entry.get("ip", ""),
                          "note": note})
            data["entries"][proxy.raw] = entry
            self._write(data)

    def prune(self, keep_hours: float = 168.0) -> int:
        cutoff = time.time() - keep_hours * 3600
        with self._locked():
            data = self._read()
            before = len(data["entries"])
            data["entries"] = {
                k: v for k, v in data["entries"].items()
                if float(v.get("used_at") or 0) >= cutoff
            }
            self._write(data)
            return before - len(data["entries"])


# ============================================================ 一用一取
def _same_region(proxy: Proxy, region: str | None) -> bool:
    if not region:
        return True
    return (proxy.region or "").lower() == region.lower()


def acquire_once(pool: "ProxyPool", ledger: ProxyLedger,
                 region: str | None = None, check: bool = False,
                 timeout: int = 10) -> Proxy | None:
    """取一条**从没用过**的代理并立即占位。

    池子按地区优先，其次按从未使用。开了 ``check`` 就先真连一次，
    死代理直接记 ``dead`` 消耗掉，不浪费后面的浏览器窗口。
    """
    cands = [p for p in pool.proxies if p.health == "ok"]
    if region:
        same = [p for p in cands if _same_region(p, region)]
        cands = same or cands
    ordered = sorted(cands, key=lambda x: (x.last_used, x.port))
    for p in ordered:
        if ledger.is_used(p.raw):
            continue
        if check and not pool.check(p, timeout=timeout):
            ledger.mark(p, note="dead")
            continue
        ledger.reserve(p, note="reserved")
        p.last_used = time.time()
        p.use_count += 1
        return p
    return None


# ============================================================ 模板续期
def render_proxy(template: str, *, region: str = "us", user: str = "",
                 password: str = "", ttl: int = 5,
                 session: str | None = None) -> str:
    """按模板渲染一条代理，``{session}`` 不给就随机生成 8 位十六进制。"""
    sid = session or "".join(random.choices(_SESSION_ALPHABET, k=8))
    out = template
    for key, val in (("{region}", region), ("{user}", user),
                     ("{password}", password), ("{pass}", password),
                     ("{ttl}", str(ttl)), ("{session}", sid)):
        out = out.replace(key, val)
    return out


def extend_from_template(*, template: str, user: str, password: str,
                         ttl: int = 5, count: int = 20, region: str = "us",
                         verify: bool = False, timeout: int = 15,
                         concurrency: int = 6) -> list[str]:
    """生成 count 条互不相同的代理；``verify=True`` 时只留真连得通的。"""
    raws: list[str] = []
    seen: set[str] = set()
    guard = 0
    while len(raws) < count and guard < count * 6:
        guard += 1
        sid = "".join(random.choices(_SESSION_ALPHABET, k=8))
        if sid in seen:
            continue
        seen.add(sid)
        raws.append(render_proxy(template, region=region, user=user,
                                 password=password, ttl=ttl, session=sid))
    if not verify:
        return raws

    def _check(raw: str) -> tuple[str, bool, str]:
        px = parse_line(raw)
        if px is None:
            return raw, False, "解析失败"
        try:
            r = requests.get("http://api.ipify.org?format=json",
                             proxies=px.requests_proxies(), timeout=timeout)
            return raw, r.status_code == 200, r.text[:80]
        except Exception as exc:  # noqa: BLE001
            return raw, False, type(exc).__name__

    alive: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        for raw, ok, detail in ex.map(_check, raws):
            if ok:
                alive.append(raw)
    return alive


def append_to_file(path: str | Path, raws: list[str], dedupe: bool = True) -> int:
    """把新代理追加到文件（默认按行去重），返回实际新增条数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if p.exists() and dedupe:
        existing = {ln.strip() for ln in p.read_text(encoding="utf-8-sig",
                                                     errors="replace").splitlines()}
    added = [r for r in raws if r not in existing]
    if added:
        with p.open("a", encoding="utf-8") as fh:
            for r in added:
                fh.write(r + "\n")
    return len(added)