# Opsark_admin 平台管理


**2026-09-27 J3 更新**：模型路由可明确选择 Chat Completions 或 Responses。Responses 首批仅接受文本、非流式与客户端上下文；按同一协议转发，不开放原生工具、后台生成或供应商续接。部署时先执行 `alembic upgrade head`（0011）；既有自定义路由保留旧能力契约，新增路由使用版本化 `capabilitiesV2`，未知输出能力需管理员明确声明。`X-Opsark-Expected-Capability-Revision` 由网关预检消费，不转交上游。官方账号继续按实际 input + output 用量扣费，cached/reasoning 单独记录且不重复计费；缺失用量保持未知。模型 Key 保留原有授权与 RPM 语义，不继承账号幂等/余额权限。详见根项目 J3 实施记录。

更新：2026-09-08。目标：本项目独立负责多家第三方模型 API 配置、模型映射、用户模型 Key、实际转发和调用监控，后续承载用户/计费；不负责知识管理。知识项目应有自己的管理页面和鉴权。

**2026-09-08 代码更新**：知识管理已迁至知识项目。Admin 现有供应商配置、模型路由、用户模型 Key、Chat Completions 兼容协议的 JSON/SSE 转发及调用监控。真实供应商、生产部署和完整一期目标尚未验收。升级/启动请优先使用 [独立系统开发版使用与验收](docs/独立系统开发版使用与验收.md)，其中包含新增环境变量、迁移与端口说明。没有改动用户 .env 或升级用户数据库。

## 服务器部署（zgspace.cn）

服务器需安装 Git、Docker Engine、Docker Compose 插件和 Nginx，并将 `zgspace.cn` 的 DNS A/AAAA 记录指向服务器。当前生产管理页使用 `https://zgspace.cn/opsark-admin/`，与官网、原 `/admin/` 和 Java `/api/` 共存。

```bash
sudo mkdir -p /srv/opsark
sudo chown "$USER":"$USER" /srv/opsark
cd /srv/opsark
git clone https://github.com/480786136/Opsark_admin.git admin
cd admin
cp .env.example .env 2>/dev/null || touch .env
openssl rand -hex 32
```

把最后一条命令生成的值作为数据库密码，并编辑 `.env`。已有 `MODEL_KEY_ENCRYPTION_KEY` 的部署必须继续使用原值，不能重新生成：

```dotenv
PLATFORM_DB_PASSWORD=生成的数据库密码
COOKIE_SECURE=true
ALLOWED_ORIGINS=https://zgspace.cn
MODEL_KEY_ENCRYPTION_KEY=现有Fernet密钥
GITHUB_CLIENT_ID=Ov23li9wegtr4OvOmtIW
GITHUB_CLIENT_SECRET=在服务器上填写GitHubClientSecret
GITHUB_CALLBACK_URL=https://zgspace.cn/api/core/v1/auth/github/callback
```

启动服务并首次创建管理员：

```bash
docker compose up -d --build
docker compose exec api python -m app.bootstrap
docker compose ps
curl http://127.0.0.1:8001/health/live
```

在 `zgspace.cn` 现有 HTTPS `server` 中加入以下路由，并放在通用 `/api/` 和 `/` 路由之前：

```nginx
location = /api/core/v1/auth/github/callback { access_log off; proxy_pass http://127.0.0.1:8001; }
location = /api/core/v1/feedback { client_max_body_size 86m; proxy_pass http://127.0.0.1:8001; }
location = /health/live { access_log off; proxy_pass http://127.0.0.1:8001; }
location ^~ /api/core/ { proxy_pass http://127.0.0.1:8001; }
location ^~ /api/admin/v1/ { proxy_pass http://127.0.0.1:8001; }
location ^~ /v1/ { proxy_pass http://127.0.0.1:8001; proxy_buffering off; proxy_read_timeout 300s; }
location = /opsark-admin { return 301 /opsark-admin/; }
location ^~ /opsark-admin/ {
    proxy_pass http://127.0.0.1:8001/;
    sub_filter_once off;
    sub_filter '/assets/' '/opsark-admin/assets/';
}
```

运行 `sudo nginx -t && sudo systemctl reload nginx`。生产环境需使用有效 HTTPS 证书。

部署后检查：

```bash
curl https://zgspace.cn/health/live
curl https://zgspace.cn/api/core/v1/auth/github/config
```

第二个接口应返回 `{"enabled":true}`。更新版本时先备份数据库卷和 `MODEL_KEY_ENCRYPTION_KEY`，再执行：

```bash
cd /srv/opsark/admin
git pull --ff-only
docker compose up -d --build
```

## 三个平级项目

- 当前路径：`E:\Opsark\Opsark_admin`，平台 API 默认 8001。
- `E:\Opsark\Opsark_knowledge`，知识 API 默认 8002，独立 Worker、数据库、依赖和迁移。
- `E:\Opsark\Opsark_core`，桌面客户端，独立开发发布。

旧路径 `E:\vscode` 在当前环境不存在。运行命令均以各项目根目录为工作目录。此前混合业务骨架中的知识模型/契约已经迁至 knowledge，没有迁移或删除用户业务数据库。

## 当前已实现

