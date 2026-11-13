"""验证 Outlook OAuth2 收信链路。

背景：微软已禁用 IMAP 基本认证（用户名+密码），必须走 OAuth2：
  refresh_token → access_token → IMAP XOAUTH2

邮箱文件格式（发货格式）：
    邮箱----密码----clientid----refresh_token----辅助邮箱----辅助邮箱密码

字段 [3] 形如 ``M.C511_BAY.0.U.MsaArtifacts...`` 是 Microsoft 的 refresh token。

本脚本验证四步：
1. refresh_token 能否换到 access_token
2. IMAP XOAUTH2 能否登录
3. 能否列出邮件
4. 能否读到最近一封邮件（用来找 Qoder 验证信）

用法：
    python tools/verify_outlook_oauth.py
    python tools/verify_outlook_oauth.py --limit 3
"""

from __future__ import annotations

import argparse
import base64
import imaplib
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access"
IMAP_HOST = "outlook.office365.com"


def parse_line(line: str) -> dict | None:
    p = line.strip().split("----")
    if len(p) < 4:
        return None
    return {
        "email": p[0].strip(),
        "password": p[1].strip(),
        "client_id": p[2].strip(),
        "refresh_token": p[3].strip(),
        "recovery_email": p[4].strip() if len(p) > 4 else "",
        "recovery_password": p[5].strip() if len(p) > 5 else "",
    }


def get_access_token(client_id: str, refresh_token: str) -> tuple[str, str]:
    """refresh_token 换 access_token。返回 (token, error)。"""
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=30)
    except Exception as exc:
        return "", f"网络错误: {exc}"

    try:
        j = r.json()
    except Exception:
        return "", f"非 JSON 响应: {r.text[:200]}"

    if "access_token" in j:
        return j["access_token"], ""
    err = j.get("error", "")
    desc = j.get("error_description", "")[:200]
    return "", f"{err}: {desc}"


def imap_xoauth2(user: str, access_token: str, host: str = IMAP_HOST) -> tuple[bool, str]:
    """IMAP XOAUTH2 登录。"""
    auth = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    b64 = base64.b64encode(auth.encode()).decode()

    try:
        conn = imaplib.IMAP4_SSL(host, 993, timeout=30)
    except Exception as exc:
        return False, f"连接失败: {exc}"

    try:
        conn.authenticate("XOAUTH2", lambda _: b64.encode())
        return True, ""
    except Exception as exc:
        return False, f"认证失败: {exc}"
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def check_mailbox(user: str, access_token: str) -> tuple[bool, int, str]:
    """登录后列出 INBOX 邮件数，读最近一封主题。"""
    auth = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    b64 = base64.b64encode(auth.encode()).decode()
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, 993, timeout=30)
        conn.authenticate("XOAUTH2", lambda _: b64.encode())
        conn.select("INBOX")
        typ, data = conn.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        total = len(ids)
        subject = ""
        if ids:
            typ, msg_data = conn.fetch(ids[-1], "(RFC822.HEADER)")
            if msg_data and msg_data[0]:
                raw = msg_data[0][1].decode("utf-8", "ignore")
                for line in raw.splitlines():
                    if line.lower().startswith("subject:"):
                        subject = line[8:].strip()[:80]
                        break
        conn.logout()
        return True, total, subject
    except Exception as exc:
        return False, 0, str(exc)[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="D:/Backup/Downloads/outlook.txt")
    ap.add_argument("--limit", type=int, default=3, help="测试前几个")
    args = ap.parse_args()

    f = Path(args.file)
    if not f.exists():
        print(f"文件不存在：{f}")
        return 2

    lines = [x for x in f.read_text(encoding="utf-8", errors="ignore").splitlines() if x.strip()]
    print(f"=== Outlook OAuth2 验证（共 {len(lines)} 个，测前 {args.limit} 个）===\n")

    results: list[tuple[str, bool, str]] = []

    for i, line in enumerate(lines[: args.limit]):
        acc = parse_line(line)
        if not acc:
            print(f"[{i+1}] 格式异常，跳过")
            continue

        print(f"[{i+1}] {acc['email']}")
        print(f"    client_id: {acc['client_id'][:8]}...")
        print(f"    refresh_token 长度: {len(acc['refresh_token'])}")

        tok, err = get_access_token(acc["client_id"], acc["refresh_token"])
        if not tok:
            print(f"    换 token 失败: {err}")
            results.append((acc["email"], False, f"token: {err}"))
            continue
        print(f"    access_token 长度: {len(tok)}  OK")

        ok, err2 = imap_xoauth2(acc["email"], tok)
        if not ok:
            print(f"    IMAP 登录失败: {err2}")
            results.append((acc["email"], False, f"imap: {err2}"))
            continue
        print(f"    IMAP XOAUTH2 登录 OK")

        ok2, total, subject = check_mailbox(acc["email"], tok)
        if ok2:
            print(f"    INBOX 共 {total} 封，最近主题: {subject or '(无)'}")
            results.append((acc["email"], True, f"{total} 封邮件"))
        else:
            print(f"    读邮箱失败: {subject}")
            results.append((acc["email"], False, f"mailbox: {subject}"))
        print()

    print("=== 结果 ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for email, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {email:<45} {detail[:80]}")
    print(f"\n{passed}/{len(results)} 通过")

    if passed == 0:
        print("\n诊断提示：")
        print("  - AADSTS70000 / invalid_grant → refresh token 已失效或已被使用")
        print("  - AADSTS9002313 → client_id 与 token 不匹配")
        print("  - unauthorized_client → 该 client_id 未授权 IMAP scope")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
