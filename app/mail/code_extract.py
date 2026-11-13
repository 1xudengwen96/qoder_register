"""Qoder 验证码邮件解析。

实测的坑（2026-11-12）
----------------------
Qoder 的验证邮件**纯文本正文是空的**，只有 HTML。
用 ``\\b\\d{6}\\b`` 匹配会抓到一堆 **CSS 颜色值**：

    实测匹配结果: ['232323', '464646', '232323', '296366', '464646', '464646']
                    ↑颜色    ↑颜色     ↑颜色    ↑真码     ↑颜色     ↑颜色

真验证码 `296366` 的实际 HTML：

    <td style="font-size:40px;font-family:SF Pro;font-weight:normal;
               line-height:150%;letter-spacing:5px;color:#232323;
               margin-bottom:40px"> 296366 </td>

识别特征：
1. **font-size 最大**（40px，正文其他文字是 16~24px）
2. **独立标签包裹**：``> 296366 <``，前后是标签边界
3. 有 ``letter-spacing``（字间距，便于阅读）
4. 颜色值 ``#232323`` 前面有 ``#``，容易排除

提取策略（按优先级）
--------------------
1. 找 ``font-size:Npx`` 与 6 位数字同处一个标签的，取 N 最大
2. 找 ``> CODE <`` 形式（前后是标签），排除 ``#hex``
3. 兜底：全文找 6 位数字，但排除 ``#`` 开头的十六进制颜色
"""

from __future__ import annotations

import re

# 6 位数字，且前面不是 # （排除 CSS 颜色）
_CODE_RE = re.compile(r"(?<![#\w])(\d{6})(?![\w])")

# font-size:Npx ... > CODE <
_FONT_SIZE_RE = re.compile(
    r"font-size\s*:\s*(\d{1,3})\s*px[^>]*?>\s*(\d{6})\s*<",
    re.IGNORECASE | re.DOTALL,
)

# 独立标签包裹的 6 位数字：> CODE <
_TAG_WRAPPED_RE = re.compile(r">\s*(\d{6})\s*<")

# 十六进制颜色
_HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _strip_colors(html: str) -> str:
    """把 #rrggbb 之类的颜色值替换掉，避免误匹配。"""
    return _HEX_COLOR_RE.sub("#COLOR", html)


def extract_code(text: str) -> str:
    """从邮件正文提取 6 位验证码。空串表示没找到。"""
    if not text:
        return ""

    # ---- 策略 1：font-size 最大的那个 ----
    matches = _FONT_SIZE_RE.findall(text)
    if matches:
        # (size, code)，按 size 降序
        matches.sort(key=lambda x: int(x[0]), reverse=True)
        best_size = int(matches[0][0])
        # 只在最大字号那一档里取（避免抓到次要元素）
        top = [c for s, c in matches if int(s) >= best_size - 2]
        if top:
            return top[0]

    # ---- 策略 2：独立标签包裹 ----
    cleaned = _strip_colors(text)
    wrapped = _TAG_WRAPPED_RE.findall(cleaned)
    if wrapped:
        return wrapped[0]

    # ---- 策略 3：兜底，排除颜色 ----
    cands = _CODE_RE.findall(cleaned)
    if cands:
        return cands[0]

    return ""


def extract_code_details(text: str) -> dict:
    """返回提取过程细节，便于排查。"""
    matches = _FONT_SIZE_RE.findall(text)
    cleaned = _strip_colors(text)
    return {
        "font_size_matches": matches[:10],
        "tag_wrapped": _TAG_WRAPPED_RE.findall(cleaned)[:10],
        "all_6digit_after_strip": _CODE_RE.findall(cleaned)[:10],
        "picked": extract_code(text),
    }


def is_qoder_mail(sender: str, subject: str, body: str = "") -> bool:
    """判断是不是 Qoder 的验证邮件。

    实测发件人是 ``<no-reply@qoder.com>``，主题
    ``Verify your Email with Qoder``。
    """
    blob = f"{sender} {subject}".lower()
    if "qoder.com" in blob:
        return True
    if "qoder" in blob:
        return True
    low_body = (body or "").lower()
    return "qoder" in low_body and ("verify" in low_body or "code" in low_body)
