# 公网部署说明

这个项目现在支持通过环境变量配置，适合放到云服务器、Render、Railway、Fly.io 或 Docker 主机上运行。

## 推荐路线

如果只是自己和家人访问，最安全的方式仍然是 Tailscale / ZeroTier 这类私有网络。

如果需要真正公网网址，请使用 HTTPS，不要把本机开发命令直接暴露到互联网。

## 关键环境变量

复制 `.env.example` 为 `.env`，或在云平台后台添加这些变量：

```bash
HOME_INVENTORY_ENV=production
HOME_INVENTORY_DATA_DIR=/data
HOME_INVENTORY_USERNAME=family
HOME_INVENTORY_PASSWORD=换成强密码
HOME_INVENTORY_HTTPS_ONLY=1
HOME_INVENTORY_ALLOWED_HOSTS=你的域名
```

说明：

- `HOME_INVENTORY_DATA_DIR` 应指向持久化磁盘；数据库、备份和 `.security.json` 都会放在这里。
- 首次启动时，如果没有 `.security.json`，系统会用 `HOME_INVENTORY_USERNAME` 和 `HOME_INVENTORY_PASSWORD` 自动创建。
- 自动创建后，建议从云平台环境变量里删除 `HOME_INVENTORY_PASSWORD`，避免长期保存明文密码。
- 使用公网 HTTPS 时，`HOME_INVENTORY_HTTPS_ONLY=1` 必须开启。
- 设置 `HOME_INVENTORY_ALLOWED_HOSTS` 后，只有这些域名能访问，多个域名用英文逗号分隔。

## Docker 本地试运行

```bash
cd ~/Desktop/home_inventory
cp .env.example .env
```

编辑 `.env`，把密码改掉。本地通过 `http://localhost:8000` 测试时，先保持：

```bash
HOME_INVENTORY_HTTPS_ONLY=0
```

然后运行：

```bash
docker compose up --build
```

访问：

```text
http://localhost:8000
```

## 云端运行命令

不使用 Docker 时，可以在云平台设置启动命令：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --proxy-headers
```

云平台启用 HTTPS 后，请设置：

```bash
HOME_INVENTORY_HTTPS_ONLY=1
```

## 数据与备份

生产环境里最重要的是持久化目录：

- 数据库：`$HOME_INVENTORY_DATA_DIR/home_inventory.db`
- 自动备份：`$HOME_INVENTORY_DATA_DIR/data_backups/`
- 登录安全配置：`$HOME_INVENTORY_DATA_DIR/.security.json`

如果云平台没有持久化磁盘，服务重启或重新部署后数据可能丢失。

## 安全提醒

- 不要提交 `.env`、`.security.json`、数据库或备份文件。
- 不要使用弱密码。
- 不要用 `--reload` 跑生产环境。
- 不要直接用 HTTP 暴露到公网；请使用平台自带 HTTPS 或反向代理 HTTPS。
- 定期从系统里的“数据备份”页面下载一份数据库备份到别处保存。
