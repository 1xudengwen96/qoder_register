"""逐项调试 token harvest：Local State / DPAPI / v10 / AESGCM / CDP。"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.claim.token_harvester import (  # noqa: E402
    QoderTokenHarvester, qoder_data_dir,
)

h = QoderTokenHarvester(qoder_data_dir())
d = h.data_dir
print(f"data_dir={d}")
for name in ("Local State", "auth.v1.dat", "DevToolsActivePort",
             "auth.machine-id", "machineid"):
    p = d / name
    print(f"  {name:20s} exists={p.exists()} "
          f"size={p.stat().st_size if p.exists() else '-'}")

ls = d / "Local State"
if ls.exists():
    try:
        data = json.loads(ls.read_text(encoding="utf-8"))
        enc = base64.b64decode(data["os_crypt"]["encrypted_key"])
        print(f"encrypted_key prefix={enc[:5]} len={len(enc)}")
        if enc[:5] == b"DPAPI":
            import ctypes

            class _B(ctypes.Structure):
                _fields_ = [("cbData", ctypes.c_ulong),
                            ("pbData", ctypes.POINTER(ctypes.c_char))]

            inb = _B(len(enc[5:]),
                     ctypes.cast(ctypes.c_char_p(enc[5:]),
                                 ctypes.POINTER(ctypes.c_char)))
            outb = _B()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inb), None, None, None, None, 0,
                ctypes.byref(outb))
            print(f"DPAPI CryptUnprotectData ok={ok}")
            if ok:
                key = ctypes.string_at(outb.pbData, outb.cbData)
                print(f"AES key len={len(key)}")
                blob = (d / "auth.v1.dat").read_bytes()
                print(f"auth.v1.dat prefix={blob[:3]} total={len(blob)}")
                payload = blob[3:]
                try:
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                    plain = AESGCM(key).decrypt(payload[:12], payload, None)
                    print(f"AESGCM ok, plain[:80]={plain[:80]}")
                    obj = json.loads(plain)
                    print(f"token_prefix={str(obj.get('token'))[:24]} "
                          f"keys={list(obj)[:8]}")
                except ImportError:
                    print("无 cryptography 库")
                    plain = h._bcrypt_gcm(key, payload)
                    print(f"bcrypt_gcm plain[:80]={plain[:80] if plain else None}")
                except Exception as exc:
                    print(f"AESGCM fail: {exc}")
    except Exception as exc:
        print(f"Local State 解析失败: {exc}")

print("\nharvest_from_cdp:", h.harvest_from_cdp() and "OK" or "None")
print("harvest_from_safe_storage:",
      "OK" if h.harvest_from_safe_storage() else "None")
print("harvest_from_leveldb:",
      "OK" if h.harvest_from_leveldb() else "None")
