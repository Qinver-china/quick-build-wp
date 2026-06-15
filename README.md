# Quick Build WP

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub](https://img.shields.io/badge/GitHub-Qinver--china%2Fquick--build--wp-181717?logo=github)](https://github.com/Qinver-china/quick-build-wp)

> 一键搭建 WordPress 环境的全流程自动化工具 —— 填写配置，自动完成宝塔安装、LNMP 部署与 WordPress 建站。

**作者：老唐** · 仓库：[github.com/Qinver-china/quick-build-wp](https://github.com/Qinver-china/quick-build-wp)

---

## 项目简介

Quick Build WP 面向需要快速上线 WordPress 网站的用户，提供**单页向导 + 全自动远程部署**能力。你只需填写 SSH 信息、环境版本和网站配置，系统会通过 SSH 连接目标服务器，依次完成宝塔面板安装、Nginx / PHP / MySQL / Redis 环境搭建、WordPress 安装与 SSL 证书申请，并在完成后输出宝塔面板、网站后台等全部登录信息。

前后端完全分离：前端为纯静态页面（HTML + jQuery），后端为 FastAPI + Celery 异步任务队列，适合 Docker 一键部署，也支持前后端分开托管。

---

## 主要特点

- **单页配置，一键开跑** — 在一个页面填写 SSH、宝塔、LNMP 版本、网站与管理员信息即可开始部署
- **9 步可视化进度** — 宝塔 → Nginx → PHP → MySQL → Redis → PHP 扩展 → 环境优化 → 建站 → SSL，实时展示当前阶段
- **部署前环境检测** — 自动检测 SSH 连通性、系统类型、是否已安装宝塔、PHP 版本兼容性等，非全新环境需二次确认
- **智能跳过与续跑** — 已完成的步骤自动跳过；任务中断后可重试，从缺失步骤继续执行
- **多网站支持** — 一次部署可创建多个 WordPress 站点（不同域名）
- **Redis 对象缓存** — 自动安装 Redis 服务、PHP 扩展及 WordPress Redis Object Cache 插件
- **宝塔面板凭证可靠记录** — 安装后统一写入并记录面板账号密码，结果页以数据库记录为准输出
- **SSL 失败不阻断整体流程** — 证书申请失败时任务仍视为成功，并提示在宝塔手动申请
- **安全设计** — SSH 密码加密暂存，任务成功结束后自动清除；敏感信息仅在结果页展示一次
- **24 小时自动清理** — 定时任务自动清除超过 24 小时的部署记录与相关数据，避免长期占用
- **前后端分离** — 前端可部署到 CDN / 静态托管，后端独立提供 API

---

## 技术架构

| 组件 | 说明 |
|------|------|
| 前端 | 单页 HTML + CSS + jQuery，Nginx 静态托管 |
| 后端 API | FastAPI（Python 3.11） |
| 任务队列 | Celery Worker + Beat |
| 数据库 | PostgreSQL 16 |
| 缓存/消息 | Redis 7 |
| 容器化 | Docker Compose（6 个服务） |

### 部署流水线（9 步）

1. 安装宝塔面板
2. 安装 Nginx
3. 安装 PHP（默认 8.2，支持版本兼容检测与降级）
4. 安装 MySQL
5. 安装 Redis
6. 安装 PHP 组件与扩展
7. 环境参数调优
8. 创建网站并安装 WordPress
9. 申请 SSL 证书（Let's Encrypt，失败仅警告）

---

## 快速开始（Docker）

### 环境要求

- Docker + Docker Compose v2
- 建议内存 2GB 以上

### 启动

```bash
git clone https://github.com/Qinver-china/quick-build-wp.git
cd quick-build-wp

# 下载部署备用资源（WordPress 安装包、WP-CLI 等，约 45MB，不纳入 Git）
bash scripts/download_assets.sh

docker compose up --build -d
```

启动后访问：

| 服务 | 地址 |
|------|------|
| 前端页面 | http://localhost:5173 |
| 部署统计页 | http://localhost:5173/stats.html |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |

查看容器状态：

```bash
docker compose ps
docker compose logs -f api worker beat
```

停止服务：

```bash
docker compose down
```

---

## 使用方式

### 1. 准备目标服务器

- 使用**全新 Linux 服务器**（Ubuntu / Debian / CentOS 等，不支持 Windows）
- 安全组 / 防火墙放行：**22、80、443、8888**
- 准备好 **root SSH 密码**（或具有 root 权限的账号）

### 2. 填写部署配置

打开前端页面，按向导填写：

| 配置项 | 说明 |
|--------|------|
| SSH 地址 / 密码 | 目标服务器的 root 登录信息 |
| 服务器系统类型 | 可选 Ubuntu、Debian、CentOS 或通用 |
| 宝塔账号密码 | 可留空，系统自动生成 |
| Nginx / PHP / MySQL 版本 | 默认 Nginx 1.24、PHP 8.2、MySQL 8.0 |
| 网站信息 | 域名、站点名称、WP 管理员账号等 |
| 多网站 | 可添加多个站点，一次部署全部完成 |

建议先点击 **「检测服务器环境」**，确认 SSH 连通且环境符合要求后再开始部署。

### 3. 等待部署完成

- 页面会显示 9 步进度条与实时日志
- 可随时点击 **「终止任务」** 取消部署
- 部署过程中请勿关闭页面（完成后需保存结果信息）

### 4. 保存结果信息

部署成功后，页面会展示：

- 宝塔面板地址、账号、密码
- 网站地址、后台地址
- WordPress 管理员账号、密码
- 数据库信息

**请立即复制或截图保存**，关闭页面后将无法再次查看（任务记录在 24 小时后自动清理）。

### 5. 后续操作

- 登录宝塔面板，检查站点与 SSL 状态
- 若 SSL 申请失败，在宝塔中为站点手动申请证书即可
- 尽快修改 SSH、宝塔、WordPress 管理员密码

---

## 部署统计（管理端）

系统会在用户确认「一键部署」时写入 `deploy_stats` 进行中记录，任务终止（成功 / 失败 / 用户取消）时更新同一条记录；**不参与 24 小时任务清理**，用于长期统计。

### 配置

1. 在后端环境变量中设置 `ADMIN_STATS_TOKEN`（与 WordPress 插件 CSF 中的「管理统计 Token」保持一致）
2. 访问统计页并携带 Token：
   - 独立前端：`http://localhost:5173/stats.html`（Token 保存在浏览器会话中）
   - WordPress 后台：**Quick Build WP → 部署统计**（Token 从 CSF 配置自动注入）

### 展示内容

- **汇总栏**：今日 / 本周 / 本月的任务总数、成功数、失败数（已取消计入失败）
- **明细表**：IP、网站域名、状态、失败阶段、错误摘要，支持分页与状态筛选

汇总时区默认为 `Asia/Shanghai`，可通过环境变量 `STATS_TIMEZONE` 调整。

---

## 生产环境部署

当前 `docker-compose.yml` 默认面向本地开发（含热重载）。生产部署建议做以下调整。

### 服务器要求

- Linux 服务器（宝塔面板或其他 VPS 均可）
- 已安装 Docker + Docker Compose
- 建议内存 2GB+

### 1. 上传项目

将项目目录上传至服务器，例如：

```bash
/www/wwwroot/quick-build-wp
```

### 2. 修改生产配置

**生成密钥：**

```bash
openssl rand -hex 32
```

**修改 `docker-compose.yml` 中以下内容：**

- `APP_SECRET` → 改为上面生成的随机串
- `CORS_ORIGINS` → 改为实际前端 HTTPS 地址，多个用逗号分隔
- PostgreSQL 密码 → 改为强密码
- `api` 的启动命令 → **去掉 `--reload`**
- 端口绑定 → 建议只绑本机：

```yaml
api:
  ports:
    - "127.0.0.1:8000:8000"

web:
  ports:
    - "127.0.0.1:5173:80"
```

**配置前端 API 地址**（`frontend/index.html`，在 jQuery 引入之前）：

```html
<script>window.QBW_API_BASE = 'https://api.你的域名.com';</script>
```

### 3. 启动服务

```bash
cd /www/wwwroot/quick-build-wp
docker compose up --build -d
```

### 4. 配置反向代理与 HTTPS（宝塔示例）

推荐前后端使用两个域名：

| 域名 | 反向代理目标 |
|------|-------------|
| `deploy.example.com` | `http://127.0.0.1:5173` |
| `api.example.com` | `http://127.0.0.1:8000` |

在宝塔中：添加站点 → 设置反向代理 → 申请 SSL 证书。

同时确保：

- 后端 `CORS_ORIGINS` 包含 `https://deploy.example.com`
- 前端 `QBW_API_BASE` 指向 `https://api.example.com`

**防火墙：** 仅放行 22、80、443；不要对外开放 8000、5173、5432、6379。

### 5. 前后端分开部署（可选）

| 部署位置 | 内容 |
|----------|------|
| CDN / 静态服务器 / 另一台机器 | `frontend/` 目录 |
| Docker 服务器 | `api` + `worker` + `beat` + `postgres` + `redis` |

Docker 中可不启动 `web` 服务，前端通过 `QBW_API_BASE` 指向 API 域名，后端 `CORS_ORIGINS` 填写前端域名即可。

### 6. 运维命令

```bash
# 更新后重建
docker compose up --build -d

# 查看 Worker 日志
docker compose logs -f worker

# 健康检查
curl https://api.你的域名.com/api/health
```

---

## 本地开发（不用 Docker）

```bash
# 后端（需本地 PostgreSQL、Redis）
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Celery Worker
celery -A app.tasks.celery_app worker --loglevel=info

# Celery Beat（定时清理）
celery -A app.tasks.celery_app beat --loglevel=info

# 前端（任意静态服务器）
cd frontend
python3 -m http.server 5173
```

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL 连接串 | — |
| `REDIS_URL` | Redis 连接串 | — |
| `APP_SECRET` | 加密密钥（**生产必改**） | — |
| `ADMIN_STATS_TOKEN` | 管理统计 API Token（**生产必设**） | 空（未配置时统计 API 不可用） |
| `STATS_TIMEZONE` | 统计汇总时区 | `Asia/Shanghai` |
| `CORS_ORIGINS` | 允许的前端来源，逗号分隔 | `http://localhost:5173` |
| `TASK_EXPIRE_HOURS` | 任务数据保留时长（小时） | `24` |
| `RATE_LIMIT_PER_HOUR` | 每小时部署次数限制 | `5` |

---

## 项目结构

```
quick-build-wp/
├── backend/              # FastAPI 后端 + Celery 任务
│   ├── app/
│   │   ├── api/          # REST API
│   │   ├── services/     # 部署逻辑（宝塔、LNMP、WordPress 等）
│   │   ├── tasks/        # Celery 流水线与定时清理
│   │   └── models/       # 数据模型
│   └── Dockerfile
├── frontend/             # 单页静态前端
│   ├── index.html
│   ├── css/
│   └── js/
├── scripts/              # 远程安装脚本模板与资源下载
│   └── download_assets.sh  # 一键下载 backend/assets 备用包
├── docker-compose.yml
└── README.md
```

---

## 安全提示

- 本工具需要目标服务器的 **root 权限**，请仅在**自有服务器**上使用
- SSH 密码在部署期间加密暂存，任务成功结束后自动从数据库清除
- 部署结果中的账号密码请妥善保存，关闭页面后无法再次查看
- 生产环境务必修改 `APP_SECRET` 和数据库密码，并使用 HTTPS

---

## 推荐主题：子比 zibll 主题

WordPress 环境搭建完成后，推荐搭配 **[子比 zibll 主题](https://www.zibll.com/)** 快速打造高颜值、功能完善的网站。

子比（zibll）是专为 WordPress 打造的中文社区 / 论坛 / 商城一体化主题，由 [zibll.com](https://www.zibll.com/) 官方持续维护更新，深受站长喜爱。

**主要亮点：**

- 强大精美的**商城系统** — 支持知识付费、实物商城、卡密自动发货
- 完善的**社区论坛** — 帖子、圈子、付费板块等社交功能开箱即用
- 丰富的**用户系统** — 登录注册、会员等级、消息通知、微信公众号推送
- **模块可视化布局** — 古腾堡编辑器 + 模块导入导出，灵活搭建页面
- **性能优化** — 支持 Redis 对象缓存、OPcache 等加速方案（与本工具自动配置的 Redis 缓存完美配合）
- **持续更新** — 官方文档齐全，功能迭代活跃

官网：[https://www.zibll.com/](https://www.zibll.com/)

---

## 作者

**老唐**

---

## License

本项目采用 [MIT License](LICENSE) 开源协议。

Copyright (c) 2026 老唐
