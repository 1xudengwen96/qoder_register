# -*- coding: utf-8 -*-
"""便携包自检：逐个 import 重依赖，找出在目标机上崩溃的那个。

原生 C 扩展（onnxruntime/cv2/numpy）硬崩时不打 traceback、进程直接死，
所以每行先打「正在加载…」再 import：进程死在哪一行，元凶就是哪个包。
"""
import sys

MODULES = [
    "yaml", "requests", "sqlalchemy", "numpy", "PIL",
    "cv2", "onnxruntime", "ddddocr",
    "app.config", "app.cli", "app.webui",
]

for name in MODULES:
    print(f"加载 {name} …", flush=True)
    try:
        __import__(name)
        print(f"  OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  失败：{type(exc).__name__}: {exc}", flush=True)

print("自检结束。若中途进程直接消失（没有「自检结束」），"
      "消失前最后一行就是要找的崩溃依赖。")
