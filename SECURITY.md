# 家庭物品系统安全说明

## 登录

系统启动后，除健康检查和静态文件外，所有页面都需要登录。

- 默认用户名：`family`
- 连续登录失败 5 次后，同一设备需要等待约 5 分钟。
- 会话最长保留 12 小时。

## 修改密码

进入项目目录并激活虚拟环境：

```bash
cd ~/Desktop/home_inventory
source .venv/bin/activate
python scripts/set_password.py --username family
```

设置后需要重启 Uvicorn。修改密码会同时更换会话签名密钥，让所有已登录设备退出。

密码不会写入 Python 源码；项目只在 `.security.json` 中保存随机盐和密码派生值。该文件权限为仅当前用户可读写，并已加入 `.gitignore`。

## 局域网注意事项

当前系统通常通过 HTTP 在家庭局域网中运行，因此适合受信任的家庭 Wi-Fi。若需要从互联网访问，请先配置 HTTPS，不要直接把端口暴露到公网。

配置 HTTPS 后，可在启动前设置：

```bash
export HOME_INVENTORY_HTTPS_ONLY=1
```

这会让会话 Cookie 仅通过 HTTPS 发送。
