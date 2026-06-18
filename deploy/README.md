# 生产部署（systemd 开机自启）

本目录为**生产环境**提供 systemd 服务单元，实现**主服务 + 内网穿透**开机自启。  
当前示例路径为 **`/opt/voicebridge`**，运行用户 **`voicebridge`**。部署到其他机器时请修改各 `.service` 中的路径与 `User`/`Group`。

## 文件说明

| 文件 | 用途 |
|------|------|
| `voicebridge.service` | VoiceBridge 主服务（API + 前端 + WebSocket） |
| `natapp.service` | NATAPP 内网穿透（在主服务之后启动） |

## 一键安装（主服务 + 内网穿透均开机自启）

在项目根目录执行：

```bash
cd /opt/voicebridge

# 1. 安装两个服务单元
sudo cp deploy/voicebridge.service /etc/systemd/system/
sudo cp deploy/natapp.service /etc/systemd/system/

# 2. 重载并启用开机自启
sudo systemctl daemon-reload
sudo systemctl enable voicebridge
sudo systemctl enable natapp

# 3. 立即启动（先主服务，再内网穿透）
sudo systemctl start voicebridge
sudo systemctl start natapp

# 4. 确认状态
sudo systemctl status voicebridge
sudo systemctl status natapp
```

## 常用命令

| 操作 | 命令 |
|------|------|
| 主服务状态 | `sudo systemctl status voicebridge` |
| 主服务日志 | `journalctl -u voicebridge -f` |
| 主服务重启 | `sudo systemctl restart voicebridge` |
| 内网穿透状态 | `sudo systemctl status natapp` |
| 内网穿透日志 | `journalctl -u natapp -f`；或 `tail -f /opt/voicebridge/logs/natapp/natapp.log` |
| 内网穿透停止/启动 | `sudo systemctl stop natapp` / `sudo systemctl start natapp` |
| 关闭开机自启 | `sudo systemctl disable voicebridge` / `sudo systemctl disable natapp` |

## 部署到其他机器时

修改 `voicebridge.service` 和 `natapp.service` 中：

- 所有 **`/opt/voicebridge`** → 该机器上的项目根目录  
- **`User=`、`Group=`** → 该机器上运行服务的系统用户名

然后重新执行上面的「一键安装」中的复制与 `daemon-reload`、`enable`、`start`。

详细说明与生产检查项见 **docs/部署/部署文档.md** 第十二、十三节。
