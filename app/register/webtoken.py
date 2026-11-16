"""注册成功后立刻抓「会话凭据」并落库。

为什么要「立刻」
----------------
环境配置了「启动前清 cookie」（这是躲风控的关键），所以**一旦关闭浏览器，
登录态就没了**。必须在注册成功的同一个实例里完成抓取。

抓什么
------
1. ``qoder_session_cookie``（httpOnly，登录态本身）
2. ``qoderuid``
3. 顺手在页面内调一次 ``/api/v1/users/me`` 等接口，确认这份凭据可用

落库
----
写进 ``account.token_json``（JSON）并把状态推到 ``VERIFIED``，同时在
``data/sessions.jsonl`` 留一份原始快照。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSIONS = ROOT / "data" / "sessions.jsonl"

PROBE_PATHS = (
    "/api/v1/me",
    "/api/v1/me/userplan",
    "/api/v2/me/usages/big_model_credits",
)


def parse_profile(snapshot: dict) -> dict:
    """从探测结果里抽出 id / 计划 / 额度。"""
    out: dict = {"user_id": "", "name": "", "plan": "", "credits": 0,
                 "credit_limit": 0}
    for p in snapshot.get("probe") or []:
        if p.get("status") != 200:
            continue
        try:
            obj = json.loads(p.get("body") or "{}")
        except Exception:
            continue
        path = p.get("path", "")
        if path == "/api/v1/me":
            out["user_id"] = obj.get("id", "")
            out["name"] = obj.get("name", "")
            out["email"] = obj.get("email", "")
        elif path == "/api/v1/me/userplan":
            out["plan"] = obj.get("plan_tier_name") or obj.get("plan_tier", "")
            out["plan_status"] = obj.get("status", "")
        elif "big_model_credits" in path:
            total = obj.get("total_quota") or {}
            summary = total.get("quota_summary") or {}
            out["credits"] = int(summary.get("remaining_value") or 0)
            out["credit_limit"] = int(summary.get("limit_value") or 0)
    return out

JS_FETCH = r"""(async (opts) => {
  const out = [];
  for (const p of opts.paths) {
    try {
      const r = await fetch(p, {credentials: 'include',
                                headers: {'Accept': 'application/json'}});
      const t = await r.text();
      out.push({path: p, status: r.status, body: t.slice(0, 400)});
    } catch (e) {
      out.push({path: p, status: 0, body: 'ERR ' + String(e).slice(0, 100)});
    }
  }
  return out;
})"""


def snapshot_session(cdp) -> dict:
    """取 cookie（含 httpOnly）+ 页面内接口探测。"""
    out: dict = {"cookies": {}, "probe": [], "captured_at": dt.datetime.now()
                 .strftime("%Y-%m-%d %H:%M:%S")}
    try:
        res = cdp.call("Network.getCookies",
                       {"urls": ["https://qoder.com", "https://www.qoder.com"]})
        out["cookies"] = {c["name"]: c.get("value", "")
                          for c in res.get("cookies") or []}
    except Exception as exc:
        out["cookies_error"] = f"{type(exc).__name__}: {exc}"

    expr = f"({JS_FETCH})({json.dumps({'paths': list(PROBE_PATHS)})})"
    try:
        out["probe"] = cdp.evaluate(expr, await_promise=True) or []
    except Exception as exc:
        out["probe_error"] = f"{type(exc).__name__}: {exc}"

    out["uid"] = out["cookies"].get("qoderuid", "")
    out["session_cookie"] = out["cookies"].get("qoder_session_cookie", "")
    ok = [p for p in out.get("probe") or [] if p.get("status") == 200]
    out["api_ok"] = bool(ok)
    out["api_sample"] = ok[0] if ok else {}
    return out


def save_session(email: str, snapshot: dict, db_url: str = "") -> dict:
    """落盘 + 写库。返回写入信息。"""
    SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"email": email, **snapshot}
    with SESSIONS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    payload = {
        "kind": "web_session",
        "session_cookie": snapshot.get("session_cookie", ""),
        "qoderuid": snapshot.get("uid", ""),
        "cookies": snapshot.get("cookies", {}),
        "api_ok": snapshot.get("api_ok"),
        "api_sample": snapshot.get("api_sample", {}),
        "profile": parse_profile(snapshot),
        "captured_at": snapshot.get("captured_at", ""),
    }

    db_id = None
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import sessionmaker

        from app.models import Account, AccountState, Base

        engine = create_engine(db_url or f"sqlite:///{ROOT / 'data' / 'qoder.db'}")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as s:
            row = s.execute(select(Account).where(
                Account.email == email)).scalar_one_or_none()
            if row is not None:
                row.token_json = json.dumps(payload, ensure_ascii=False)
                prof = payload.get("profile") or {}
                row.credits = int(prof.get("credits") or 0)
                if prof.get("plan"):
                    row.plan = str(prof["plan"])[:64]
                state = getattr(AccountState, "VERIFIED", None)
                if state is not None:
                    row.state = state
                s.commit()
                db_id = int(row.id)
    except Exception as exc:
        payload["db_error"] = f"{type(exc).__name__}: {exc}"

    return {"jsonl": str(SESSIONS), "db_id": db_id, "payload": payload}
