"""解压并提取会话日志里的可读文本。

用法：
    python tools/read_session.py --session d7567110-8d8c-4344-8c59-8b79f294b510
    python tools/read_session.py --session <id> --grep qoder
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SESS_ROOT = Path.home() / ".dsh" / "sessions"


def find_session_file(sid: str) -> Path | None:
    if not SESS_ROOT.exists():
        return None
    for p in SESS_ROOT.rglob(f"session-{sid}*"):
        if p.is_file():
            return p
        for f in p.rglob("*"):
            if f.is_file() and f.suffix in (".zstd", ".jsonl"):
                return f
    return None


def decompress(path: Path) -> str:
    """解压会话日志。

    注意：这类日志是**追加式多帧 zstd**（每次写入压缩一帧再追加），
    一次性 ``decompress()`` 只能拿到第一帧。必须用 stream_reader 逐帧读完。
    """
    raw = path.read_bytes()
    if path.suffix == ".zstd" or raw[:4] == b"\x28\xb5\x2f\xfd":
        import io

        import zstandard as zstd

        out: list[bytes] = []
        # 方式一：流式读取（能处理多帧）
        try:
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(io.BytesIO(raw)) as reader:
                while True:
                    chunk = reader.read(1 << 20)
                    if not chunk:
                        break
                    out.append(chunk)
            if out:
                return b"".join(out).decode("utf-8", "ignore")
        except Exception:
            pass

        # 方式二：手动逐帧解压
        try:
            dctx = zstd.ZstdDecompressor()
            buf = raw
            parts: list[bytes] = []
            while buf:
                dobj = dctx.decompressobj()
                parts.append(dobj.decompress(buf))
                unused = dobj.unused_data
                if not unused or unused == buf:
                    break
                buf = unused
            return b"".join(parts).decode("utf-8", "ignore")
        except Exception:
            pass

        # 兜底
        return zstd.ZstdDecompressor().decompress(
            raw, max_output_size=500 * 1024 * 1024).decode("utf-8", "ignore")
    return raw.decode("utf-8", "ignore")


def iter_messages(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def extract_texts(obj) -> list[str]:
    """从一条记录里抽出所有可见文本。"""
    out: list[str] = []

    def walk(node, depth=0):
        if depth > 8:
            return
        if isinstance(node, dict):
            t = node.get("type")
            if t == "text" and isinstance(node.get("text"), str):
                out.append(node["text"])
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str) and len(node) > 40:
            out.append(node)

    walk(obj)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--grep", default="", help="只保留含此关键词的段落")
    ap.add_argument("--role", default="", help="user / assistant")
    ap.add_argument("--out", default="data/session_dump.txt")
    ap.add_argument("--max", type=int, default=200, help="最多输出多少段")
    args = ap.parse_args()

    f = find_session_file(args.session)
    if f is None:
        print(f"找不到 session {args.session}")
        return 2

    print(f"文件：{f}  ({f.stat().st_size} bytes)")
    text = decompress(f)
    print(f"解压后 {len(text)} 字符")

    lines = text.splitlines()
    print(f"共 {len(lines)} 行记录")

    # 统计角色
    role_count: dict[str, int] = {}
    chunks: list[tuple[str, str]] = []

    for obj in iter_messages(text):
        msg = obj.get("message") or obj
        role = msg.get("role") or obj.get("role") or ""
        if role:
            role_count[role] = role_count.get(role, 0) + 1
        for t in extract_texts(msg):
            chunks.append((role, t))

    print(f"角色分布：{role_count}")
    print(f"提取到 {len(chunks)} 段文本")

    # 过滤
    sel = chunks
    if args.role:
        sel = [(r, t) for r, t in sel if r == args.role]
    if args.grep:
        pat = re.compile(args.grep, re.IGNORECASE)
        sel = [(r, t) for r, t in sel if pat.search(t)]

    print(f"过滤后 {len(sel)} 段\n")

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for i, (r, t) in enumerate(sel[: args.max], 1):
            fh.write(f"\n{'='*70}\n[{i}] role={r}\n{'-'*70}\n{t}\n")
            print(f"\n{'='*70}\n[{i}] role={r}\n{'-'*70}")
            print(t[:1500])

    print(f"\n已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
