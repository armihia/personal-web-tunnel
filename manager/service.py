"""管理本项目的路径路由、FRPC 与直连路由守护进程。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

from .model import CONFLICT_REASON, PENDING_REASON, MappingStore, atomic_write, caddy_config, validate_mapping, validate_mappings
from .collaboration import CollaborationClient, AddressConflict, LeaseExpired, LeasePending, RegistryUnavailable, machine_owner

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def quote_ps(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(code: str, *, timeout: int = 15) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); " + code],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-2000:])
    return result.stdout.strip()


class PublishLock:
    """与现有 PowerShell FileShare.None 发布锁共用同一 Windows 文件句柄。"""
    def __init__(self, path: Path):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise RuntimeError("另一个发布命令正在运行，请停止它后再连接。")

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Worker:
    def __init__(self, root: Path, runtime: Path, mode: str, settings: dict, parent_ticks: str, mapping: dict | None = None):
        self.mode = mode
        self.instance = mode.lower() + "-" + uuid.uuid4().hex
        self.ready = runtime / (self.instance + ".ready.json")
        self.stop_file = runtime / (self.instance + ".stop")
        self.output = runtime / (self.instance + ".worker.log")
        arguments = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(root / "manager/worker.ps1"), "-Mode", mode,
                     "-RuntimeDirectory", str(runtime), "-Instance", self.instance,
                     "-ParentPid", str(os.getpid()), "-ParentStartTicks", parent_ticks,
                     "-ServerIP", settings["DirectServerIP"],
                     "-RouterPort", str(settings["LocalRouterPort"]),
                     "-AdminPort", str(settings["LocalRouterAdminPort"])]
        if mode in {"Tunnel", "TestTunnel"}:
            shared = settings.get("SharedTestEnabled", False)
            main = mode == "Tunnel" and shared
            arguments += ["-RemotePort", str(settings["SharedMainPort"] if main else settings["RemotePort"]),
                          "-ProxyName", "personal-main-web" if main else "personal-temp-web"]
        elif mode == "MappingTunnel":
            if not mapping:
                raise ValueError("缺少协作映射参数。")
            arguments += ["-RemotePort", str(mapping["remote_port"]), "-ProxyName", mapping["proxy_name"],
                          "-LogStem", "mapping-" + mapping["id"]]
        with self.output.open("wb") as log:
            self.process = subprocess.Popen(arguments, stdout=log, stderr=log, cwd=root,
                                            creationflags=NO_WINDOW)

    def alive(self) -> bool:
        return self.process.poll() is None

    def state(self) -> dict:
        try:
            state = json.loads(self.ready.read_text("utf-8-sig"))
        except (OSError, ValueError):
            state = {"status": "starting"}
        if not self.alive():
            state["status"] = "error"
            state.setdefault("error", "工作进程已退出，正在重试。")
        return state

    def stop(self):
        self.stop_file.touch()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired as exc:
            # 不强杀守护进程，以便它完成子进程和路由清理。
            raise RuntimeError("停止仍在进行，请稍后再试；未强制结束清理进程。") from exc
        for path in (self.ready, self.stop_file):
            path.unlink(missing_ok=True)


class TunnelService:
    def __init__(self, root: Path, data: Path, settings: dict, *, local_only: bool = False):
        self.root, self.data, self.settings = root, data, settings
        self.local_only = local_only
        self.runtime = data / "manager-runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.store = MappingStore(data / "mappings.json", settings)
        self.mappings = self.store.load()
        self.lock = threading.RLock()
        self.workers: dict[str, Worker] = {}
        self.coordination_state_path = data / 'coordination-state.json'
        self.distributed = False
        if self.coordination_state_path.exists():
            remembered = json.loads(self.coordination_state_path.read_text('utf-8-sig'))
            self.distributed = (remembered.get('server') == settings['DirectServerIP'] and
                                remembered.get('domain') == settings['PublicDomain'])
            if self.distributed:
                settings['SharedTestEnabled'] = True
        self.capability_after = 0.0
        self.registry_retry_after = 0.0
        self.registry_error = ""
        self.coordinator = CollaborationClient(settings, root / "client/token.txt")
        self.owner = uuid.uuid4().hex
        self.device = None
        self.leases: dict[str, dict] = {}
        self.mapping_errors: dict[str, str] = {}
        self.retries: dict[str, float] = {}
        self.desired = not local_only
        self.error = ""
        self.publish_lock = None
        self.closed = threading.Event()
        self.parent_ticks = run_ps(f"(Get-Process -Id {os.getpid()}).StartTime.ToUniversalTime().Ticks.ToString()")
        self.config_path = self.runtime / "Caddyfile"
        atomic_write(self.config_path, caddy_config(self.mappings, settings, self.runtime))
        self.thread = threading.Thread(target=self._monitor, name="穿透进程管理", daemon=True)

    def start(self):
        self.thread.start()

    def _acquire_publish(self):
        if self.publish_lock or self.local_only:
            return
        # 只停止旧脚本记录的本项目 FRPC，留给旧直连脚本时间回收它的路由。
        if (self.root / "client/frpc.state.json").exists():
            run_ps("& " + quote_ps(self.root / "client/stop-publish.ps1") + "; if ($LASTEXITCODE) { exit $LASTEXITCODE }")
            time.sleep(2)
        self.publish_lock = PublishLock(self.root / "client/frpc.lock")
        try:
            # 多条映射并发启动前只规范一次令牌，避免多个工作进程同时截断和重写文件。
            run_ps(". " + quote_ps(self.root / "common/Tunnel.Common.ps1") + "; Assert-TunnelToken " +
                   quote_ps(self.root / "client/token.txt"))
        except Exception:
            self.publish_lock.close()
            self.publish_lock = None
            raise

    def _ensure_worker(self, mode: str):
        worker = self.workers.get(mode)
        if worker and worker.alive():
            return
        if worker:
            self.error = worker.state().get("error", "工作进程已退出。")
        if time.monotonic() < self.retries.get(mode, 0):
            return
        self.retries[mode] = time.monotonic() + 5
        if worker:
            worker.stop()
        logs = sorted(self.runtime.glob(mode.lower() + "-*.worker.log"), key=lambda path: path.stat().st_mtime)
        for path in logs[:-4]:
            path.unlink(missing_ok=True)
        self.workers[mode] = Worker(self.root, self.runtime, mode, self.settings, self.parent_ticks)

    def _stop_worker(self, mode: str):
        worker = self.workers.get(mode)
        if worker:
            worker.stop()
            del self.workers[mode]

    @staticmethod
    def _local_up(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return True
        except OSError:
            return False

    def _test_requested(self) -> bool:
        return bool(self.settings.get("SharedTestEnabled") and
                    any(item["enabled"] and not item.get("hostname") and item["path"].lower() == "/test"
                        and self._local_up(item["port"]) for item in self.mappings))

    def _lease_payload(self, item: dict) -> dict:
        if self.device is None:
            self.device = machine_owner(self.data)
        return {"hostname": item.get("hostname", self.settings["PublicDomain"]),
                "path": item["path"], "owner": self.owner, "device": self.device, "mapping_id": item["id"]}

    def _release_mapping(self, mapping_id: str, *, notify: bool = True):
        key = "Mapping:" + mapping_id
        self._stop_worker(key)
        lease = self.leases.pop(mapping_id, None)
        if lease and notify and time.monotonic() >= self.registry_retry_after:
            try:
                self.coordinator.request('lease/release', lease['payload'])
            except (AddressConflict, LeaseExpired, LeasePending):
                # 登记服务恢复后会按租约期限释放。
                pass
            except (RegistryUnavailable, OSError):
                self.registry_retry_after = time.monotonic() + 5

    def _disable_conflict(self, mapping_id: str, reason: str = CONFLICT_REASON):
        self._release_mapping(mapping_id)
        self.mapping_errors.pop(mapping_id, None)
        mappings = [dict(item) for item in self.mappings]
        for item in mappings:
            if item['id'] == mapping_id:
                item['enabled'] = False
                item['disabled_reason'] = reason
        self.replace_mappings(mappings)

    def _reconcile_mappings(self):
        desired = {item['id']: item for item in self.mappings if item['enabled'] and
                   (item.get('hostname') or item['path'].lower() != '/test') and self._local_up(item['port'])}
        for mapping_id in set(self.mapping_errors) - set(desired):
            self.mapping_errors.pop(mapping_id, None)
        for mapping_id in set(self.leases) - set(desired):
            self._release_mapping(mapping_id)
        if time.monotonic() < self.registry_retry_after:
            for mapping_id, lease in list(self.leases.items()):
                if time.monotonic() - lease['heartbeat'] >= lease['ttl']:
                    self._release_mapping(mapping_id, notify=False)
            return
        for mapping_id, item in desired.items():
            now = time.monotonic()
            if now < self.retries.get('Mapping:' + mapping_id, 0):
                continue
            lease = self.leases.get(mapping_id)
            try:
                if lease and (lease['payload']['hostname'] != item.get('hostname', self.settings['PublicDomain']) or
                              lease['payload']['path'] != item['path']):
                    self._release_mapping(mapping_id)
                    lease = None
                if not lease:
                    payload = self._lease_payload(item)
                    result = self.coordinator.request('lease/acquire', payload)
                    port, ttl = result.get('port'), result.get('ttl')
                    if type(port) is not int or not 30000 <= port <= 30255 or type(ttl) is not int or not 10 <= ttl <= 300:
                        raise RegistryUnavailable('服务器返回的协作端口或期限不正确。')
                    lease = self.leases[mapping_id] = {'payload': payload, 'port': port, 'ttl': ttl,
                                                         'heartbeat': now, 'active': bool(result.get('active')),
                                                         'last_connected': now}
                key = 'Mapping:' + mapping_id
                worker = self.workers.get(key)
                if worker and not worker.alive():
                    failure = worker.state().get('error', '映射工作进程已退出，正在重试。')
                    self._release_mapping(mapping_id)
                    self.mapping_errors[mapping_id] = failure
                    self.retries[key] = now + 5
                    continue
                if not worker:
                    active_logs = {getattr(current, 'output', None) for current in self.workers.values() if current.alive()}
                    old_logs = sorted(self.runtime.glob('mappingtunnel-*.worker.log'), key=lambda path: path.stat().st_mtime)
                    for path in old_logs[:-8]:
                        if path not in active_logs:
                            try:
                                path.unlink(missing_ok=True)
                            except OSError:
                                pass
                    self.workers[key] = Worker(self.root, self.runtime, 'MappingTunnel', self.settings,
                                               self.parent_ticks, {'id': mapping_id, 'remote_port': lease['port'],
                                                                   'proxy_name': 'personal-map-' + self.owner + '-' + mapping_id})
                states = {key: self.workers[key].state()}
                connected, failure = self._tunnel_status(states, key, 'mapping-' + mapping_id + '.log')
                if connected:
                    lease['last_connected'] = now
                    if not lease['active']:
                        self.coordinator.request('lease/activate', lease['payload'])
                        lease['active'] = True
                elif lease['active'] and now - lease['last_connected'] > 5:
                    self._release_mapping(mapping_id)
                    self.retries[key] = now + 5
                    continue
                elif not lease['active'] and now - lease['last_connected'] > 20:
                    self._release_mapping(mapping_id)
                    self.retries[key] = now + 5
                    continue
                if now - lease['heartbeat'] >= min(12, lease['ttl'] / 3):
                    self.coordinator.request('lease/heartbeat', lease['payload'])
                    lease['heartbeat'] = now
                self.registry_error = ''
                self.registry_retry_after = 0
                if not connected and failure:
                    self.mapping_errors[mapping_id] = '映射隧道尚未连接：' + failure
                else:
                    self.mapping_errors.pop(mapping_id, None)
            except AddressConflict:
                self._disable_conflict(mapping_id)
            except LeaseExpired as exc:
                self._release_mapping(mapping_id)
                self.mapping_errors[mapping_id] = str(exc)
                self.retries['Mapping:' + mapping_id] = now + 1
            except LeasePending as exc:
                self.mapping_errors[mapping_id] = str(exc)
                if lease and now - lease['heartbeat'] >= lease['ttl']:
                    self._release_mapping(mapping_id)
                self.retries['Mapping:' + mapping_id] = now + 5
            except RegistryUnavailable as exc:
                retry_after = time.monotonic() + 5
                self.registry_retry_after = retry_after
                for pending_id in desired:
                    self.mapping_errors[pending_id] = str(exc)
                    self.retries['Mapping:' + pending_id] = retry_after
                for pending_id, pending_lease in list(self.leases.items()):
                    if time.monotonic() - pending_lease['heartbeat'] >= pending_lease['ttl']:
                        self._release_mapping(pending_id, notify=False)
                return
            except (OSError, ValueError) as exc:
                self.mapping_errors[mapping_id] = str(exc)
                if lease and now - lease['heartbeat'] >= lease['ttl']:
                    self._release_mapping(mapping_id)
                self.retries['Mapping:' + mapping_id] = now + 5

    def _reconcile(self):
        """普通入口持续运行；/test 只在本机实际使用时申请原来的入口。"""
        self._ensure_worker("Router")
        router = self.workers.get("Router")
        if router and router.state()["status"] == "ready":
            self.error = ""
        if self.desired and not self.local_only:
            self._acquire_publish()
            self._ensure_worker("Route")
            route = self.workers.get("Route")
            if route and router and route.state()["status"] == router.state()["status"] == "ready":
                test_requested = self._test_requested()
                # 先释放不再使用的入口，之后才维护普通映射。
                if not test_requested:
                    self._stop_worker("TestTunnel")
                if not self.distributed and time.monotonic() >= self.capability_after:
                    self.capability_after = time.monotonic() + 15
                    try:
                        self.coordinator.request('health')
                        if not self.settings.get('SharedTestEnabled'):
                            # 仅在确认服务器已部署协作入口后自动启用，避免旧服务器切换错误。
                            self.settings['SharedTestEnabled'] = True
                            try:
                                self.replace_mappings(self.mappings)
                            except Exception:
                                self.settings['SharedTestEnabled'] = False
                                raise
                        atomic_write(self.coordination_state_path, json.dumps({'server': self.settings['DirectServerIP'],
                                                                                'domain': self.settings['PublicDomain']}))
                        self.distributed = True
                        self.registry_error = ''
                    except (RegistryUnavailable, OSError):
                        self.registry_error = '尚未确认服务器已启用逐条协作；当前不能检查其他电脑的普通映射。'
                if self.distributed:
                    self._stop_worker("Tunnel")
                    self._reconcile_mappings()
                else:
                    self._ensure_worker("Tunnel")
                if test_requested:
                    self._ensure_worker("TestTunnel")
                    worker = self.workers.get("TestTunnel")
                    states = {"TestTunnel": worker.state()} if worker else {}
                    connected, failure = self._tunnel_status(states, "TestTunnel", "frpc-test.log")
                    if not connected and ("port already used" in failure or "already exists" in failure):
                        self._stop_worker("TestTunnel")
                        for item in self.mappings:
                            if item['enabled'] and not item.get('hostname') and item['path'].lower() == '/test':
                                self._disable_conflict(item['id'])
                                break
            else:
                self._stop_worker("TestTunnel")
                self._stop_worker("Tunnel")
                for mapping_id in list(self.leases):
                    self._release_mapping(mapping_id)
        else:
            self._stop_worker("TestTunnel")
            self._stop_worker("Tunnel")
            for mapping_id in list(self.leases):
                self._release_mapping(mapping_id)
            self._stop_worker("Route")

    def _monitor(self):
        while not self.closed.is_set():
            try:
                with self.lock:
                    self._reconcile()
            except Exception as exc:
                with self.lock:
                    self.error = str(exc)
            self.closed.wait(1)

    def set_connected(self, value: bool):
        if self.local_only and value:
            raise ValueError("当前为本地预览；请双击启动管理器以连接公网隧道。")
        with self.lock:
            self.desired = value
            self.error = ""

    def _adapt(self, path: Path) -> bytes:
        result = subprocess.run(
            [str(self.root / "client/caddy.exe"), "adapt", "--validate", "--config", str(path), "--adapter", "caddyfile"],
            capture_output=True, timeout=10, creationflags=NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace")[-2000:])
        json.loads(result.stdout)
        return result.stdout

    def _reload(self, config: bytes, *, force: bool = False):
        address = f"http://127.0.0.1:{self.settings['LocalRouterAdminPort']}"
        headers = {"Content-Type": "application/json", "Origin": address}
        if force:
            # 相同配置也重新加载，释放旧的代理连接；使用 Caddy 官方的强制重载选项。
            headers["Cache-Control"] = "must-revalidate"
        request = urllib.request.Request(address + "/load", data=config, headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=8) as response:
            response.read()

    def replace_mappings(self, mappings: list, *, force_reload: bool = False):
        mappings = validate_mappings(mappings, self.settings)
        with self.lock:
            proposed = {item['id']: item for item in mappings if item['enabled']}
            for mapping_id, lease in list(self.leases.items()):
                item = proposed.get(mapping_id)
                if not item or item['path'] != lease['payload']['path'] or item.get('hostname', self.settings['PublicDomain']) != lease['payload']['hostname']:
                    self._release_mapping(mapping_id)
            router = self.workers.get("Router")
            ready = router and router.state()["status"] == "ready"
            old_text = self.config_path.read_text("utf-8")
            try:
                atomic_write(self.config_path, caddy_config(mappings, self.settings, self.runtime))
                # 即使路由进程离线，也先让官方 Caddy 验证配置，避免保存不可用路由。
                config = self._adapt(self.config_path)
                if ready:
                    self._reload(config, force=force_reload)
                self.store.save(mappings)
                self.mappings = mappings
            except Exception:
                atomic_write(self.config_path, old_text)
                if ready:
                    self._reload(self._adapt(self.config_path))
                raise

    def refresh_mappings(self):
        with self.lock:
            router = self.workers.get("Router")
            if not router or router.state()["status"] != "ready":
                raise RuntimeError("本机路由尚未就绪，请等待启动完成后再刷新。")
            self.replace_mappings(self.store.load(), force_reload=True)

    def mutate(self, method: str, value: dict | None = None, mapping_id: str | None = None):
        with self.lock:
            mappings = [dict(item) for item in self.mappings]
            previous = None
            if method == "POST":
                mappings.append(validate_mapping(value, self.settings))
                index = len(mappings) - 1
            else:
                indices = [i for i, item in enumerate(mappings) if item["id"] == mapping_id]
                if not indices:
                    raise KeyError("映射不存在。")
                index = indices[0]
                previous = dict(mappings[index])
                if method == "DELETE":
                    mappings.pop(index)
                else:
                    mappings[index] = validate_mapping(value, self.settings, mapping_id=mapping_id)
            if method != 'DELETE':
                candidate = mappings[index]
                canonical = (candidate.get('hostname', '').lower(), candidate['path'].lower())
                if any(other['id'] != candidate['id'] and
                       (other.get('hostname', '').lower(), other['path'].lower()) == canonical for other in mappings):
                    raise ValueError('该公网地址已在本机配置，路径不区分大小写。')
            if method != 'DELETE' and self.distributed and not self.local_only:
                item = mappings[index]
                changed_address = (not previous or not previous['enabled'] or
                                   previous['path'] != item['path'] or
                                   previous.get('hostname') != item.get('hostname'))
                if item['enabled'] and changed_address:
                    try:
                        test_worker = self.workers.get('TestTunnel')
                        own_test = bool(not item.get('hostname') and item['path'].lower() == '/test' and test_worker and
                                        self._tunnel_status({'TestTunnel': test_worker.state()}, 'TestTunnel', 'frpc-test.log')[0])
                        check = {'occupied': False} if own_test else self.coordinator.request('lease/check', self._lease_payload(item))
                        if type(check.get('occupied')) is not bool:
                            raise RegistryUnavailable('服务器没有返回有效的占用状态。')
                        if check.get('pending'):
                            item['enabled'] = False
                            item['disabled_reason'] = PENDING_REASON
                        elif check['occupied']:
                            item['enabled'] = False
                            item['disabled_reason'] = CONFLICT_REASON
                    except AddressConflict:
                        item['enabled'] = False
                        item['disabled_reason'] = CONFLICT_REASON
                    except (RegistryUnavailable, OSError, ValueError):
                        item['enabled'] = False
                        item['disabled_reason'] = '无法核对服务器，请稍后重新启用'
            self.replace_mappings(mappings)

    def _tunnel_status(self, states: dict, mode: str, logfile: str) -> tuple[bool, str]:
        if states.get(mode, {}).get("status") != "ready":
            return False, states.get(mode, {}).get("error", "")
        try:
            with (self.runtime / logfile).open("rb") as stream:
                stream.seek(max(0, stream.seek(0, 2) - 65536))
                log = stream.read().decode("utf-8", "replace")
        except OSError:
            return False, ""
        events = list(re.finditer(r"start proxy success|try to connect to server|connect to server error|control writer is closing|start error:", log))
        connected = bool(events and events[-1].group() == "start proxy success")
        failure = ''
        if events and events[-1].group() in {'connect to server error:', 'start error:'}:
            event = events[-1]
            start = log.rfind('\n', 0, event.start()) + 1
            end = log.find('\n', event.end())
            failure = log[start:end if end >= 0 else len(log)][-600:]
        return connected, failure

    def status(self) -> dict:
        with self.lock:
            states = {name: worker.state() for name, worker in self.workers.items()}
            connected, tunnel_error = self._tunnel_status(states, "Tunnel", "frpc.log")
            if self.distributed:
                connected = any(lease['active'] and not self.mapping_errors.get(key) and self._tunnel_status(states, 'Mapping:' + key, 'mapping-' + key + '.log')[0]
                                for key, lease in self.leases.items())
            test_connected, test_error = self._tunnel_status(states, "TestTunnel", "frpc-test.log")
            shared = self.settings.get("SharedTestEnabled", False)
            test_desired = self.desired and not self.local_only and self._test_requested()
            if not test_desired:
                test_connected, test_error = False, ""
            if self.distributed and test_connected:
                connected = True
            if not self.desired:
                connected = False
            test_message = ""
            if shared:
                if test_connected:
                    test_message = "/test 正由本机发布。停止对应本地端口或停用映射后会自动释放。"
                elif any(item.get('disabled_reason') in {CONFLICT_REASON, '地址已由其他电脑使用'} for item in self.mappings if item['path'].lower() == '/test' and not item.get('hostname')):
                    test_message = "/test 的兼容入口已被占用，已停用本机映射。"
                elif not test_desired:
                    test_message = "/test 本机未申请；不影响对方使用原来的采集穿透代码。"
                elif "port already used" in test_error or "already exists" in test_error:
                    test_message = "/test 入口已被其他客户端占用；你的其他映射继续运行。"
                else:
                    test_message = "/test 正在申请入口；你的其他映射独立运行。"
                if not self.distributed and not self.local_only:
                    test_message += " 当前为旧版协作方式，其他路径尚不支持逐条冲突检查。"
            items = [dict(item) for item in self.mappings]
            for item in items:
                item["url"] = "https://" + item.get("hostname", self.settings["PublicDomain"]) + (item["path"].rstrip("/") + "/")
                item["local_up"] = self._local_up(item["port"])
                item['coordination_error'] = self.mapping_errors.get(item['id'], '')
                mapping_connected = (bool(self.leases.get(item['id'], {}).get('active')) and
                                     not item['coordination_error'] and self._tunnel_status(states, 'Mapping:' + item['id'], 'mapping-' + item['id'] + '.log')[0])
                item["published"] = bool(item["enabled"] and item["local_up"] and
                                         (test_connected if shared and item["path"].lower() == "/test" and not item.get('hostname')
                                          else mapping_connected if self.distributed else connected))
            errors = [state["error"] for mode, state in states.items() if mode != "TestTunnel" and state.get("error")]
            return {"domain": self.settings["PublicDomain"], "domain_suffix": self.settings["ServerAddress"], "server": self.settings["DirectServerIP"], "subdomain_supported": True, "automatic_subdomains_supported": True,
                    "connected": connected, "desired": self.desired, "local_only": self.local_only,
                    "router_ready": states.get("Router", {}).get("status") == "ready",
                    "route": states.get("Route", {}), "error": next((item['disabled_reason'] + '：' + item['url'] for item in items if item.get('disabled_reason')), '') or next((item['coordination_error'] + '：' + item['url'] for item in items if item['coordination_error']), '') or (errors[0] if errors else self.registry_error or tunnel_error or self.error),
                    "distributed": self.distributed,
                    "shared_test": {"enabled": shared, "desired": test_desired, "connected": test_connected,
                                    "message": test_message, "error": test_error}, "mappings": items}

    def close(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=20)
        with self.lock:
            for mapping_id in list(self.leases):
                self._release_mapping(mapping_id)
            for mode in ("TestTunnel", "Tunnel", "Route", "Router"):
                self._stop_worker(mode)
            if self.publish_lock:
                self.publish_lock.close()
                self.publish_lock = None
