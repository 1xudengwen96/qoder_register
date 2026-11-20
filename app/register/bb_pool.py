"""比特浏览器窗口池：固定 N 个带标识的窗口循环复用。

旧逻辑（备份在 data/logic_backup_20261118）：每注册一个号新建一个
``acct{id}`` 环境，环境无限堆积（实测堆到 33 个），列表失控、启动配额吃紧。

新逻辑（2026-11-18 按用户设计）：
1. **探测 + 标识**：扫环境列表，``qpool_`` 前缀 + remark 标记的是池窗口，
   不足 N 个就补齐，多了不删（status 里可见）；
2. **每用一次重配一套**：关窗 → 重写指纹（按 ``acct{id}`` seed 确定性派生，
   与领取段设备画像匹配逻辑一致）→ 写入新代理 → 启动 → CDP 清残留；
3. **清残留是复用的前提**：Cookie/缓存/localStorage/indexedDB 不清，
   服务端能把多个账号关联到同一浏览器存储——比指纹重复更致命；
4. **注册成功后窗口留开并锁定**：领取段（cdp 登录/额度兜底）还要复用
   注册 Cookie 做免密授权，领取完成才释放；池被占满时溢出建临时窗口，
   用完即删，不污染池；
5. **进程结束统一关窗**：``close_all()`` 由流水线收尾调用；新进程来了
   随机挑空闲窗口重新开。

并发安全：账本文件 + O_EXCL 锁（复用 ``proxy._locked_file``），
线程/进程都互斥；持锁进程崩溃后按 pid 存活检测自动回收。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

from app.net.proxy import _locked_file
from app.register.bb_cdp import BBLaunch, _fingerprint_for, launch_env
from app.register.bitbrowser import (
    BitBrowserClient,
    BitBrowserEnv,
    build_fingerprint,
    build_proxy_config,
)

log = logging.getLogger("bb-pool")

POOL_REMARK = "qoder_register/pool"
TEMP_PREFIX = "qtmp_"
# 清残留的目标源：注册/确认链路实际涉及的站点（storageTypes=all 一次清干净）。
# qoder.sh 实测不存在（ERR_NAME_NOT_RESOLVED），不列入，避免每次取窗白导航。
WIPE_ORIGINS = (
    "https://qoder.com", "https://www.qoder.com",
)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def wipe_residue(endpoint: str, origins: tuple[str, ...] = WIPE_ORIGINS) -> dict:
    """全 profile 清残留：全量 Cookie + 缓存 + 各站点本地存储。

    实测（2026-11-18）：比特暴露的 browser 级 ws 只支持有限域，
    Network/Storage 一律报 "wasn't found"。改走 **page 级会话**：
    - ``Network.clearBrowserCookies/Cache`` 虽从 page 会话发出，
      但作用域是整个浏览器 profile（不是当前页）；
    - ``Storage.clearDataForOrigin`` 要求该 origin 被访问过，
      否则 Internal error——所以先导航注册 origin 再清。
    返回各动作成败，供日志与测试断言。
    """
    from app.register.cdp_page import new_page

    rep: dict = {"cookies": False, "cache": False, "origins": {}}
    page = new_page(endpoint, "about:blank", reuse_blank=True)
    cdp = page.cdp
    try:
        for key, method in (("cookies", "Network.clearBrowserCookies"),
                            ("cache", "Network.clearBrowserCache")):
            try:
                cdp.call(method)
                rep[key] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("清残留 %s 失败：%s", method, exc)
        for origin in origins:
            try:
                page.goto(origin + "/", wait_ms=500)
                cdp.call("Storage.clearDataForOrigin",
                         {"origin": origin, "storageTypes": "all"})
                rep["origins"][origin] = True
            except Exception as exc:  # noqa: BLE001
                rep["origins"][origin] = False
                log.warning("清残留 %s 失败：%s", origin, exc)
    finally:
        try:
            # 复用同一标签：清完导回空白页留给注册段，不另开/不关标签
            page.goto("about:blank", wait_ms=200)
        except Exception:  # noqa: BLE001
            pass
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
    return rep


class EnvPool:
    """固定窗口数的比特环境池（探测/补齐/重配/清残留/回收）。"""

    def __init__(self, client: BitBrowserClient, size: int = 4,
                 prefix: str = "qpool", group_id: str | None = None,
                 ledger_path: str | Path = "data/.bb_pool.json",
                 accounts_path: str | Path = "data/.bb_pool_accounts.json",
                 stale_after: int = 1800) -> None:
        self.client = client
        self.size = max(1, int(size))
        self.prefix = prefix.rstrip("_") + "_"
        self.group_id = group_id
        self.path = Path(ledger_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.accounts_path = Path(accounts_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stale_after = stale_after

    # ---------------------------------------------------------- 账本
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def _entry_free(self, entry: dict | None) -> bool:
        """无记录 / 记录者已死 / 同进程超时遗留 → 可抢。hold 只保活进程。"""
        if not entry:
            return True
        pid = int(entry.get("pid") or 0)
        if pid == os.getpid():
            return False
        if not _pid_alive(pid):
            return True
        return time.time() - float(entry.get("ts") or 0) > self.stale_after

    # ---------------------------------------------------------- 探测/补齐
    def pool_envs(self) -> list[BitBrowserEnv]:
        envs = [e for e in self.client.list_all_envs()
                if e.name.startswith(self.prefix)]
        return sorted(envs, key=lambda e: e.name)

    def ensure(self) -> list[BitBrowserEnv]:
        """探测池窗口，不足 size 就补齐（打 remark 标识）。

        新建窗口一律**直连 + 带指纹**（build_proxy_config(None) =
        proxyMethod=2 + noproxy）；任务取用时再按模式重配代理。
        """
        envs = self.pool_envs()
        have = {e.name for e in envs}
        for i in range(1, self.size + 1):
            name = f"{self.prefix}{i:02d}"
            if name in have:
                continue
            created = self.client.create_env(
                name=name, fingerprint=build_fingerprint(),
                remark=POOL_REMARK, group_id=self.group_id)
            log.info("窗口池补齐：%s -> %s", name, created.id)
            envs.append(created)
        return sorted(envs, key=lambda e: e.name)

    def prepare(self) -> dict:
        """开工前预创建：补齐到 size 个直连窗口（指纹已建好），随取随用。

        存量池窗口统一刷成直连（保留各自指纹）——预创建阶段不配 IP，
        直连任务直接用；IP 模式任务在 acquire 时再写入代理。
        返回 created（本次新建数）与池窗口清单。
        """
        before = {e.id for e in self.pool_envs()}
        envs = self.ensure()
        # 直连组合 + 清掉存量代理的 host/port，避免残留字段干扰
        direct = {**build_proxy_config(None), "host": "", "port": "",
                  "proxyUserName": "", "proxyPassword": ""}
        for e in envs:
            self.client.update_env(e.id, **direct)
        created = sum(1 for e in envs if e.id not in before)
        log.info("窗口池预创建完成：共 %d 个直连窗口（新建 %d）",
                 len(envs), created)
        return {"created": created, "total": len(envs),
                "names": [e.name for e in envs]}

    def status(self, items: list | None = None) -> dict:
        """给控制台/CLI 看的池状态：总数、池窗口、运行中、占用者。

        ``items`` 可传入已拉取的全量窗口列表（webui 短缓存复用，
        避免一次状态计算打两次 /browser/list 撞限流）。"""
        all_envs = items if items is not None else self.client.list_all_envs()
        entries = self._read()
        pool = []
        for e in sorted(all_envs, key=lambda x: x.name):
            if not e.name.startswith(self.prefix):
                continue
            ent = entries.get(e.id) or {}
            pool.append({
                "name": e.name, "id": e.id,
                "running": bool(e.status),
                "busy": not self._entry_free(ent) if ent else False,
                "hold": bool(ent.get("hold")),
                "tag": ent.get("tag", ""),
            })
        return {
            "size": self.size, "prefix": self.prefix,
            "env_total": len(all_envs),
            "pool_total": len(pool),
            "running": sum(1 for p in pool if p["running"]),
            "pool": pool,
        }

    # ---------------------------------------------------------- 取用
    def acquire(self, account_tag: str, region: str = "us",
                proxy=None, headless: bool = False) -> BBLaunch:
        """随机挑一个空闲池窗口：重配指纹+代理 → 启动 → 清残留。

        池全被占用（含领取锁定）时溢出建临时窗口，``is_temp`` 可查，
        调用方用完应 ``delete_temp``。
        """
        with _locked_file(self.lock_path):
            envs = self.ensure()
            entries = self._read()
            free = [e for e in envs
                    if self._entry_free(entries.get(e.id))]
            temp = False
            if free:
                env = random.choice(free)      # 新进程随机开启
            else:
                temp = True
                name = (f"{TEMP_PREFIX}{account_tag or 'x'}_"
                        f"{random.randrange(1 << 20):05x}")
                env = self.client.create_env(
                    name=name, fingerprint=build_fingerprint(),
                    remark=POOL_REMARK + "/overflow", group_id=self.group_id)
                log.warning("窗口池全占用，溢出临时窗口 %s", name)
            entries[env.id] = {
                "pid": os.getpid(), "ts": time.time(),
                "tag": account_tag, "name": env.name,
                "hold": False, "temp": temp,
            }
            self._write(entries)

        try:
            return self._reconfigure_and_launch(env, account_tag, region,
                                                proxy, headless)
        except Exception:
            # 重配/启动失败：释放占用，让同伴能用这个窗口
            with _locked_file(self.lock_path):
                entries = self._read()
                entries.pop(env.id, None)
                self._write(entries)
            raise

    def _reconfigure_and_launch(self, env: BitBrowserEnv, account_tag: str,
                                region: str, proxy, headless: bool) -> BBLaunch:
        # 指纹要生效必须重启窗口：开着先关（卡"正在打开中"时短重试）
        if env.status:
            for _ in range(3):
                if self.client.close_env(env.id):
                    break
                time.sleep(2.0)
        fp = _fingerprint_for(region, account_tag)
        self.client.update_env(
            env.id, browserFingerPrint=fp, **build_proxy_config(proxy),
            url="about:blank", syncTabs=False, workbench="")
        # 环境已关：修 profile 关会话恢复，下次启动不带回上轮旧标签
        try:
            from app.register.bb_cdp import disable_session_restore
            disable_session_restore(env.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("关会话恢复失败 %s：%s", env.name, exc)
        endpoint, res = launch_env(self.client, env.id, headless=headless)
        try:
            from app.register.bb_cdp import close_extra_tabs
            n = close_extra_tabs(endpoint)
            if n:
                log.info("窗口 %s 关掉多余标签 %d 个（单标签保障）", env.name, n)
        except Exception as exc:  # noqa: BLE001
            log.warning("清多余标签失败 %s：%s", env.name, exc)
        try:
            rep = wipe_residue(endpoint)
            log.info("窗口 %s 残留已清：cookies=%s cache=%s 站点 %d/%d",
                     env.name, rep["cookies"], rep["cache"],
                     sum(rep["origins"].values()), len(rep["origins"]))
        except Exception as exc:  # noqa: BLE001
            # 清不干净宁可失败，也不带着上一个账号的存储去注册
            raise RuntimeError(f"窗口池清残留失败（{env.name}）：{exc}") from exc
        if not (rep["cookies"] and rep["cache"]):
            raise RuntimeError(
                f"窗口池清残留不彻底（{env.name}）："
                f"cookies={rep['cookies']} cache={rep['cache']}")
        return BBLaunch(
            env_id=env.id, env_name=env.name, endpoint=endpoint,
            created=False, pid=res.pid, core_version=res.core_version,
            proxy_label=(proxy.label if proxy else "直连"),
            proxy_raw=(proxy.raw if proxy else ""),
        )

    # ---------------------------------------------------------- 占用管理
    def is_temp(self, env_id: str) -> bool:
        return bool((self._read().get(env_id) or {}).get("temp"))

    def mark_claim_hold(self, env_id: str) -> None:
        """注册成功：窗口留给领取段复用注册 Cookie，直到领取完成。"""
        with _locked_file(self.lock_path):
            entries = self._read()
            if env_id in entries:
                entries[env_id]["hold"] = True
                entries[env_id]["ts"] = time.time()
                self._write(entries)

    def release(self, env_id: str) -> None:
        """释放占用；领取锁定中的窗口保留（等 clear_hold）。"""
        with _locked_file(self.lock_path):
            entries = self._read()
            ent = entries.get(env_id)
            if ent and not ent.get("hold"):
                entries.pop(env_id, None)
                self._write(entries)

    def clear_hold(self, env_id: str) -> None:
        """领取完成（无论成败）：彻底释放窗口。"""
        with _locked_file(self.lock_path):
            entries = self._read()
            if entries.pop(env_id, None) is not None:
                self._write(entries)

    def clear_hold_by_name(self, name: str) -> None:
        with _locked_file(self.lock_path):
            entries = self._read()
            hit = [k for k, v in entries.items() if v.get("name") == name]
            for k in hit:
                entries.pop(k, None)
            if hit:
                self._write(entries)

    # ---------------------------------------------------------- 账号映射
    # 领取段靠「环境名」找回注册窗口复用 Cookie。池窗口名是 qpool_XX，
    # 不再是 acct{id}，所以把 账号tag→窗口名 落成独立映射文件：
    # 进程收尾 close_all 只清占用账本，映射保留——比特 profile 存盘，
    # 关窗不丢 Cookie，下个进程按名字重开同一窗口照样免密授权。
    def _read_accounts(self) -> dict:
        try:
            data = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_accounts(self, data: dict) -> None:
        tmp = self.accounts_path.with_suffix(self.accounts_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.accounts_path)

    def remember_account(self, account_tag: str, env_id: str, env_name: str) -> None:
        """注册成功：记下这个账号用的是哪个池窗口。"""
        with _locked_file(self.lock_path):
            amap = self._read_accounts()
            amap[account_tag] = {"id": env_id, "name": env_name}
            self._write_accounts(amap)

    def env_name_for(self, account_tag: str) -> str:
        """领取段查窗口名；没记录返回空串（调用方退回 acct{id} 旧逻辑）。"""
        ent = self._read_accounts().get(account_tag) or {}
        return str(ent.get("name") or "")

    def finish_claim(self, account_tag: str) -> None:
        """领取完成（无论成败）：关窗、释放占用，窗口回池。

        账号→窗口映射保留：重领/补领还要靠它找回注册窗口
        （实测删映射后重领退回 acct{id} 新建→撞环境上限）。
        """
        with _locked_file(self.lock_path):
            entries = self._read()
            hit = [(k, v) for k, v in entries.items()
                   if v.get("tag") == account_tag]
            for k, _ in hit:
                entries.pop(k, None)
            if hit:
                self._write(entries)
        for env_id, ent in hit:
            self.client.close_env(env_id)
            if ent.get("temp"):
                self.client.delete_env(env_id)
        if not hit:
            # 占用记录已被收尾清掉（如进程重启后补领）：按映射关窗
            ment = self._read_accounts().get(account_tag) or {}
            if ment.get("id"):
                self.client.close_env(str(ment["id"]))

    def delete_temp(self, env_id: str) -> None:
        """溢出临时窗口用完即删，不污染池。"""
        self.client.close_env(env_id)
        self.client.delete_env(env_id)
        with _locked_file(self.lock_path):
            entries = self._read()
            if entries.pop(env_id, None) is not None:
                self._write(entries)

    # ---------------------------------------------------------- 收尾
    def close_all(self) -> int:
        """进程结束：关掉全部池窗口（含溢出临时窗），清空账本。"""
        closed = 0
        for e in self.client.list_all_envs():
            if e.name.startswith(self.prefix) or e.name.startswith(TEMP_PREFIX):
                if self.client.close_env(e.id):
                    closed += 1
        with _locked_file(self.lock_path):
            self._write({})
        log.info("进程收尾：已关闭 %d 个池窗口", closed)
        return closed


def build_env_pool(cfg, client: BitBrowserClient) -> EnvPool | None:
    """按配置建池；``browser.env_pool.enabled=false`` 时返回 None（走旧逻辑）。"""
    from app.config import get

    if not bool(get(cfg, "browser.env_pool.enabled", True)):
        return None
    return EnvPool(
        client,
        size=int(get(cfg, "browser.env_pool.size", 4)),
        prefix=str(get(cfg, "browser.env_pool.prefix", "qpool")),
        group_id=(get(cfg, "browser.bitbrowser.group_id", "") or None),
        ledger_path=str(get(cfg, "browser.env_pool.ledger", "data/.bb_pool.json")),
        accounts_path=str(get(cfg, "browser.env_pool.accounts",
                              "data/.bb_pool_accounts.json")),
    )