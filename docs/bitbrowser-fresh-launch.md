# 比特浏览器：每次启动都是干净新窗口

## 问题

注册跑几轮之后，环境一启动就带回一堆旧标签页（`about:blank`、`qoder.com`、
还有客户端自己的工作台页 `console.bitbrowser.net/?id=...`），既碍事又容易被
风控看出「同一环境反反复复」。

## 根因（三个字段 + 一个 profile 设置）

| 位置 | 字段 | 现象 |
|---|---|---|
| 环境 | `url` | 是**逗号分隔的列表**，实测值 `about%3Ablank,https%3A%2F%2Fqoder.com%2F` —— 每次启动把列表里每个地址都开一遍 |
| 环境 | `syncTabs` | `true` 时同步标签，会带回上次的 |
| 环境 | `workbench` | 非空（默认 `localserver`）时，客户端每次启动都打开自己的 `console.bitbrowser.net` 工作台标签 |
| profile | `Default/Preferences` → `session.restore_on_startup` | Chrome 层的「恢复上次会话」 |

## 修法

```python
from app.register.bitbrowser import BitBrowserClient

c = BitBrowserClient()
c.prepare_fresh_launch(env_id, "about:blank")
c.open_env(env_id)
```

`prepare_fresh_launch()` 写三个字段：

```python
{"url": "about:blank", "syncTabs": False, "workbench": ""}
```

profile 层的兜底（可选，一次就够）：

```powershell
python tools/bb_no_restore.py --verify
```

它会把 `D:\bitbrowser-cache\<env_id>\Default\Preferences` 里的

```json
{"session": {"restore_on_startup": 4, "startup_urls": []}}
```

写好（`4` = 打开新标签页，不恢复上次会话），然后关掉重开数标签页验证。

## 实测结果

| 阶段 | 启动后标签 |
|---|---|
| 改之前 | 3 个（`about:blank` + `qoder.com` + 工作台） |
| 只改 `url` + `syncTabs` | 1 个（工作台） |
| 再把 `workbench` 置空 | **1 个（纯 `about:blank`）** |

## 顺带修掉的两个次生问题

1. **工具自己开的页签会残留**
   `CDPPage.close_tab()` 会先 `Page.close` 再断连接；`run_signup_cdp.py`
   跑完就关，不再给下次启动留尾巴。

2. **工具复用运行中的环境时不会重开**
   `run_signup_cdp.py` 的 `resolve_endpoint()` 现在会：环境在跑 → 先关 →
   `prepare_fresh_launch()` → 再开。所以每次跑注册都是一个干净窗口。

## 相关工具

| 命令 | 作用 |
|---|---|
| `python tools/bb_env_fields.py` | 打印环境里所有跟标签/会话/同步相关的字段 |
| `python tools/bb_no_restore.py --verify` | 改配置 + 修 Preferences + 数标签页 |
| `python tools/bb_status.py --pages` | 看当前运行的标签页 |
| `python tools/bb_close.py` | 关环境 |
