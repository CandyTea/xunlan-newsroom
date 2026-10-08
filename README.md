# 讯览（Newsroom）v1

讯览是一款单用户、自托管的新闻阅读器。它按游戏、体育、股票和国际新闻分类汇总来源，支持关注词条、收藏、已读状态与定时抓取。视觉方向以财新现有识别度为灵感。

![桌面阅读界面](docs/screenshots/desktop.png)

<img src="docs/screenshots/mobile.png" width="300" alt="手机阅读界面"> <img src="docs/screenshots/schedules.png" width="300" alt="手机收取计划">

截图使用独立测试数据中的实际资讯，关注对象与时间计划为演示设置。正式安装时自行创建账户、添加关注并设置计划。

讯览只整理来源提供的 RSS 摘要和文章链接，不生成 AI 摘要、不翻译文章，也不抓取付费墙全文。关注命中表示该条原始文章与词条匹配，不代表系统生成了新消息。首次安装不会写入虚构新闻；抓取为空时，阅读页会显示真实空状态。

## 已配置来源

初始配置含 11 个已启用 RSS 来源和 2 个默认关闭的 Steam 新闻示例。来源覆盖会随网站 RSS 内容变化；用户也可以在「来源」设置中增删或停用来源。

| 分类 | 已启用来源 |
| --- | --- |
| 游戏 | [游研社](https://www.yystv.cn/rss/feed)、[机核](https://www.gcores.com/rss) |
| 体育 | [中新网体育](https://www.chinanews.com.cn/rss/sports.xml)、[BBC 体育](https://feeds.bbci.co.uk/sport/rss.xml)、[ESPN](https://www.espn.com/espn/rss/news) |
| 股票 | [中新网财经](https://www.chinanews.com.cn/rss/finance.xml)、[FT 中文网](https://www.ftchinese.com/rss/feed)、[The Guardian 财经](https://www.theguardian.com/business/rss) |
| 政治 | [中新网国际](https://www.chinanews.com.cn/rss/world.xml)、[BBC 国际](https://feeds.bbci.co.uk/news/world/rss.xml)、[The Guardian 国际](https://www.theguardian.com/world/rss) |

Steam 环世界（App ID `294100`）和只狼（App ID `814380`）是可选示例，初始关闭。启用后读取 Steam 官方新闻接口。抓取到的标题、来源摘要和原文链接均来自来源页面；覆盖与更新频率由来源决定。

## 功能

- 按游戏、体育、股票、政治分类阅读，可搜索文章并筛选未读、收藏和关注命中。
- 在关注词条中设置公司、工作室、球队、国家或政治人物。名称、别名或股票代码可命中；关键词可作为额外上下文条件，排除词优先。
- 手动抓取来源，并为不同分类设置多个抓取时间。初次安装不会自动创建定时任务；时区默认为 `Asia/Shanghai`，星期一在 API 中记为 `0`。停机漏过的时段会按补跑设置合并，最多补跑一次。
- 「股票」分类整理财经新闻，不提供实时行情或交易功能。
- 文章、阅读状态、关注词条、来源、定时任务和设置保存在本机 SQLite 数据库中。
- 提供 JSON 配置导出。导出仅含设置、来源、关注词条和定时任务，不含文章数据库或登录凭据。

需要让 RSS 或 Steam 请求经由代理时，可在 `.env` 显式设置 `NEWSROOM_OUTBOUND_PROXY`，例如填入一个容器网络可访问的 HTTP 代理地址。留空时直接出站；应用不会自动读取主机上的 `HTTP_PROXY` 或 `HTTPS_PROXY`。容器里的 `127.0.0.1` 指容器本身，不是服务器主机；若代理运行在主机上，需使用一个允许容器访问的主机地址，并确认代理监听该接口。

## Windows 本地预览

需要 Python 3.11。项目依赖安装在本地 `.venv\Lib\site-packages`；启动脚本忽略 pip 环境变量和用户配置，并显式指定安装目录，避免写入全局 target：

```powershell
.\run-local.ps1
```

打开 `http://127.0.0.1:8000`。若希望手动启动，可在 PowerShell 中运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip --isolated install --upgrade --target .\.venv\Lib\site-packages --disable-pip-version-check -r requirements.txt
$env:NEWSROOM_DATA_DIR = "$PWD\data"
$env:TZ = "Asia/Shanghai"
$env:COOKIE_SECURE = "0"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

应用内的定时调度器使用单进程运行；本地和 Docker 命令都固定为一个 Uvicorn worker。不要把 `--workers` 调大，否则多个 worker 会各自启动调度器。

调度器每 15 秒检查一次到期计划；若已有采集正在运行，会在这轮采集结束后处理到期计划。

## OpenCloudOS 9 部署

从 GitHub 下载后，在服务器上进入仓库目录。公开仓库可以使用 `git clone <仓库地址>`；私有仓库需要先配置 GitHub 访问权限。然后按下文安装 Docker、填写 `.env` 并启动应用。

### 1. 准备 Docker 与 Compose v2

如果 Docker 已安装且服务正常运行，跳过安装步骤。腾讯云《搭建 Docker》文档的 **OpenCloudOS 9.0** 章节给出的安装与启动命令是：

```bash
sudo yum install docker -y
sudo systemctl start docker
sudo systemctl enable docker
sudo docker info
```

该文档也列出 `docker info` 作为安装检查。讯览需要 Docker Compose v2，并使用 `docker compose` 命令。先确认：

```bash
docker compose version
```

如果命令不可用，请按 [Docker 官方 Linux Compose 插件安装说明](https://docs.docker.com/compose/install/linux/) 安装 Compose 插件，再运行版本检查。该指南列出 RPM 系统的仓库安装方式和手动插件安装方式；请按服务器当前 Docker 软件源选择可用方式。

### 2. 设置首次初始化令牌并启动

在服务器上进入本项目目录，创建环境文件：

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把第二条命令输出的随机字符串填入 `.env` 的 `SETUP_TOKEN=`。令牌只用于保护首次创建账户；创建账户后，登录仍使用你设置的用户名和密码。`COOKIE_SECURE=0` 对应下文的本机 SSH 隧道访问。

然后构建并启动：

```bash
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f app
```

默认只监听服务器本机的 `127.0.0.1:8088`。在自己的电脑上建立 SSH 隧道：

```bash
ssh -L 8088:127.0.0.1:8088 <SSH用户>@<服务器公网IP>
```

然后浏览器访问 `http://127.0.0.1:8088`。首次打开时输入 `.env` 中的 `SETUP_TOKEN`，创建单用户账户。

健康检查地址为服务器本机 `http://127.0.0.1:8088/healthz`。容器以非 root 用户运行，SQLite 数据目录 `/data` 使用持久化 Docker 卷；镜像会在首次创建卷时准备可写权限。

### 3. 个人 IP 临时直连

如果要从浏览器直接访问公网 IP，可在项目目录运行以下命令，用公开绑定配置替代本机绑定：

```bash
sudo docker compose --env-file .env --env-file deploy/public-bind.env up -d --build
```

如果所用 Compose v2 版本不支持多个 `--env-file` 参数，也可以直接把 `.env` 中的 `NEWSROOM_BIND` 改为 `0.0.0.0`，然后运行普通启动命令。

随后在腾讯云控制台的实例安全组入站规则中，只允许你当前公网 IP 的 `/32` 来源访问 TCP `8088`。若 OpenCloudOS 主机启用了 `firewalld`，也要在系统防火墙放行该端口。访问地址为 `http://<服务器公网IP>:8088`。

直连 HTTP 只适合受限 IP 下的短期个人预览：登录信息不会经过 TLS 加密。面向长期或多人网络访问时，请配置下面的 HTTPS 入口。没有设置首次初始化令牌时，不要开放公网访问。

### 4. 配置 HTTPS（推荐）

准备一个指向服务器公网 IP 的域名 A 记录；确认公网 TCP `80`、`443` 可到达服务器。编辑 `.env`，把 `NEWSROOM_DOMAIN` 改成真实域名，并设置 `COOKIE_SECURE=1`。然后执行：

```bash
sudo docker compose -f compose.yaml -f deploy/compose.caddy.yaml up -d --build
sudo docker compose -f compose.yaml -f deploy/compose.caddy.yaml ps
```

Caddy 会代理到应用容器并自动申请、续期 TLS 证书；应用端口仍只绑定本机。腾讯云安全组为 TCP `80`、`443` 允许公网访问；若主机启用了 `firewalld`，也放行这两个端口。SSH 端口则继续限制为自己的 IP。打开 `https://<你的域名>` 完成首次初始化。首次配置证书期间可查看：

```bash
sudo docker compose -f compose.yaml -f deploy/compose.caddy.yaml logs -f caddy
```

## 使用说明

- 在「来源」中添加 RSS URL，或填写 Steam App ID。仅支持 RSS 和 Steam 官方新闻接口；不会把任意网页或付费墙页面当作全文来源。
- 在「关注」中填写名称和别名；名称、别名和股票代码按 OR 关系匹配。填写关键词后，匹配结果还需包含关键词；排除词会优先过滤。
- 在「定时」中设置 `HH:mm` 时间、星期和分类。可建立多个时间点，时区使用「设置」中的 `Asia/Shanghai`。
- 阅读卡片中的「来源摘要」是 RSS 或 Steam 提供的文字摘要，经清理后显示；没有来源摘要时不会补写内容。
- 空列表表示当前筛选条件或来源尚无可显示文章。先检查来源状态，再运行手动抓取。
- Steam 官方新闻接口要求容器 DNS 将目标解析到公网地址；如果 DNS 返回私有地址，安全校验会拒绝请求。

## 更新、停止与备份

重新构建并启动更新后的程序：

```bash
sudo docker compose up -d --build
```

停止应用但保留数据库：

```bash
sudo docker compose down
```

不要附加 `-v`，否则 Compose 会删除持久化数据卷。

SQLite 文件位于 `/data/newsroom.sqlite3`。备份前先停止应用，再复制整个数据目录（包括可能存在的 SQLite WAL 文件），完成后重新启动：

```bash
mkdir -p backups
container_id=$(sudo docker compose ps -q app)
sudo docker compose stop app
sudo docker cp "$container_id:/data/." "backups/newsroom-data-$(date +%Y%m%d-%H%M%S)"
sudo docker compose start app
```

`GET /api/export` 可导出设置、来源、关注词条和定时任务配置，适合单独留存配置；完整文章和阅读状态仍需备份 SQLite 数据目录。`.env` 含首次设置令牌等部署信息，应妥善保存，避免提交到公开仓库。

## 常见维护命令

```bash
sudo docker compose ps
sudo docker compose logs -f app
sudo docker compose restart app
```

Docker 官方 Compose 插件文档：[Linux 安装指南](https://docs.docker.com/compose/install/linux/)。腾讯云 OpenCloudOS 9.0 Docker 步骤：[搭建 Docker](https://cloud.tencent.com/document/product/213/46000)。
