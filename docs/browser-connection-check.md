# 比特浏览器本地 API 与 CDP 连接检查

本文记录独立的浏览器连接检查方法，不运行注册、验证码、设备重置或积分领取流程。

## 当前实测状态

2026-11-13 的检查结果：

| 环节 | 结果 |
| --- | --- |
| 本地服务 `http://127.0.0.1:54345/health` | 返回 `success: true` |
| 环境列表 | 可读取，存在两个已有环境 |
| 运行中的环境 | 当次查询为空 |
| 启动已有环境 | 两个环境都返回缺少 `dynamicIpUrl` |
| Playwright CDP 连接 | 尚未建立，未进入连接步骤 |

这些结果是当次实测记录，不代表客户端当前一定处于相同状态。

## 本地 API 与 CDP 的区别

本地 API 由比特浏览器客户端提供，负责健康检查、环境管理和启动浏览器。
`54345` 是此次使用的管理 API 端口。

CDP 是启动后的 Chromium 浏览器提供的调试接口。浏览器启动成功后，本地 API 才会返回对应的调试地址。
不能把管理 API 地址直接当作 CDP 地址，也不能沿用上一次启动时的调试端口。

因此，`/health` 成功只能证明管理服务可访问。只有 Playwright 连接成功并取得
`Browser.getVersion` 的结果，才能确认本次 CDP 已连通。

## 运行检查

在 PowerShell 中进入项目目录，使用安装了 Playwright 的 Python：

```powershell
Set-Location 'D:\qoder_register'
python -c "from playwright.sync_api import sync_playwright; print('Playwright import OK')"
```

检查已有环境时，将下方占位符替换为比特浏览器中的环境 ID：

```powershell
python tools/check_browser_connection.py --env-id '替换为已有环境ID'
$LASTEXITCODE
```

本地 API 使用其他端口时，可显式指定地址：

```powershell
python tools/check_browser_connection.py --base 'http://127.0.0.1:54345' --env-id '替换为已有环境ID'
```

客户端开启 API 认证时，脚本从进程环境变量 `BITBROWSER_TOKEN` 读取认证值。
请通过本机凭据管理方式注入该变量，不要将实际值写入文档或提交到仓库。

脚本会调用本地健康接口，并请求打开所选已有环境，指定忽略默认 URL、使用空白页参数。
它不修改环境配置，不读取已有页面内容。连接成功后读取浏览器版本、断开检查客户端，并保留浏览器窗口。
若所选环境已经打开，启动参数是否生效取决于客户端对已有窗口的处理。

只运行 `python tools/check_browser_connection.py` 时，仅检查管理服务；由于没有指定环境，退出码为 `2`，不代表 CDP 已连通。

## 成功标准

以下是成功时的输出结构示例，不是本项目已经取得的实测结果：

```text
PASS local API health
PASS browser opened; local CDP endpoint received
PASS Playwright CDP connection
{
  "product": "...",
  "protocolVersion": "...",
  "jsVersion": "..."
}
PASS disconnected; selected browser remains open
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 本次 CDP 连接和版本读取成功 |
| `1` | 连接检查失败，或被用户中断 |
| `2` | 未指定环境，或命令参数不合法 |

## 当前代理报错的含义

`请填写提取URL: dynamicIpUrl` 是比特浏览器在启动环境时返回的配置错误。
它表示当前环境的代理配置要求提供动态代理提取 URL，但启动校验没有取得所需值。
当前请求在浏览器启动阶段就被拒绝，所以还不能据此判断 CDP 或代理出口是否可用。

动态代理提取 URL、代理网关地址和代理供应商的管理 API 文档地址是不同的概念。
管理 API 文档页面本身不是代理提取 URL。该报错也不能证明 IPDEEP 的签名算法有问题：
本次独立检查没有调用 IPDEEP 的管理 API。

本文不包含代理供应商的签名实现、提取链接生成或代理接入上述账号流水线的配置步骤。

## 其他故障定位

| 现象 | 检查方向 |
| --- | --- |
| 本地 API 连接失败 | 客户端是否运行、API 是否开启、本机端口是否一致 |
| 本地 API 拒绝请求 | API 认证、所选环境 ID 和客户端权限 |
| 启动受限，错误码 `11018` | 客户端账号的 API/RPA 启动授权与并发额度 |
| 启动成功，但无 CDP 地址 | 客户端启动响应是否包含 `http` 或 `ws` 字段 |
| `ModuleNotFoundError` | 当前 Python 环境是否安装 Playwright |
| CDP 连接超时 | 浏览器是否仍运行、地址是否属于本次启动、调试端口是否可达 |

检查脚本刻意只输出部分诊断信息；其他异常只显示异常类型，需要结合比特浏览器客户端日志继续定位。
