# Personal Web Tunnel

[简体中文](README.zh-CN.md)

Publish local Windows web applications through your own Windows server. Manage local ports in a browser, assign paths or subdomains, and coordinate mappings across computers.

This repository contains the client, server automation and coordination service. Provision a public server and domain, then install the third-party dependencies from their official distributions.

## Features

- A local browser interface for adding, editing, enabling and deleting mappings.
- Path mappings such as `https://demo.example.com/app/` and subdomain mappings such as `https://review.example.com/`.
- Server-side address reservations. If another updated client owns the same address, the local mapping is disabled and its reason is shown.
- Automatic coordination mode after the client authenticates the server's coordination service.
- HTTP, HTTPS and WebSocket forwarding, including an optional CA file for a local HTTPS application.
- Windows startup tasks and a temporary route to the server's public IPv4 address for proxy/TUN environments.

## Requirements

Use Windows x64 and PowerShell 5.1 or newer on the client and server. Administrative rights are required for scheduled tasks, firewall rules and the client route.

Provide a Windows server with a public IPv4 address and DNS records for a domain you control. The examples use `example.com` and `203.0.113.10`, which you must replace with your own values.

Install these dependencies separately.

| Dependency | Tested version | Installation source |
| --- | --- | --- |
| Python | 3.12.10, with `sqlite3` and `ssl`; source requires 3.10+ | [Python for Windows](https://www.python.org/downloads/windows/) |
| FRP | 0.71.0 on both ends | [Official FRP release](https://github.com/fatedier/frp/releases/tag/v0.71.0) |
| Caddy | 2.11.7 | [Official Caddy installation guide](https://caddyserver.com/docs/install) |

Place the executables on the machines where they are used.

```text
Server
  server/frps.exe
  server/reverse-proxy/caddy.exe

Client
  client/frpc.exe
  client/caddy.exe
```

Install Python so `python.exe` is available in PowerShell. An activated conda environment can also supply Python. Use the same FRP version on the server and clients. The version fields in your settings must match the installed executables.

## 1. Configure your deployment

Clone this repository on the server and client, then create a local configuration on each machine.

```powershell
git clone https://github.com/armihia/personal-web-tunnel.git
Set-Location personal-web-tunnel
Copy-Item settings.example.json settings.json
```

Edit the following fields in `settings.json` on both ends.

| Field | Example | Purpose |
| --- | --- | --- |
| `ServerAddress` | `example.com` | Base domain used for subdomains |
| `PublicDomain` | `demo.example.com` | Shared path-mapping and control endpoint |
| `DirectServerIP` | `203.0.113.10` | Your server's public IPv4 address |
| `FrpsBindPort` | `7000` | FRP control port |
| `SharedTestEnabled` | `true` | Enables the legacy-compatible `/test` split |

Add an A record for `demo` pointing to the server. For automatic subdomains, add an A record with host `*` pointing to the same address. Existing explicit DNS records can take precedence over the wildcard, so check records for names you plan to use.

Allow inbound TCP `80`, `443` and the configured FRP control port in your provider's security group and the Windows firewall.

The following services bind to loopback addresses.

| Port | Purpose |
| --- | --- |
| `2019` | Caddy administration |
| `18080` | Legacy-compatible `/test` tunnel |
| `18081` | Legacy ordinary tunnel and coordination reservation |
| `18083` | Coordination service |
| `30000–30255` | Per-mapping FRP proxy ports |
| `17890–17892` | Client manager and local router |

## 2. Install the server

Run the following in an administrator PowerShell on a server where ports `80` and `443` are available.

```powershell
.\server\install-server.ps1
```

The installer creates `server/token.txt` and Windows startup tasks. Transfer that file securely to `client/token.txt` on each trusted client computer.

If an HTTPS service already owns `80` or `443`, use the existing-service installation mode and merge the generated configuration into your Caddy configuration.

```powershell
.\server\install-server.ps1 -HttpsMode Existing
```

The generated Caddy fragment is under `server/reverse-proxy/generated/`. The coordination setup expects a separate `PublicDomain` site block, the `/test` split, and a loopback Caddy admin endpoint. It validates the detected process and configuration before applying changes.

## 3. Enable multi-computer coordination

Complete this step once on the server, before connecting ordinary clients. Python must be accessible to the SYSTEM startup task. Supply its absolute path explicitly when using a conda environment.

```powershell
$python = (Get-Command python.exe).Source
.\server\enable-collaboration.ps1 -PythonExe $python
```

Inspect the generated preview. Apply it when the legacy ordinary entry is idle.

```powershell
.\server\enable-collaboration.ps1 -PythonExe $python -Apply
.\server\status-collaboration.ps1
.\server\status-server.ps1
```

Application restarts FRPS, so active FRP connections briefly reconnect. The script backs up the FRPS and Caddy configuration and attempts recovery if application fails. The coordination service is registered as a SYSTEM startup task.

For an existing Caddy installation, supply `-CaddyExe` and `-CaddyConfig` when automatic detection cannot identify the active configuration. Complex imported configurations require manual integration.

## 4. Start a client

After installing the client dependencies and copying `client/token.txt`, start the local website and launch the manager.

```powershell
.\start-manager.ps1 -OpenBrowser
```

You can also double-click `启动管理器.cmd`. Open `http://127.0.0.1:17890/` if the browser does not open automatically.

Add a mapping with the local port and either a path or a subdomain prefix.

| Local application | Input | Public address |
| --- | --- | --- |
| `http://127.0.0.1:3000` | Path `/app` | `https://demo.example.com/app/` |
| `http://127.0.0.1:8765` | Subdomain `review` | `https://review.example.com/` |

The path field starts empty and uses `/` when both fields are empty. A subdomain uses its own root path. Root-relative resources such as `/static/app.js` work naturally with a subdomain. Applications with hard-coded localhost URLs or origin restrictions may need their own configuration.

The client checks address availability before enabling a mapping. Registration is also atomic, so two simultaneous requests for the same address cannot both succeed. Conflicting local records are saved as disabled. A server or network failure is shown as awaiting verification.

Stopping an application's local port releases its ordinary mapping. `/test` can use any local port and uses the legacy entry separately.

## Compatibility and lifecycle

Updated clients can publish different paths and subdomains concurrently. An older ordinary client shares one server entry and cannot disclose its internal mapping list. While that entry is occupied, updated clients wait for verification. Update those clients to use per-mapping coordination.

Use the manager's exit button or `一键停止穿透.cmd` to stop the local tunnel. Server processes use Windows scheduled tasks and can be checked after a reboot.

```powershell
.\server\status-server.ps1
.\server\status-collaboration.ps1
```

For a manual server restart after stopping its tasks, start the coordination task and the server entry points.

```powershell
Start-ScheduledTask -TaskName PersonalWebTunnel-Collaboration
.\server\start-server.ps1
```

The client maintains a temporary `/32` route to `DirectServerIP`. This requires administrative rights and a usable physical gateway. Use the actual public IP in that field when a proxy provides fake DNS addresses.

## Troubleshooting

Client diagnostics are under `client/manager-runtime/`. Server startup failures are recorded in `server/collaboration-registry.log`. Check the scheduled-task result when the coordination port does not become ready.

```powershell
Get-ScheduledTaskInfo -TaskName PersonalWebTunnel-Collaboration
Get-Content .\server\collaboration-registry.log -Tail 60
```

Windows cold startup can take longer than an interactive launch. Coordination setup waits up to at least 60 seconds for its Python listener.

Path mappings forward a URL prefix. For an application designed to run at `/`, use a subdomain or configure the application's public base path. A mapping refresh cannot rewrite application-defined URLs.

## Development and security

The Python application uses the standard library. After preparing `settings.json` and the test executables, run the focused tests on Windows.

```powershell
python tests/test_collaboration_registry.py
python tests/test_collaboration_client.py
python tests/test_collaboration_setup.py
```

Tests exercise reservations, conflict handling, HTTPS authentication, WebSockets and migration recovery. Real cloud reachability and certificate issuance depend on your deployment.

Treat `server/token.txt` and `client/token.txt` as credentials. The control service uses one-time HMAC challenges and signed responses, while FRP uses its configured TLS authentication. Only share a deployment token with trusted clients. A local web application becomes publicly accessible when its mapping is published, so apply the application's own authentication as needed.

## Licence

Project source is available under the [MIT licence](LICENSE). FRP, Caddy and Python are distributed under their own licences.
