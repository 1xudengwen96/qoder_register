"""修 OTP 注入的 %-format 崩溃：模板改用占位符替换。"""

from __future__ import annotations

from pathlib import Path

P = Path("app/register/qoder_signup_cdp.py")
s = P.read_text(encoding="utf-8")

# 1) 模板里的 %s 改成命名占位符，注释里的 100% 去掉
s = s.replace("  const e = document.querySelector(%s);",
              "  const e = document.querySelector(__SEL__);")
s = s.replace("  const code = %s;", "  const code = __CODE__;")
s = s.replace("// 2) 直接调 React 的 onChange —— 受控组件只有这条 100% 生效",
              "// 2) 直接调 React 的 onChange —— 受控组件只有这条必定生效")

# 2) 加一个安全的渲染方法
helper = '''
    def _js_otp_fill(self, selector_json: str, code_json: str) -> str:
        """安全渲染 JS_OTP_FILL（不要用 % 格式化：模板里有字面 % 会炸）。"""
        return (self.JS_OTP_FILL
                .replace("__SEL__", selector_json)
                .replace("__CODE__", code_json))

'''
anchor = "    def fill_otp(self, code: str) -> str:"
if "_js_otp_fill" not in s:
    s = s.replace(anchor, helper + anchor)

# 3) 两处调用改成 _js_otp_fill
s = s.replace('out = self.page.eval(self.JS_OTP_FILL % (s, json.dumps(target)))',
              'out = self.page.eval(self._js_otp_fill(s, json.dumps(target)))')
s = s.replace('''                    self.page.eval(self.JS_OTP_FILL % (json.dumps(sel2),
                                                       json.dumps(digits)))''',
              '''                    self.page.eval(
                        self._js_otp_fill(json.dumps(sel2),
                                          json.dumps(digits)))''')

P.write_text(s, encoding="utf-8")
import ast  # noqa: E402

ast.parse(P.read_text(encoding="utf-8"))
print("patched & syntax OK")
print("剩余 %-format 调用:", s.count("JS_OTP_FILL %"))
