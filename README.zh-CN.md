# Personal Web Tunnel

[English](README.md)

通过自备的 Windows 公网服务器发布本地网站，在浏览器里管理端口映射、访问路径和独立子域名，并协调多台电脑的地址占用。

仓库包含客户端、服务端自动化脚本和协作登记服务。部署时需要自行准备服务器和域名，并从官方渠道安装第三方依赖。

## 功能

- 在本机浏览器中添加、编辑、启用和删除映射。
- 使用 `https://demo.example.com/app/` 路径或 `https://review.example.com/` 子域名访问网站。
- 由服务器原子登记地址。其他新版客户端占用同一地址时，本机映射自动停用并显示原因。
- 验证服务器身份后自动启用逐映射协作模式。
- 转发 HTTP、HTTPS 和 WebSocket。HTTPS 本地应用可以指定自己的 CA 文件。
- 通过 Windows 计划任务开机启动，并为代理或 TUN 环境维护到服务器公网 IPv4 的临时直连路由。

## 准备环境

客户端和服务器使用 Windows x64、PowerShell 5.1 或更新版本。计划任务、防火墙和临时路由操作需要管理员权限。

自行租用或准备具有公网 IPv4 的 Windows 服务器，并准备可以修改 DNS 的域名。下文的 `example.com` 和 `203.0.113.10` 是占位值，配置时替换成自己的地址。

第三方依赖分别安装。

