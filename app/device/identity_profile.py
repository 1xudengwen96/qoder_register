"""设备身份画像：把指纹浏览器的方法论搬到 Qoder 客户端侧。

指纹浏览器（BitBrowser / AdsPower）指纹的三条铁律：
1. **一个种子派生全套字段** —— 同一 profile 重访指纹完全稳定，绝不漂移；
2. **字段间强一致** —— Win 机器不配 Apple GPU、CPU 线程数等于
   ``navigator.hardwareConcurrency``、内存与 ``deviceMemory`` 同源；
3. **跨 profile 互不相同** —— 不同种子产出不同且自洽的设备。

本模块把这三条套到「设备指纹替换」（qoder_reset）上：

- ``make_device_profile(seed=None, ...)``：独立画像。带 seed → 同一 seed
  永远同一台"物理机"（跨重置稳定）；不带 seed → 一套全新的自洽设备
  （字段间有一致性约束，但每次都是新机器）。
- ``profile_from_browser(fp)``：**浏览器匹配模式**。注册段用比特浏览器跑，
  画像是 ``make_profile(region, seed=account_tag)``（确定性）。领取段用
  同一个种子重建同一 FingerprintProfile，再派生设备身份——平台/时区/语言/
  CPU 线程数/内存/GPU/分辨率与注册环境完全一致，服务端看到的是
  「同一台机器先注册、后登录领取」。

派生全部用 ``sha256(seed|ns)`` 而不是 ``random``：确定性、不依赖调用顺序。
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
import uuid

# Windows 版本候选（与 fingerprint_engine 的 UA 模板 Windows NT 10.0 对应）
_WIN_BUILDS = ("10.0.19045", "10.0.22621", "10.0.22631", "10.0.26100")

# GPU 厂商 → ANGLE renderer 串（与 fingerprint_engine.make_profile 的表一致）
_VENDOR_RENDERERS = {
    "Google Inc. (NVIDIA)": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Intel)": "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Google Inc. (Apple)": "ANGLE (Apple, Apple M2 Pro, OpenGL 4.1)",
    "Google Inc. (AMD)": "ANGLE (AMD, AMD Radeon RX 7600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
}

_VENDOR_POOL_WIN = ("Google Inc. (NVIDIA)", "Google Inc. (Intel)", "Google Inc. (AMD)")


# ------------------------------------------------------------ 确定性派生
def _det_uuid(seed: str, ns: str) -> str:
    h = hashlib.sha256(f"{seed}|{ns}".encode("utf-8")).hexdigest()[:32]
    return str(uuid.UUID(h, version=4))


def _det_sha(seed: str, ns: str) -> str:
    return hashlib.sha256(f"{seed}|{ns}".encode("utf-8")).hexdigest()


def _det_hex16(seed: str, ns: str) -> str:
    return _det_sha(seed, ns)[:16]


# ------------------------------------------------------------ 字段构造
def _gpu_name_from_renderer(vendor: str, renderer: str, rng) -> str:
    """从 ANGLE renderer 串里抠出干净 GPU 名（如 NVIDIA GeForce RTX 4060）。"""
    inner = renderer or ""
    if inner.startswith("ANGLE ("):
        inner = inner[7:-1] if inner.endswith(")") else inner[7:]
    parts = [p.strip() for p in inner.split(",")]
    cand = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    for cut in (" Direct3D11", " vs_", " OpenGL"):
        idx = cand.find(cut)
        if idx > 0:
            cand = cand[:idx]
    cand = cand.strip()
    if cand:
        return cand
    return {"Apple": "Apple M2 Pro", "Intel": "Intel(R) UHD Graphics 770",
            "NVIDIA": "NVIDIA GeForce RTX 4060",
            "AMD": "AMD Radeon RX 7600"}.get(vendor.split("(")[-1].rstrip(")").strip()
                                             or vendor, "Intel(R) UHD Graphics 770")


def _cpu_name(vendor: str, rng) -> str:
    """CPU 与 GPU 厂商同源（Intel 核显配 Intel CPU，独显配主流 CPU）。"""
    v = (vendor or "").lower()
    if "apple" in v:
        return rng.choice(["Apple M1 Pro", "Apple M2", "Apple M2 Pro", "Apple M3"])
    if "intel" in v:
        return rng.choice(["Intel Core i5-14400", "Intel Core i5-13400",
                           "Intel Core i7-14700K"])
    return rng.choice(["Intel Core i5-14600K", "Intel Core i7-14700K",
                       "AMD Ryzen 5 7600", "AMD Ryzen 7 7700"])


def _system_fields(rng, plat_hint: str, timezone: str, locale: str) -> dict:
    if plat_hint == "mac":
        version = f"{rng.choice([12, 13, 14, 15])}.{rng.randint(0, 6)}.{rng.randint(0, 9)}"
        return {"system.platform": "darwin", "system.arch": "arm64",
                "system.version": version, "system.build": str(rng.randint(20000, 24000)),
                "system.locale": locale, "system.timezone": timezone}
    if plat_hint == "linux":
        version = f"{rng.choice([5, 6])}.{rng.randint(0, 19)}.{rng.randint(0, 50)}"
        return {"system.platform": "linux", "system.arch": "x64",
                "system.version": version, "system.build": str(rng.randint(20000, 26100)),
                "system.locale": locale, "system.timezone": timezone}
    version = f"{rng.choice(_WIN_BUILDS)}.{rng.randint(1, 999)}"
    build = version.rsplit(".", 1)[0].rsplit(".", 1)[1]   # 10.0.22631 → 22631
    return {"system.platform": "win32", "system.arch": "x64",
            "system.version": version, "system.build": build,
            "system.locale": locale, "system.timezone": timezone}


def _hardware_fields(rng, plat_hint: str, logical_cores: int, mem_gb: int,
                     vendor: str, gpu_name: str, w: int, h: int) -> dict:
    """CPU 线程数 = navigator.hardwareConcurrency；物理核 = 逻辑核的一半（Win）。"""
    if plat_hint == "mac":
        cpu = _cpu_name(vendor, rng)
        return {
            "cpu": {"name": cpu, "cores": logical_cores,
                    "frequency": f"{rng.uniform(2.0, 4.0):.1f}GHz"},
            "gpu": {"name": gpu_name, "memory": f"{rng.choice([16, 24, 32])}GB"},
            "memory": {"total": f"{mem_gb}GB", "type": "LPDDR5"},
            "display": {"resolution": f"{w}x{h}",
                        "refresh_rate": rng.choice([60, 120])},
        }
    physical = logical_cores // 2 if logical_cores >= 8 else logical_cores
    return {
        "cpu": {"name": _cpu_name(vendor, rng), "cores": physical,
                "threads": logical_cores,
                "frequency": f"{rng.uniform(3.0, 5.0):.1f}GHz"},
        "gpu": {"name": gpu_name, "memory": f"{rng.choice([8, 12, 16, 24])}GB",
                "driver_version": f"{rng.randint(530, 560)}.{rng.randint(60, 99)}"},
        "memory": {"total": f"{mem_gb}GB", "type": rng.choice(["DDR4", "DDR5"]),
                   "speed": f"{rng.choice([3200, 3600, 4800, 5600])}MHz"},
        "display": {"resolution": f"{w}x{h}",
                    "refresh_rate": rng.choice([60, 75, 120, 144, 165])},
    }


def _telemetry_fields(seed: str, machine_id: str) -> dict:
    from app.device.qoder_reset import TELEMETRY_KEYS

    out: dict[str, str] = {}
    for key, kind in TELEMETRY_KEYS.items():
        out[key] = (_det_sha(seed, f"telemetry.{key}") if kind == "sha256"
                    else _det_uuid(seed, f"telemetry.{key}"))
    # 与 step_machine_id 的同步逻辑保持一致：machineId 系列是 machineid 的 sha256
    mhash = hashlib.sha256(machine_id.encode("utf-8")).hexdigest()
    out["telemetry.machineId"] = mhash
    out["machineId"] = mhash
    return out


def _id_file_fields(seed: str, machine_id: str) -> dict[str, str]:
    from app.device.qoder_reset import ID_FILES

    ids = {name: _det_uuid(seed, f"idfile.{name}") for name in ID_FILES}
    ids["machineid"] = machine_id          # 与主 ID 同源，两步写同一个值不漂移
    return ids


def _browser_fp_fields(seed: str, user_agent: str, plat: str, w: int, h: int,
                        color_depth: int, timezone: str, locale: str,
                        vendor: str) -> dict:
    return {
        "user_agent": user_agent,
        "screen_resolution": f"{w}x{h}",
        "color_depth": color_depth,
        "timezone": timezone,
        "language": locale,
        "platform": plat,
        "webgl_vendor": vendor,
        # 确定性哈希（原方案是 md5(random) → 每次漂移，破绽）
        "canvas_fingerprint": _det_hex16(seed, "canvas"),
        "audio_fingerprint": _det_hex16(seed, "audio"),
    }


# ------------------------------------------------------------ 画像对象
@dataclasses.dataclass(slots=True)
class DeviceIdentityProfile:
    """一套自洽的设备身份。同 seed → 逐字段完全一致；跨 seed → 不同且自洽。"""

    seed: str
    machine_id: str
    id_file_ids: dict[str, str]
    telemetry: dict[str, str]
    system: dict[str, str]
    hardware: dict
    browser_fp: dict

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)

    def brief(self) -> str:
        return (f"seed={self.seed} machine={self.machine_id[:8]}… "
                f"{self.system.get('system.platform')} "
                f"{self.system.get('system.version')} "
                f"tz={self.system.get('system.timezone')} "
                f"gpu={self.hardware.get('gpu', {}).get('name', '')[:28]}")


# ------------------------------------------------------------ 构造入口
def _assemble(seed: str, rng, plat_hint: str, timezone: str, locale: str,
              logical_cores: int, mem_gb: int, vendor: str, renderer: str,
              w: int, h: int, user_agent: str, color_depth: int,
              plat_js: str) -> DeviceIdentityProfile:
    machine_id = _det_uuid(seed, "machineid")
    return DeviceIdentityProfile(
        seed=seed,
        machine_id=machine_id,
        id_file_ids=_id_file_fields(seed, machine_id),
        telemetry=_telemetry_fields(seed, machine_id),
        system=_system_fields(rng, plat_hint, timezone, locale),
        hardware=_hardware_fields(rng, plat_hint, logical_cores, mem_gb,
                                   vendor, _gpu_name_from_renderer(vendor, renderer, rng),
                                   w, h),
        browser_fp=_browser_fp_fields(seed, user_agent, plat_js, w, h,
                                      color_depth, timezone, locale, vendor),
    )


def profile_from_browser(fp, seed: str | None = None) -> DeviceIdentityProfile:
    """**浏览器匹配模式**：从注册段的浏览器画像派生一致的设备身份。

    ``fp`` 是 ``fingerprint_engine.FingerprintProfile``（注册时
    ``make_profile(region, seed=account_tag)`` 的产物）。领取段用同一个
    seed 重建后传入本函数，即可得到与注册环境完全一致的设备画像：
    平台/时区/语言/CPU 线程数/内存/GPU/分辨率一一对齐。
    """
    seed = seed or str(getattr(fp, "profile_id", "") or "anon")
    plat_js = str(getattr(fp, "platform", "Win32"))
    plat_hint = "mac" if plat_js == "MacIntel" else "win"
    rng = random.Random(f"{seed}|device")
    return _assemble(
        seed, rng, plat_hint,
        timezone=str(getattr(fp, "timezone", "America/New_York")),
        locale=str(getattr(fp, "locale", "en-US")),
        logical_cores=int(getattr(fp, "hardware_concurrency", 8)),
        mem_gb=int(getattr(fp, "device_memory", 16)),
        vendor=str(getattr(fp, "webgl_vendor", "Google Inc. (Intel)")),
        renderer=str(getattr(fp, "webgl_renderer", "")),
        w=int(getattr(fp, "viewport_w", 1920)),
        h=int(getattr(fp, "viewport_h", 1080)),
        user_agent=str(getattr(fp, "user_agent", "")),
        color_depth=int(getattr(fp, "color_depth", 24)),
        plat_js=plat_js,
    )


def make_device_profile(seed: str | None = None, *,
                         platform_hint: str = "win",
                         timezone: str = "America/New_York",
                         locale: str = "en-US",
                         screen: tuple[int, int] = (1920, 1080),
                         logical_cores: int | None = None,
                         mem_gb: int | None = None) -> DeviceIdentityProfile:
    """独立画像（不依赖浏览器画像时的指纹浏览器式设备）。

    seed=None → 全新自洽设备（每次不同）；seed 给定 → 同一"物理机"跨重置稳定。
    """
    tag = seed or f"dev_{uuid.uuid4().hex[:12]}"
    rng = random.Random(tag) if seed else random.Random()
    plat_hint = "mac" if platform_hint == "mac" else platform_hint
    if plat_hint == "mac":
        vendor = "Google Inc. (Apple)"
        cores = logical_cores or rng.choice([8, 10, 12])
        mem = mem_gb or rng.choice([16, 24, 32])
        w, h = screen
    else:
        vendor = rng.choice(_VENDOR_POOL_WIN)
        cores = logical_cores or rng.choice([4, 8, 12, 16])
        mem = mem_gb or rng.choice([8, 16, 32])
        w, h = screen
    renderer = _VENDOR_RENDERERS[vendor]
    plat_js = "MacIntel" if plat_hint == "mac" else "Win32"
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
          if plat_hint == "mac" else
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    return _assemble(tag, rng, plat_hint, timezone, locale, cores, mem,
                     vendor, renderer, w, h, ua, 24, plat_js)
