# Oracle Cloud Always Free 部署指南

这份文档适用于把家庭物品系统部署到 Oracle Cloud Infrastructure，也就是 OCI 的 Always Free 云主机上。这样你的 Mac 关机后，系统仍然可以访问。

官方说明：

- Oracle Cloud Free Tier 包含不会过期的 Always Free 资源。
- Always Free Compute 需要创建在账号的 home region。
- 当前 Always Free Ampere A1 资源适合创建小型 ARM 云主机。
- Block Volume 总计 200 GB Always Free 额度，启动盘也会占用这个额度。

参考：

- <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm>
- <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>

## 推荐配置

创建实例时，优先选择：

- Image：Ubuntu 24.04 或 Ubuntu 22.04
- Shape：`VM.Standard.A1.Flex`
- OCPU：1
- Memory：6 GB 或更低
- Boot volume：默认 50 GB

注意：页面上要看到 `Always Free eligible` 标识。没有这个标识就不要创建。

你的项目很小，1 OCPU + 1 到 2 GB 内存也够用。如果控制台资源紧张，可以多试几个 availability domain，或过段时间再试。

## 1. 创建 Oracle Cloud 账号

进入 Oracle Cloud Free Tier：

<https://www.oracle.com/cloud/free/>

注册时通常需要银行卡验证。只要资源保持在 Always Free 范围内，不应产生费用；但仍建议创建后立刻设置预算提醒。

## 2. 创建预算提醒

在 OCI 控制台里：

1. 搜索 Budgets。
2. 创建一个 Budget。
3. 金额可以设为很小，例如 1 美元。
4. 添加邮件告警。

这一步不是必须，但很建议做。

## 3. 创建云主机

进入：

```text
Compute → Instances → Create instance
```

建议设置：

- Name：`home-inventory`
- Image：Ubuntu
- Shape：`VM.Standard.A1.Flex`
- OCPU：1
- Memory：1 GB 到 6 GB
- Networking：创建或选择默认 VCN
- Public IPv4 address：启用
- SSH keys：上传你本机的 SSH 公钥，或让 Oracle 生成一对新的

如果使用你 Mac 上现有公钥，可以在终端查看：

```bash
cat ~/.ssh/id_ed25519.pub
```

把输出复制到 OCI 的 SSH public key 输入框。

## 4. 开放网络端口

如果你准备先用 HTTP 测试，需要开放：

- TCP 8000

如果正式公网访问，建议用 HTTPS，需要开放：

- TCP 80
- TCP 443

在 OCI 控制台中通常需要配置：

```text
Virtual Cloud Networks → 你的 VCN → Security Lists 或 Network Security Groups
```

添加 ingress rules：

```text
Source CIDR: 0.0.0.0/0
IP Protocol: TCP
Destination Port Range: 80
```

```text
Source CIDR: 0.0.0.0/0
IP Protocol: TCP
Destination Port Range: 443
```

测试阶段如果不用 Nginx/Caddy，也可以临时开放 8000：

```text
Destination Port Range: 8000
```

正式使用时建议关闭 8000，只暴露 80/443。

## 5. 登录服务器

在本机终端里：

```bash
ssh ubuntu@你的服务器公网IP
```

如果你选择的是 Oracle Linux，用户名可能是：

```bash
ssh opc@你的服务器公网IP
```

## 6. 安装 Docker

Ubuntu 上可以运行：

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

然后退出 SSH，重新登录一次，让 Docker 用户组生效。

检查：

```bash
docker --version
docker compose version
```

## 7. 下载你的项目

服务器里运行：

```bash
git clone https://github.com/milaZ1129/home_inventory.git
cd home_inventory
```

## 8. 配置环境变量

复制示例文件：

```bash
cp .env.example .env
nano .env
```

建议先这样配置：

```bash
HOME_INVENTORY_ENV=production
HOME_INVENTORY_DATA_DIR=/data
HOME_INVENTORY_USERNAME=family
HOME_INVENTORY_PASSWORD=换成一个强密码
HOME_INVENTORY_HTTPS_ONLY=0
HOME_INVENTORY_ALLOWED_HOSTS=
```

说明：

- 先用 `HOME_INVENTORY_HTTPS_ONLY=0` 测试 HTTP。
- 等配置好 HTTPS 后，再改成 `HOME_INVENTORY_HTTPS_ONLY=1`。
- 如果你绑定了域名，再设置 `HOME_INVENTORY_ALLOWED_HOSTS=你的域名`。

## 9. 启动应用

```bash
docker compose up -d --build
```

检查日志：

```bash
docker compose logs -f
```

浏览器访问：

```text
http://你的服务器公网IP:8000
```

确认可以登录后，建议从云端环境变量里移除明文密码：

```bash
nano .env
```

删除这一行：

```bash
HOME_INVENTORY_PASSWORD=换成一个强密码
```

然后重启：

```bash
docker compose up -d
```

因为 `/data/.security.json` 已经生成，系统后续会继续用这个安全配置。

## 10. 配置 HTTPS

正式使用不要长期暴露 HTTP。最简单路线是给服务器绑定一个域名，然后用 Caddy 自动申请 HTTPS 证书。

### 10.1 域名解析

在你的域名 DNS 后台添加：

```text
类型：A
名称：inventory
值：你的服务器公网IP
```

假设域名是 `example.com`，访问地址就是：

```text
https://inventory.example.com
```

### 10.2 新建 Caddyfile

在项目目录创建 `Caddyfile`：

```text
inventory.example.com {
    reverse_proxy home-inventory:8000
}
```

### 10.3 修改 docker-compose.yml

可以把 compose 改成类似这样：

```yaml
services:
  home-inventory:
    build: .
    container_name: home-inventory
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/data

  caddy:
    image: caddy:2
    container_name: home-inventory-caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - home-inventory

volumes:
  caddy_data:
  caddy_config:
```

然后 `.env` 里改成：

```bash
HOME_INVENTORY_HTTPS_ONLY=1
HOME_INVENTORY_ALLOWED_HOSTS=inventory.example.com
```

重启：

```bash
docker compose up -d
```

正式 HTTPS 能访问后，建议在 OCI 安全规则里关闭 8000，只保留 80 和 443。

## 11. 更新代码

以后我帮你修改并推送 GitHub 后，你在服务器里运行：

```bash
cd ~/home_inventory
git pull
docker compose up -d --build
```

## 12. 备份

应用会把数据放到：

```text
./data/
```

这个目录里包括：

- SQLite 数据库
- 自动备份
- 登录安全配置

建议定期做三件事：

1. 在网页“数据备份”里下载 `.db` 文件。
2. 用 OCI 的 Boot Volume Backup 备份云主机。
3. 不要把 `./data/` 提交到 GitHub。

## 13. 常见问题

### 为什么打不开？

检查：

```bash
docker compose ps
docker compose logs -f
```

再检查 OCI Security List / NSG 是否开放了端口。

### 为什么重启后数据没了？

检查 `docker-compose.yml` 里是否有：

```yaml
volumes:
  - ./data:/data
```

没有这个挂载，容器重建时可能丢数据。

### 会不会收费？

只要你创建的是 Always Free eligible 资源，并且不超过免费额度，理论上不收费。但云平台规则可能变化，所以建议：

- 创建预算提醒；
- 不要创建不带 Always Free eligible 标识的资源；
- 不要随便开大规格；
- 定期看 Billing 页面。
