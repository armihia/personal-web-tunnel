"""验证两台客户端的原子争用、独立映射和安全释放。"""
from pathlib import Path
import json
import tempfile
import socket
import socketserver
import select
import http.server
import http.client
import ssl
import subprocess
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import parse_qs, urlsplit
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from manager.collaboration import AddressConflict, LeaseExpired, LeasePending, RegistryUnavailable, CollaborationClient, DirectHTTPS, machine_owner
from manager.model import CONFLICT_REASON, load_settings, validate_mapping
from manager.service import TunnelService
from server.collaboration_registry import Conflict, LeaseExpired as ServerLeaseExpired, LeasePending as ServerLeasePending, Registry, Server
from http_fixture import Website as WebSocketWebsite
from common.collaboration_auth import issue_ticket, sign


class Worker:
    def __init__(self, root, runtime, mode, settings, parent_ticks, mapping=None):
        self.mode = mode
        self.running = True
        self.listener = None
        if mode == 'MappingTunnel':
            self.listener = socket.socket()
            self.listener.bind(('127.0.0.1', mapping['remote_port']))
            self.listener.listen()
            (runtime / ('mapping-' + mapping['id'] + '.log')).write_text('start proxy success', 'utf-8')
    def alive(self): return self.running
    def state(self): return {'status': 'ready'}
    def stop(self):
        self.running = False
        if self.listener: self.listener.close()


class Coordinator:
    def __init__(self, registry): self.registry, self.offline = registry, False
    def request(self, action, payload=None):
        if self.offline: raise RegistryUnavailable('测试网络不可用')
        try:
            if action == 'health': return {'ok': True}
            if action == 'lease/check': return self.registry.check(payload)
            if action == 'lease/acquire': return self.registry.acquire(payload)
            self.registry.update(payload, action.rsplit('/', 1)[-1]); return {'ok': True}
        except Conflict as exc: raise AddressConflict(str(exc)) from exc
        except ServerLeaseExpired as exc: raise LeaseExpired(str(exc)) from exc
        except ServerLeasePending as exc: raise LeasePending(str(exc)) from exc


class ClientTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / 'tests/.work'; work.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); legacy_port = probe.getsockname()[1]
        self.registry = Registry(self.path/'leases.db', base_domain='example.com', public_domain='demo.example.com', first_port=30000, last_port=30100, legacy_port=legacy_port)
        self.addCleanup(self.registry.close)
        self.settings = load_settings(ROOT)
        self.settings['SharedTestEnabled'] = False
        self.worker_patch = patch('manager.service.Worker', Worker); self.worker_patch.start(); self.addCleanup(self.worker_patch.stop)
        self.run_patch = patch('manager.service.run_ps', return_value='1'); self.run_patch.start(); self.addCleanup(self.run_patch.stop)
        self.owner_patch = patch('manager.service.machine_owner', side_effect=['a'*32, 'b'*32]); self.owner_patch.start(); self.addCleanup(self.owner_patch.stop)
        self.services = []
        for name in ('a', 'b'):
            service = TunnelService(ROOT, self.path/name, dict(self.settings))
            service.coordinator = Coordinator(self.registry)
            service._acquire_publish = lambda: None
            service._local_up = lambda port: True
            service._adapt = lambda path: b'{}'
            service._reload = lambda value, **kw: None
            self.services.append(service)
        self.addCleanup(lambda: [service.close() for service in self.services])

    def test_conflicting_root_disabled_and_different_paths_coexist(self):
        a, b = self.services
        a._reconcile()
        self.assertTrue(a.distributed)
        self.assertTrue(a.settings['SharedTestEnabled'])
        self.assertTrue(a.status()['mappings'][0]['published'])
        b._reconcile()
        self.assertFalse(b.mappings[0]['enabled'])
        self.assertEqual(b.mappings[0]['disabled_reason'], CONFLICT_REASON)
        self.assertFalse(b.store.load()[0]['enabled'])
        b.mutate('PUT', {**b.mappings[0], 'enabled': True}, 'home')
        self.assertFalse(b.mappings[0]['enabled'])
        self.assertEqual(b.mappings[0]['disabled_reason'], CONFLICT_REASON)
        b.mappings.append(validate_mapping({'name':'独立路径', 'port':4567, 'path':'/other'}, b.settings, mapping_id='other'))
        b._reconcile()
        self.assertTrue(b.status()['mappings'][1]['published'])
        self.assertNotEqual(a.leases['home']['port'], b.leases['other']['port'])
        b._local_up = lambda port: False
        b._reconcile()
        self.assertNotIn('other', b.leases)
        self.assertTrue(a.status()['mappings'][0]['published'])

    def test_network_outage_does_not_disable_mapping(self):
        a = self.services[0]
        a._reconcile()
        a.coordinator.offline = True
        a.leases['home']['heartbeat'] = 0
        a._reconcile()
        self.assertTrue(a.mappings[0]['enabled'])
        self.assertNotIn('disabled_reason', a.mappings[0])

    def test_server_outage_backs_off_all_mapping_requests(self):
        service=self.services[0]
        service.mappings.append(validate_mapping({'name':'第二条','port':4321,'path':'/another'},service.settings,mapping_id='another'))
        service._reconcile()
        for lease in service.leases.values():
            lease['heartbeat']=0
        with patch.object(service.coordinator,'request',side_effect=RegistryUnavailable('服务器离线')) as request:
            service._reconcile()
            self.assertEqual(request.call_count,1)
            service._reconcile()
            self.assertEqual(request.call_count,1)
        self.assertFalse(service.leases)
        self.assertTrue(all(item['enabled'] for item in service.mappings))

    def test_expired_lease_is_retried_without_false_conflict(self):
        a = self.services[0]
        a._reconcile()
        with self.registry.connect() as db:
            db.execute('UPDATE leases SET expires=0')
        a.leases['home']['heartbeat'] = 0
        a._reconcile()
        self.assertTrue(a.mappings[0]['enabled'])
        self.assertNotIn('home', a.leases)

    def test_restart_remembers_collaboration_without_legacy_fallback(self):
        a = self.services[0]
        a._reconcile(); a.close()
        again = TunnelService(ROOT, self.path/'a', dict(self.settings))
        self.addCleanup(again.close)
        again.coordinator = Coordinator(self.registry); again.coordinator.offline = True
        again._acquire_publish = lambda: None
        again._local_up = lambda port: True
        self.assertTrue(again.distributed)
        self.assertTrue(again.settings['SharedTestEnabled'])
        again._reconcile()
        self.assertNotIn('Tunnel', again.workers)
        self.assertTrue(again.mappings[0]['enabled'])

    def test_legacy_test_port_is_checked_even_when_local_port_is_offline(self):
        a = self.services[0]
        a._reconcile()
        with socket.socket() as legacy:
            legacy.bind(('127.0.0.1', 0)); legacy.listen()
            self.registry.test_port = legacy.getsockname()[1]
            a._local_up = lambda port: False
            a.mutate('POST', {'name': '测试', 'port': 8765, 'path': '/test'})
        item = next(item for item in a.mappings if item['path'] == '/test')
        self.assertFalse(item['enabled'])
        self.assertEqual(item['disabled_reason'], CONFLICT_REASON)

    def test_check_unavailable_saves_disabled_mapping_with_clear_reason(self):
        a = self.services[0]
        a._reconcile(); a.coordinator.offline = True
        a.mutate('POST', {'name': '待核对', 'port': 8765, 'path': '/new'})
        item = next(item for item in a.store.load() if item['path'] == '/new')
        self.assertFalse(item['enabled'])
        self.assertEqual(item['disabled_reason'], '无法核对服务器，请稍后重新启用')

    def test_two_running_copies_on_one_computer_do_not_release_each_other(self):
        a,b=self.services
        with patch('manager.service.machine_owner', return_value='a'*32):
            a._reconcile(); b._reconcile()
            self.assertTrue(a.status()['mappings'][0]['published'])
            self.assertTrue(b.mappings[0]['enabled'])
            self.assertTrue(b.status()['mappings'][0]['coordination_error'])
            self.assertTrue(a.status()['mappings'][0]['published'])
            a.close()
            b.retries['Mapping:home']=0
            b._reconcile()
            self.assertTrue(b.status()['mappings'][0]['published'])

    def test_legacy_client_is_preserved_and_new_mapping_is_not_published(self):
        a = self.services[0]
        with socket.socket() as legacy:
            legacy.bind(('127.0.0.1', self.registry.legacy_port)); legacy.listen()
            a._reconcile()
            self.assertTrue(a.mappings[0]['enabled'])
            self.assertIn('无法核对', a.status()['mappings'][0]['coordination_error'])
            self.assertFalse(a.status()['mappings'][0]['published'])
            self.assertEqual(self.registry.lookup('demo.example.com', '/'), self.registry.legacy_port)

    def test_enable_checks_case_insensitive_address_before_local_port_starts(self):
        a, b = self.services
        a.mappings.append(validate_mapping({'name':'路径', 'port':1234, 'path':'/Web'}, a.settings, mapping_id='web'))
        a._reconcile(); b._reconcile()
        b._local_up = lambda port: False
        b.mutate('POST', {'name':'同一路径', 'port':8765, 'path':'/web'})
        item = next(item for item in b.store.load() if item['path'] == '/web')
        self.assertFalse(item['enabled'])
        self.assertEqual(item['disabled_reason'], CONFLICT_REASON)

    def test_client_distinguishes_conflict_expiry_and_unavailable(self):
        token = self.path/'token'; token.write_text('a'*64, encoding='utf-8')
        seen = []
        class Reply:
            def __init__(self, status, body, proof=''): self.status,self.body,self.proof=status,body,proof
            def read(self, size): return self.body
            def getheader(self, key, default=''): return self.proof
        class Connection:
            status = 200
            def __init__(self, host, ip, timeout): seen.append((host, ip, timeout))
            def request(self, method, path, body=None, headers=None):
                seen.append((method, path, headers))
                if method == 'GET':
                    nonce=parse_qs(urlsplit(path).query)['nonce'][0]; ticket=issue_ticket('a'*64)
                    self.reply=Reply(200, json.dumps({'ok':True,'service':'personal-web-tunnel-coordination-v1',
                        'ticket':ticket,'proof':sign('a'*64,'health',nonce,ticket)}).encode())
                else:
                    raw=b'{"ok":true}'; ticket=headers['X-Personal-Tunnel-Ticket']
                    self.reply=Reply(self.status,raw,sign('a'*64,'response',ticket,str(self.status),raw))
            def getresponse(self): return self.reply
            def close(self): pass
        client = CollaborationClient(self.settings, token, connection_factory=Connection)
        self.assertTrue(client.request('health')['ok'])
        self.assertEqual(seen[0][:2], ('demo.example.com', self.settings['DirectServerIP']))
        for status, error in [(409,AddressConflict),(410,LeaseExpired),(423,LeasePending),(503,RegistryUnavailable)]:
            Connection.status=status
            with self.subTest(status=status), self.assertRaises(error):
                client.request('lease/acquire', {'name':'测试'})
        self.assertNotIn('Authorization', seen[-1][2])
        self.assertNotIn('a'*64, str(seen[-1][2]))

        class ForgedConflict(Connection):
            status = 409
            def getresponse(self):
                result=self.reply
                if result.status == 409: result.proof='0'*64
                return result
        forged=CollaborationClient(self.settings,token,connection_factory=ForgedConflict)
        with self.assertRaises(RegistryUnavailable):
            forged.request('lease/acquire', {'name':'测试'})

        class ForgedService(Connection):
            def getresponse(self):
                result=self.reply
                if result.status == 200:
                    value=json.loads(result.body); value['proof']='0'*64; result.body=json.dumps(value).encode()
                return result
        before=len([entry for entry in seen if entry[0]=='POST'])
        forged_service=CollaborationClient(self.settings,token,connection_factory=ForgedService)
        with self.assertRaises(RegistryUnavailable):
            forged_service.request('lease/acquire', {'name':'测试'})
        self.assertEqual(len([entry for entry in seen if entry[0]=='POST']),before)

    def test_machine_identity_is_separate_from_copied_project(self):
        with patch.dict('os.environ', {'LOCALAPPDATA': str(self.path/'pc-a')}):
            first = machine_owner(self.path/'copy-a')
            self.assertEqual(first, machine_owner(self.path/'copy-b'))
        with patch.dict('os.environ', {'LOCALAPPDATA': str(self.path/'pc-b')}):
            self.assertNotEqual(first, machine_owner(self.path/'copy-a'))

    def test_token_preparation_failure_releases_publish_lock(self):
        class Lock:
            closed=False
            def close(self): self.closed=True
        lock=Lock(); service=self.services[0]
        with patch('manager.service.PublishLock',return_value=lock), patch('manager.service.run_ps',side_effect=RuntimeError('令牌不可用')):
            with self.assertRaises(RuntimeError): TunnelService._acquire_publish(service)
        self.assertTrue(lock.closed)
        self.assertIsNone(service.publish_lock)

    def test_failed_mapping_worker_releases_lease_and_backs_off(self):
        service=self.services[0]
        service._reconcile()
        service.workers['Mapping:home'].running=False
        service._reconcile()
        self.assertNotIn('home',service.leases)
        self.assertNotIn('Mapping:home',service.workers)
        service._reconcile()
        self.assertNotIn('Mapping:home',service.workers)
        service.retries['Mapping:home']=0
        service._reconcile()
        self.assertTrue(service.status()['mappings'][0]['published'])


