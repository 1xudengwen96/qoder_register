from pathlib import Path
p=Path('app/register/bitbrowser.py');s=p.read_text(encoding='utf-8')
s=s.replace('''        if self.http.startswith("http"):
            return self.http
        return f"http://{self.http}"''','''        if not self.http:
            return ""
        if self.http.startswith(("http://", "https://")):
            return self.http
        return f"http://{self.http}"''')
s=s.replace('''        if self.ws.startswith("ws"):
            return self.ws
        return f"ws://{self.ws}"''','''        if not self.ws:
            return ""
        if self.ws.startswith(("ws://", "wss://")):
            return self.ws
        return f"ws://{self.ws}"

    @property
    def cdp_endpoint(self) -> str:
        endpoint = self.cdp_http or self.cdp_ws
        if not endpoint:
            raise BitBrowserError("Missing CDP endpoint")
        return endpoint''')
a=s.index('    def call(');b=s.index('    # ---------------------------------------------------------- 健康',a)
s=s[:a]+'''    def call(self, path: str, body: dict | None = None,
             timeout: int | None = None, retries: int = 2) -> dict:
        if not path.startswith("/") or "://" in path:
            raise ValueError("Expected local API path")
        for attempt in range(retries + 1):
            self._throttle()
            try:
                r = requests.post(self.base + path, json=body or {},
                                  headers=self._headers(),
                                  timeout=timeout or self.timeout)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError):
                raise BitBrowserError(f"{path}: HTTP/transport/JSON failure") from None
            if not isinstance(data, dict):
                raise BitBrowserError(f"{path}: expected response object")
            if data.get("success") is True:
                return data
            msg = str(data.get("msg") or "")
            if "请求频繁" in msg:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RateLimitedError("Local API rate limited")
            if "窗口被限制打开" in msg:
                raise LaunchRestrictedError(
                    "Browser launch restricted; check client permissions and limits",
                    code=data.get("code"))
            raise BitBrowserError(f"{path}: API rejected request", code=data.get("code"))
        raise BitBrowserError("Retries exhausted")

'''+s[b:]
a=s.index('    def list_groups(');b=s.index('    # ---------------------------------------------------------- CDP',a)
s=s[:a]+'''    def list_groups(self, page: int = 0, page_size: int = 100) -> list[dict]:
        d = self.call("/group/list", {"page": page, "pageSize": page_size}).get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("list") or []
        raise BitBrowserError("Invalid groups response")

'''+s[b:]
s=s.replace('''            return r.json()
        except Exception:
            return None

    # ---------------------------------------------------------- 能力自检''','''            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("webSocketDebuggerUrl"):
                return data
            return None
        except (requests.RequestException, ValueError):
            return None

    # ---------------------------------------------------------- 能力自检''')
s=s.replace('def capability_report(self) -> dict:', 'def capability_report(self, allow_lifecycle: bool = False) -> dict:')
s=s.replace('''        # 用一个临时环境试启动，测完删掉
        try:''','''        # Default diagnostics are read-only. Lifecycle probe must be explicit.
        if not allow_lifecycle:
            rep["lifecycle_tested"] = False
            return rep
        rep["lifecycle_tested"] = True
        # 用一个临时环境试启动，测完删掉
        try:''')
p.write_text(s,encoding='utf-8')
