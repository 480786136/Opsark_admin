# DeepSeek 接入与首次调用

## 1. 环境配置

Admin 根目录的 `.env` 只需允许 DeepSeek 的上游主机：

```dotenv
MODEL_ALLOWED_HOSTS=api.deepseek.com
```

这里只填写主机名，不带 `https://`、端口或路径。修改 `.env` 后必须重启 Admin API。

DeepSeek 的上游 API Key 不写入 `.env`，应在 Admin 后台录入，由 `MODEL_KEY_ENCRYPTION_KEY` 加密后保存到数据库。

## 2. 新增供应商

启动 Admin 后打开 `http://127.0.0.1:8001`，登录并进入“供应商 API”，填写：

| 字段 | 值 |
| --- | --- |
| 名称 | `DeepSeek` |
| API base URL | `https://api.deepseek.com` |
| 上游 API Key | DeepSeek 平台申请的 Key |
| 超时秒数 | 建议先用 `120`，再根据监控调整 |
| 启用 | 是 |

保存后可以点击连接测试。连接测试调用上游 `/models`，只证明地址与 Key 基本可用；仍需完成一次真实 Chat Completions 调用。

当前 Opsark Admin 首期只转发 OpenAI Chat Completions 兼容协议，因此不要填写 Anthropic 地址 `https://api.deepseek.com/anthropic`。

## 3. 新增模型路由

进入“模型路由”，为需要开放的模型分别建立路由，例如：

| 对外模型别名 | 供应商 | 上游模型名称 |
| --- | --- | --- |
| `deepseek-v4-flash` | DeepSeek | `deepseek-v4-flash` |
| `deepseek-v4-pro` | DeepSeek | `deepseek-v4-pro` |
| `deepseek-v4-flash-vision-exp` | DeepSeek | `deepseek-v4-flash-vision-exp` |

对外别名可以自定义，但 core 和 API 请求里的 `model` 必须使用这里的“对外模型别名”。视觉模型是否完全兼容当前消息格式，需要用真实图片请求单独验收。

## 4. 签发给 core 的模型 Key

进入“用户模型 Key”：

1. 归属可填 `core-dev` 或实际用户标识。
2. 勾选允许使用的模型别名。
3. 设置有效天数和每分钟请求数。
4. 创建后立即复制以 `omk_` 开头的平台 Key；明文只显示一次。

这个 `omk_` Key 才能交给 core。不要把 DeepSeek 上游 Key、管理员密码或知识库 Key 配给 core。

core 设置如下：

```text
模型 Base URL：http://127.0.0.1:8001/v1
模型 API Key：omk_...
模型名称：deepseek-v4-flash（或已创建的其他对外别名）
```

## 5. 首次 API 调用

在 PowerShell 中使用刚签发的平台 Key测试：

```powershell
$headers = @{ Authorization = "Bearer omk_替换为平台签发的Key" }
$body = @{
  model = "deepseek-v4-flash"
  messages = @(
    @{ role = "user"; content = "你好，请只回复：连接成功" }
  )
  stream = $false
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8001/v1/chat/completions" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

也可以先查看该 Key 有权访问的模型：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8001/v1/models" `
  -Headers $headers
```

调用完成后，可在 Admin 的“调用监控”页面查看状态、耗时、HTTP 状态及供应商返回的 Token 用量。

## 6. 常见错误

- `INVALID_UPSTREAM`：`.env` 的白名单必须是 `api.deepseek.com`，保存后重启 Admin。
- `INVALID_MODEL_KEY`：传入的不是有效且未过期的 `omk_` 平台 Key。
- `MODEL_DENIED`：该平台 Key 没有获准使用请求中的模型别名。
- `UPSTREAM_HTTP_ERROR`：检查 DeepSeek Key、账户额度、上游模型名和请求参数，再查看调用监控。
- `KEY_DECRYPT_FAILED`：`MODEL_KEY_ENCRYPTION_KEY` 被替换，旧供应商 Key 无法解密。