class EndToEndTests(unittest.TestCase):
    def test_real_https_client_validates_server_certificate_and_acquires(self):
        work=ROOT/'tests/.work'; work.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=work); self.addCleanup(temp.cleanup)
        directory=Path(temp.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); legacy_port=sock.getsockname()[1]
        if 30000 <= legacy_port <= 30255:
            self.skipTest('系统选择的隔离入口与端口范围重叠。')
        registry=Registry(directory/'registry.sqlite',base_domain='example.com',public_domain='demo.example.com',legacy_port=legacy_port)
        reg_server=Server(('127.0.0.1',0),registry,'a'*64)
        threading.Thread(target=reg_server.serve_forever,daemon=True).start()
        self.addCleanup(reg_server.server_close); self.addCleanup(reg_server.shutdown)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); https_port=sock.getsockname()[1]
        tls_data=(directory/'tls-data').as_posix(); config=directory/'https.Caddyfile'
        config.write_text('''{
 admin off
 skip_install_trust
 auto_https disable_redirects
 storage file_system {
  root "%s"
 }
}
demo.example.com:%s {
 bind 127.0.0.1
 tls internal
 handle /__personal_web_tunnel/registry/* {
  uri strip_prefix /__personal_web_tunnel/registry
  reverse_proxy 127.0.0.1:%s
 }
}
'''%(tls_data,https_port,reg_server.server_port),encoding='utf-8')
        caddy=subprocess.Popen([str(ROOT/'client/caddy.exe'),'run','--config',str(config),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            ca=directory/'tls-data/pki/authorities/local/root.crt'
            for attempt in range(120):
                if ca.exists():
                    try:
                        with socket.create_connection(('127.0.0.1',https_port),timeout=.2): break
                    except OSError: pass
                if caddy.poll() is not None or attempt==119: self.fail('隔离 HTTPS 服务未启动。')
                time.sleep(.05)
            context=ssl.create_default_context(cafile=str(ca))
            class TestDirectHTTPS(DirectHTTPS):
                def __init__(self,host,ip,timeout):
                    http.client.HTTPSConnection.__init__(self,host,https_port,timeout=timeout,context=context)
                    self.ip=ip
            token=directory/'token.txt'; token.write_text('a'*64,encoding='utf-8')
            settings=load_settings(ROOT); settings['DirectServerIP']='127.0.0.1'
            client=CollaborationClient(settings,token,connection_factory=TestDirectHTTPS)
            for attempt in range(60):
                try:
                    self.assertTrue(client.request('health')['ok'])
                    break
                except RegistryUnavailable:
                    if caddy.poll() is not None or attempt == 59:
                        raise
                    time.sleep(.1)
            with self.assertRaises(RegistryUnavailable):
                CollaborationClient({**settings, 'PublicDomain':'wrong.example.com'},token,connection_factory=TestDirectHTTPS).request('health')
            item={'hostname':'demo.example.com','path':'/example','owner':'a'*32,'device':'1'*32,'mapping_id':'one'}
            allocated=client.request('lease/acquire',item)
            self.assertIsInstance(allocated['port'],int)
            with self.assertRaises(AddressConflict):
                client.request('lease/acquire',{**item,'owner':'b'*32,'device':'2'*32})
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',allocated['port'])); listener.listen()
                self.assertTrue(client.request('lease/activate',item)['ok'])
                self.assertTrue(client.request('lease/heartbeat',item)['ok'])
            self.assertTrue(client.request('lease/release',item)['ok'])
        finally:
            caddy.terminate(); caddy.wait(timeout=10)

    def test_two_client_routes_and_conflict_with_real_caddy(self):
        work=ROOT/'tests/.work'; work.mkdir(exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=work); self.addCleanup(temp.cleanup)
        directory=Path(temp.name)
        sockets=[]
        for _ in range(9):
            sock=socket.socket(); sock.bind(('127.0.0.1',0)); sockets.append(sock)
        ports=[sock.getsockname()[1] for sock in sockets]
        manager_a,router_a,admin_a,manager_b,router_b,admin_b,gateway_port,registry_port,legacy_port=ports
        if 30000 <= legacy_port <= 30255:
            for sock in sockets: sock.close()
            self.skipTest('系统选择的隔离入口与端口范围重叠。')

        class Website(WebSocketWebsite):
            def log_message(self,*args): pass
            def do_GET(self):
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    return super().do_GET()
                body=(self.server.marker+':'+self.path).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(body))); self.send_header('Connection','close'); self.end_headers(); self.wfile.write(body)
        sites=[]
        for marker in ('first','second'):
            site=http.server.ThreadingHTTPServer(('127.0.0.1',0),Website); site.marker=marker
            threading.Thread(target=site.serve_forever,daemon=True).start(); sites.append(site)
        self.addCleanup(lambda:[(site.shutdown(),site.server_close()) for site in sites])
        for sock in sockets: sock.close()
        registry=Registry(directory/'registry.sqlite',base_domain='example.com',public_domain='demo.example.com',legacy_port=legacy_port)
        reg_server=Server(('127.0.0.1',registry_port),registry,'a'*64)
        threading.Thread(target=reg_server.serve_forever,daemon=True).start()
        self.addCleanup(reg_server.server_close); self.addCleanup(reg_server.shutdown)

        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with socket.create_connection(('127.0.0.1',self.server.router_port),timeout=3) as upstream:
                        peers=[self.request,upstream]
                        while True:
                            ready,_,_=select.select(peers,[],[],3)
                            if not ready: break
                            for source in ready:
                                data=source.recv(65536)
                                if not data: return
                                (upstream if source is self.request else self.request).sendall(data)
                except OSError:
                    pass
        class ForwardServer(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True

        class IsolatedWorker:
            def __init__(self, root, runtime, mode, settings, parent_ticks, mapping=None):
                self.running=True; self.child=None; self.forwarder=None
                if mode=='Router':
                    self.child=subprocess.Popen([str(root/'client/caddy.exe'),'run','--config',str(runtime/'Caddyfile'),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    for attempt in range(100):
                        try:
                            with socket.create_connection(('127.0.0.1',settings['LocalRouterPort']),timeout=.2): break
                        except OSError:
                            if self.child.poll() is not None or attempt==99: raise
                            time.sleep(.05)
                elif mode=='MappingTunnel':
                    self.forwarder=ForwardServer(('127.0.0.1',mapping['remote_port']),Forward)
                    self.forwarder.router_port=settings['LocalRouterPort']
                    threading.Thread(target=self.forwarder.serve_forever,daemon=True).start()
                    (runtime/('mapping-'+mapping['id']+'.log')).write_text('start proxy success',encoding='utf-8')
            def alive(self): return self.running and (self.child is None or self.child.poll() is None)
            def state(self): return {'status':'ready' if self.alive() else 'error'}
            def stop(self):
                if not self.running: return
                self.running=False
                if self.child is not None: self.child.terminate(); self.child.wait(timeout=10)
                if self.forwarder is not None: self.forwarder.shutdown(); self.forwarder.server_close()

        settings_a=load_settings(ROOT); settings_a.update(ManagerPort=manager_a,LocalRouterPort=router_a,LocalRouterAdminPort=admin_a,LocalPort=sites[0].server_port,SharedTestEnabled=False)
        settings_b=load_settings(ROOT); settings_b.update(ManagerPort=manager_b,LocalRouterPort=router_b,LocalRouterAdminPort=admin_b,LocalPort=sites[1].server_port,SharedTestEnabled=False)
        services=[]
        with patch('manager.service.run_ps',return_value='1'), patch('manager.service.Worker',IsolatedWorker), patch('manager.service.machine_owner',side_effect=['a'*32,'b'*32]):
            try:
                for name,settings in [('a',settings_a),('b',settings_b)]:
                    service=TunnelService(ROOT,directory/name,settings)
                    service.coordinator=Coordinator(registry); service._acquire_publish=lambda:None
                    services.append(service)
                a,b=services
                a._reconcile(); b._reconcile()
                self.assertTrue(a.status()['mappings'][0]['published'])
                self.assertFalse(b.mappings[0]['enabled'])
                b.mutate('POST',{'name':'第二台','port':sites[1].server_port,'path':'/other'})
                b._reconcile()
                self.assertTrue(next(item for item in b.status()['mappings'] if item['path']=='/other')['published'])
                gateway=directory/'gateway.Caddyfile'
                gateway.write_text('''{
 admin off
 auto_https off
}
http://:%s {
 bind 127.0.0.1
 route {
  @test path /test /test/*
  handle @test {
   respond "legacy-test" 200
  }
  handle {
   route {
    request_header -X-Personal-Tunnel-Port
    forward_auth 127.0.0.1:%s {
     uri /lookup
     header_up X-Forwarded-Host {host}
     copy_headers X-Personal-Tunnel-Port
    }
    reverse_proxy 127.0.0.1:{http.request.header.X-Personal-Tunnel-Port}
   }
  }
 }
}
'''%(gateway_port,registry_port),encoding='utf-8')
                gateway_process=subprocess.Popen([str(ROOT/'client/caddy.exe'),'run','--config',str(gateway),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    def get(path):
                        req=urllib.request.Request(f'http://127.0.0.1:{gateway_port}'+path,headers={'Host':'demo.example.com'})
                        return opener.open(req,timeout=3).read().decode()
                    for attempt in range(100):
                        try: result=get('/test/a'); break
                        except OSError:
                            if gateway_process.poll() is not None or attempt==99: raise
                            time.sleep(.05)
                    self.assertEqual(result,'legacy-test')
                    self.assertEqual(get('/'), 'first:/')
                    self.assertEqual(get('/other/page?x=1'), 'second:/page?x=1')
                    with socket.create_connection(('127.0.0.1',gateway_port),timeout=5) as ws:
                        ws.sendall(b'GET /other/socket HTTP/1.1\r\nHost: demo.example.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
                        reader=ws.makefile('rb'); self.assertIn(b'101',reader.readline())
                        while reader.readline()!=b'\r\n': pass
                        payload,mask=b'cooperative',b'abcd'
                        ws.sendall(bytes([0x81,0x80|len(payload)])+mask+bytes(v^mask[i%4] for i,v in enumerate(payload)))
                        self.assertEqual(reader.read(2),bytes([0x81,len(payload)])); self.assertEqual(reader.read(len(payload)),payload)
                    b.mutate('DELETE',mapping_id=next(item['id'] for item in b.mappings if item['path']=='/other'))
                    self.assertEqual(get('/other/page'), 'first:/other/page')
                finally:
                    gateway_process.terminate(); gateway_process.wait(timeout=10)
            finally:
                for service in services: service.close()


if __name__ == '__main__':
    unittest.main()