- 管理员初始化、登录、Cookie 会话、CSRF、退出。
- Vue 模型管理页面：供应商 API、模型路由、用户模型 Key、调用监控。
- 知识页面/代理已移除，旧代理返回 410；不调用知识服务，不访问其数据库。
- 平台、知识两个独立 Compose；可选 LiteLLM 模型网关配置。

知识服务已实现上传幂等、敏感模式检查、模板草稿、持久任务、版本化发布、关键词/可选 Embedding 检索、来源引用、撤销 Key、下架和删除。详见知识项目 README。

当前为可运行的开发版，不是一期全部生产验收完成：真实供应商/core 模型兼容、Docker/PostgreSQL 环境验收、生产 HTTPS、性能与备份恢复尚未验证。代码没有生成真实供应商 Key，也没有新增用户收费或长期记忆。

## 本地启动

本节安装/构建命令仍有效，模型加密主密钥按 [最新启动说明](docs/独立系统开发版使用与验收.md) 配置。上游主机白名单已移除。模型对外地址现在是 Admin 自身的 /v1，不是可选模板的 4000 端口。

需要 Python 3.12+（本次验证 3.14）和 Node.js 22.12+。首次安装：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -c constraints.txt -e ".[test]"
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m app.bootstrap
```

不再需要 KNOWLEDGE_SERVICE_TOKEN。现有 .env 不覆盖，已有管理员不必重新创建。用下面命令生成 MODEL_KEY_ENCRYPTION_KEY 安全保存到 .env。无需配置 MODEL_ALLOWED_HOSTS，旧值会被忽略。不要将主密钥提交 Git 或随意替换，详见最新启动说明。

```powershell
.venv/Scripts/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

前端安装和构建（在 `web` 目录）：

```powershell
npm ci
npm run build
```

返回平台根目录启动：

```powershell
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --workers 1
```

浏览器打开 http://127.0.0.1:8001。开发热更新可在 web 运行 `npm run dev`，地址 http://127.0.0.1:5174。修改 `.env` 后重启服务。

Admin 模型业务不依赖知识 API/Worker。管理员密码只在初始化提示中输入，不应写在 README 或提交到版本库。

## 模型接入

一期必做：多供应商 API 地址/Key/协议/启停/测试；对外模型别名映射；用户模型 Key 的归属、权限、有效期、撤销与限流；普通 JSON/SSE 真实转发；调用量、成功率、耗时、状态及 Token 用量监控。缺失 usage 标未知，默认不存正文/密钥。无需自部署模型，不要求 GPU。

本轮实现了受限 Chat Completions 兼容适配器，Admin 数据库是渠道/路由/Key 的权威来源。不是所有第三方原生协议都支持，真实供应商 PoC 仍待验收。LiteLLM 模板仅作可选实验，不参与当前内置转发配置。

以下仅描述现有可选模板，不是已上线业务：

- 旧模板 endpoint 可为 http://127.0.0.1:4000/v1；本轮 Admin 内置转发使用 http://127.0.0.1:8001/v1，旧 MODEL_BASE_URL 不控制新路由。
- 模板 Key 与 Admin 新签发 omk_ Key 不通用。使用内置转发时由“用户模型 Key”页面签发。
- 知识 Key 已迁至知识自己的“客户端 Key”页面，只访问 knowledge。
- 不部署模型权重，不要求 GPU；Embedding 可留空，以明确的关键词模式调试。

## 容器开发模板

Compose/Dockerfile 已调整为各系统自含运行；尚未执行 Docker/PostgreSQL 验收。先按最新说明配置模型加密主密钥与白名单。

不再需要创建共享 opsark-services 外部网络，两个项目各运行自己的 Compose，数据库/数据卷独立。

在各自 .env 设置数据库密码及本服务所需配置后运行 docker compose up --build -d；Admin 需要 MODEL_KEY_ENCRYPTION_KEY，知识管理不需要服务 Token。数据库密码建议 URL-safe；保留字符需 URL 编码。

平台容器首次创建管理员：`docker compose exec api python -m app.bootstrap`。

可选模型 profile：配置 `MODEL_DB_PASSWORD`、`LITELLM_MASTER_KEY`、`LITELLM_SALT_KEY`、`MODEL_ADMIN_USERNAME`、`MODEL_ADMIN_PASSWORD`、`UPSTREAM_MODEL`（例如带 provider 前缀的真实模型名）、`UPSTREAM_BASE_URL`、`UPSTREAM_API_KEY` 后，运行 `docker compose --profile models up -d`。

这些是本地开发模板，端口仅绑定 localhost；镜像 tag 尚非生产 digest 锁定。上线前必须锁定镜像、验证供应商参数、配置 HTTPS（`COOKIE_SECURE=true` 与正确 Origin）、隔离内部接口并完成数据库备份恢复验证。

## 验证和设计

```powershell
.venv/Scripts/python -m pytest -q
```

- [一期方案总览](docs/一期方案总览.md)
- [一期技术选型](docs/一期技术选型.md)
- [一期接口契约](docs/一期接口契约.md)
- [一期实施计划与验收](docs/一期实施计划与验收.md)
- [后续功能待办](docs/后续功能待办.md)
- [拆分决策记录](docs/ADR-002-知识服务一期独立.md)
- [知识长期演进](../Opsark_knowledge/docs/知识服务长期演进.md)
- [开发版验证记录](docs/开发版验证记录.md)

旧方案原文保存在 docs/history，仅供追溯，不再作为当前部署说明。