| 依赖 | 测试版本 | 官方来源 |
| --- | --- | --- |
| Python | 3.12.10，含 `sqlite3` 和 `ssl`，源码要求 3.10+ | [Windows Python 下载](https://www.python.org/downloads/windows/) |
| FRP | 两端均使用 0.71.0 | [FRP 官方发行版](https://github.com/fatedier/frp/releases/tag/v0.71.0) |
| Caddy | 2.11.7 | [Caddy 官方安装说明](https://caddyserver.com/docs/install) |

按使用位置放置可执行文件。

```text
服务器
  server/frps.exe
  server/reverse-proxy/caddy.exe

客户端
  client/frpc.exe
  client/caddy.exe
```

安装 Python 后，在 PowerShell 中确认 `python.exe` 可运行。也可以激活已有的 conda 环境。服务端和客户端使用相同的 FRP 版本，配置文件中的版本号应与本机可执行文件对应。

## 一、填写部署配置

在服务器和客户端分别克隆仓库并创建本地配置。

```powershell
git clone https://github.com/armihia/personal-web-tunnel.git
Set-Location personal-web-tunnel
Copy-Item settings.example.json settings.json
```

编辑两端 `settings.json` 中的部署信息。

| 字段 | 示例 | 用途 |
| --- | --- | --- |
| `ServerAddress` | `example.com` | 用于拼接独立子域名的根域名 |
| `PublicDomain` | `demo.example.com` | 路径映射和控制接口使用的公网域名 |
| `DirectServerIP` | `203.0.113.10` | 自备服务器的公网 IPv4 |
| `FrpsBindPort` | `7000` | FRP 控制连接端口 |
| `SharedTestEnabled` | `true` | 启用 `/test` 兼容分流 |

为 `demo` 添加指向服务器的 A 记录。需要自动子域名时，再添加主机记录为 `*`、值为同一服务器 IP 的 A 记录。已有的精确域名记录可能优先于泛解析，需要核对准备使用的名称。

在云安全组和 Windows 防火墙开放入站 TCP `80`、`443` 和配置的 FRP 控制端口。

下列服务在回环地址监听。

| 端口 | 用途 |
| --- | --- |
| `2019` | Caddy 管理接口 |
| `18080` | `/test` 兼容隧道 |
| `18081` | 旧版普通隧道和协作互斥占用 |
| `18083` | 协作登记服务 |
| `30000–30255` | 各条映射的 FRP 代理端口 |
| `17890–17892` | 客户端管理界面与本地路由 |

## 二、安装服务器

在 `80`、`443` 可用的服务器上，以管理员 PowerShell 执行。

```powershell
.\server\install-server.ps1
```

安装器会创建 `server/token.txt` 和开机计划任务。通过安全方式将该文件复制到每台可信客户端的 `client/token.txt`。

如果服务器已有 HTTPS 服务，使用 Existing 模式，并把生成的 Caddy 配置片段合并到自己的配置中。

```powershell
.\server\install-server.ps1 -HttpsMode Existing
```

生成目录为 `server/reverse-proxy/generated/`。协作安装要求 `PublicDomain` 使用独立站点块，包含 `/test` 分流，并提供回环地址的 Caddy 管理接口。应用前会检查运行进程和配置归属。

## 三、启用多电脑协作

在普通客户端连接前，于服务器完成一次协作设置。Python 需要能被 SYSTEM 开机任务访问，使用 conda 环境时建议明确传入可执行文件的绝对路径。

```powershell
$python = (Get-Command python.exe).Source
.\server\enable-collaboration.ps1 -PythonExe $python
```

查看生成的预览，在旧版普通入口空闲时应用。

```powershell
.\server\enable-collaboration.ps1 -PythonExe $python -Apply
.\server\status-collaboration.ps1
.\server\status-server.ps1
```

应用过程会重启 FRPS，正在运行的 FRP 连接会短暂重连。脚本会备份 FRPS 和 Caddy 配置，发生失败时尝试恢复。协作服务通过 SYSTEM 开机计划任务运行。

使用已有 Caddy 时，可以传入 `-CaddyExe` 和 `-CaddyConfig`。复杂的导入配置需要人工合并。

## 四、启动客户端

安装客户端依赖并准备好 `client/token.txt` 后，先启动本地网站，再启动管理器。

```powershell
.\start-manager.ps1 -OpenBrowser
```

也可以双击 `启动管理器.cmd`。浏览器没有自动打开时，访问 `http://127.0.0.1:17890/`。

填写本地端口，再选择路径或独立子域名前缀。

| 本地应用 | 填写内容 | 公网地址 |
| --- | --- | --- |
| `http://127.0.0.1:3000` | 路径 `/app` | `https://demo.example.com/app/` |
| `http://127.0.0.1:8765` | 子域名前缀 `review` | `https://review.example.com/` |

访问路径默认留空。路径和子域名都为空时使用 `/`，独立子域名使用自身的根路径。子域名适合使用 `/static/app.js` 等根路径资源的网站。应用写死 localhost 或配置了来源限制时，仍需调整应用本身的相应设置。

客户端在启用前检查地址占用，登记操作也有原子互斥保护。两台电脑同时申请同一地址时，只有一台能成功。冲突的本机记录会保存为停用状态，网络或服务器异常则显示待核对。

停止本地应用端口后，普通映射会释放。`/test` 可以映射任意本地端口，并使用单独的兼容入口。

## 兼容性与启停

新版客户端可以同时发布不同路径和子域名。旧版普通客户端共用一个服务器入口，无法提供内部映射列表。该入口被占用时，新版会等待核对。将对应客户端更新到新版后，即可使用逐条登记。

可以点击管理器的退出按钮，或运行 `一键停止穿透.cmd` 停止本机隧道。服务器通过 Windows 计划任务开机启动，重启后可执行状态检查。

```powershell
.\server\status-server.ps1
.\server\status-collaboration.ps1
```

手动停止任务后，再次启动服务器可以执行。

```powershell
Start-ScheduledTask -TaskName PersonalWebTunnel-Collaboration
.\server\start-server.ps1
```

客户端为 `DirectServerIP` 维护临时 `/32` 路由，需要管理员权限和可用物理网关。代理使用 Fake-IP DNS 时，该配置仍应填写服务器真实公网 IPv4。

## 排错

客户端日志位于 `client/manager-runtime/`。协作进程启动失败时，查看 `server/collaboration-registry.log` 和计划任务结果。

```powershell
Get-ScheduledTaskInfo -TaskName PersonalWebTunnel-Collaboration
Get-Content .\server\collaboration-registry.log -Tail 60
```

Windows 首次后台启动可能比交互启动更慢，协作安装会为 Python 监听等待至少 60 秒。

路径映射负责转发前缀。为按根路径设计的应用选择子域名，或配置应用的公共 base 路径。刷新映射无法改写应用自己生成的地址。

## 开发与安全

Python 应用使用标准库。准备好 `settings.json` 和测试所需的可执行文件后，可在 Windows 运行以下测试。

```powershell
python tests/test_collaboration_registry.py
python tests/test_collaboration_client.py
python tests/test_collaboration_setup.py
```

测试覆盖地址登记、冲突处理、HTTPS 认证、WebSocket 和迁移恢复。实际公网可达性与证书签发需要结合自己的部署验证。

`server/token.txt` 和 `client/token.txt` 属于凭据，仅分享给可信客户端。控制接口使用一次性 HMAC 挑战和响应认证，FRP 使用配置的 TLS 认证。映射发布后，本地应用可通过公网访问，需要按应用用途配置访问认证。

## 许可证

本项目源码采用 [MIT 许可证](LICENSE)。FRP、Caddy 和 Python 各自遵循其许可证。
