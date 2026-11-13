"""冒烟测试：验证完整流水线能组装起来（不发起网络请求）。"""

import app.orchestrator as o
from app.config import load_config


def main() -> int:
    cfg = load_config()
    p = o.Pipeline(cfg)
    print("pipeline built ok")
    print("engine backend   :", p.engine.backend)
    print("solver providers :", list(p.solver.providers))
    print("mail providers   :", list(p.mail_layer.providers))
    print("reset app_dir    :", p.resetter.app_dir)
    print("reset app exists :", p.resetter.app_dir.exists())
    print("proxy pool       :", p.proxies.summary())
    print("claimer client   :", p.claimer.client_path)
    print("\nmail health:")
    for h in p.mail_layer.health():
        print(f"  {h.name:<14} {h.remaining}/{h.total}  {h.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
