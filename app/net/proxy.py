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
import threading
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
        self._lock = threading.Lock()   # 并发 worker 共享池，last_used/use_count 必须互斥
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
        with self._lock:
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


@contextlib.contextmanager
def _locked_file(lock_path: Path, timeout: float = 15.0, stale: float = 60.0):
    """O_EXCL 锁文件：线程/进程都互斥；持锁方崩掉后 stale 秒自动回收。"""
    deadline = time.time() + timeout
    fd = None
    while time.time() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.05)
    if fd is None:
        raise RuntimeError(f"文件加锁超时：{lock_path}")
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            lock_path.unlink(missing_ok=True)


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
        with _locked_file(self.lock_path, self.lock_timeout, self.stale_lock):
            yield

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

    def reserve_if_free(self, proxy: Proxy, env: str = "",
                        note: str = "reserved") -> bool:
        """原子地“查未用 + 占位”：已被用过返回 False。

        并发下 is_used → reserve 分两步会让两个 worker 都判“未用”
        而抢到同一条，必须在同一把锁里完成。
        """
        with self._locked():
            data = self._read()
            if proxy.raw in data["entries"]:
                return False
            data["entries"][proxy.raw] = {
                "used_at": time.time(),
                "label": proxy.label,
                "env": env,
                "ip": "",
                "note": note,
            }
            self._write(data)
            return True

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


def _proxy_country(proxy: Proxy, timeout: int = 10) -> str:
    """走代理查出口国家码（2 字母，如 SG/US）。失败返回空串。"""
    import requests

    try:
        r = requests.get(
            "http://ip-api.com/json/?fields=countryCode",
            proxies={"http": proxy.raw, "https": proxy.raw},
            timeout=timeout,
        )
        cc = (r.json().get("countryCode") or "").upper()
        return cc
    except Exception:
        return ""


def acquire_for_browser(cfg, region: str = "sg",
                        ip_file: str = "data/ip.txt",
                        max_tries: int = 5) -> Proxy | None:
    """指纹浏览器代理获取（新逻辑，2026-11-17 按用户设计修订）。

    优先级：
    1. 读 ``ip_file``（每行 ``ip:port:user:password``，无 scheme）：
       取第一条可解析行即**消耗掉**——原子地从文件删行（一个窗口用掉就不再用），
       并追加记录到同目录 ``ip_used.txt`` 备查；行不做国家校验，质量由清单维护者保证。
    2. 文件不存在 / 空白 / 无可解析行 → 用 ipdeep ``extract_url``/网关凭证提取，
       **只允许 us / sg** 两个地区（2026-11-17 实测 hk 段被阿里风控打低信誉分，
       滑块连续 6+ 次"無法驗證使用者是否為人類"，弃用），出口国家必须与
       region 一致，不符就丢弃重取。

    实测 extract_url 后台配置不保证国家（曾出过 Belarus IP 导致 aliyun 滑块图
    全部拒绝下载），所以校验不可省。最多重取 ``max_tries`` 次。
    """
    region = (region or "").lower()
    if region not in ("us", "sg"):
        return None
    want_cc = region.upper()

    # 1) ip.txt：消耗式取用（取一行删一行，用完即止）
    #    读→挑→写回必须整段互斥（线程/进程），否则两个 worker 会读到
    #    同一份清单抢走同一条，且后写者把先写者的删行覆盖掉。
    p = Path(ip_file)
    if p.exists():
        try:
            with _locked_file(p.with_suffix(p.suffix + ".lock")):
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                pick = -1
                px = None
                for i, line in enumerate(lines):
                    s = line.strip()
                    if not s or s.startswith(("#", ";", "//")):
                        continue
                    cand = parse_line(s)
                    if cand:
                        pick, px = i, cand
                        break
                if px is not None:
                    rest = [ln for j, ln in enumerate(lines) if j != pick]
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    tmp.write_text("\n".join(rest) + ("\n" if rest else ""),
                                   encoding="utf-8")
                    tmp.replace(p)          # 原子替换，避免读到半截文件
                    used = p.parent / "ip_used.txt"
                    try:
                        with open(used, "a", encoding="utf-8") as fh:
                            fh.write(px.raw + "\n")
                    except Exception:
                        pass                # 审计记录失败不影响主流程
                    return px
        except Exception:
            pass

    # 2) ipdeep 兜底：底座凭证固定（gate.ipdeep.com:8080 + 账号段:密码），
    #    出口国家由用户名里的 ``-country-{cc}-`` 段控制，代码构造 + 校验，
    #    cc 只允许 us/sg（hk 段 IP 信誉差已弃用）。
    #    实测 extract_url 后台不锁国家（出过 Belarus/MY/SY）且有提取限流，
    #    所以凭证优先读 secrets.yaml 的 ipdeep.gate_*，缺失才从 extract_url 探测。
    base_host = base_port = base_user = base_pass = ""
    try:
        from app import secrets as _sec
        base_host = _sec.get("ipdeep.gate_host") or ""
        base_port = _sec.get("ipdeep.gate_port") or 8080
        base_user = _sec.get("ipdeep.gate_user") or ""
        base_pass = _sec.get("ipdeep.gate_pass") or ""
    except Exception:
        pass

    if not (base_host and base_user):
        extract_url = ""
        try:
            from app import secrets as _sec
            extract_url = _sec.get("ipdeep.extract_url") or ""
        except Exception:
            extract_url = ""
        if extract_url:
            import urllib.request

            try:
                r = urllib.request.urlopen(extract_url, timeout=15)
                text = r.read().decode("utf-8", "replace").strip()
            except Exception:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", ";", "//")):
                    continue
                px = parse_line(line)
                if px:
                    base_host, base_port = px.host, px.port
                    base_user, base_pass = px.username or "", px.password or ""
                    break

    if not (base_host and base_user):
        return None

    # 构造 country-{region} 网关用户名：把 session 段替换为带国家段的版本
    import random
    import re as _re

    for _ in range(max_tries):
        sess = random.randint(1_000_000, 99_999_999)
        user = base_user
        m = _re.search(r"-session-(\d+)", user)
        if m:
            user = user[:m.start()] + f"-country-{region}-session-{sess}" + \
                user[m.end():]
        else:
            user = f"{user}-country-{region}-session-{sess}"
        raw = f"http://{user}:{base_pass}@{base_host}:{base_port}"
        px = parse_line(raw)
        if not px:
            continue
        cc = _proxy_country(px)
        if cc == want_cc:
            return px
    return None


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
        if ledger.is_used(p.raw):      # 廉价预筛，避免对已用代理做真连检查
            continue
        if check and not pool.check(p, timeout=timeout):
            ledger.mark(p, note="dead")
            continue
        # 原子“查未用 + 占位”：并发下分两步会抢到同一条
        if not ledger.reserve_if_free(p, note="reserved"):
            continue
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