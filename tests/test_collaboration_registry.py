"""验证并发映射占用、地址边界与过期释放。"""
from concurrent.futures import ThreadPoolExecutor
import http.server
import json
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.collaboration_registry import Conflict, LeasePending, Registry, Server
from manager.model import caddy_config, load_settings, validate_mapping
from common.collaboration_auth import sign


class Backend(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        body = (self.server.marker + ':' + self.path).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        work = ROOT / 'tests/.work'; work.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.directory.cleanup)
        first = 30000
        for _ in range(20):
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0)); legacy_port = probe.getsockname()[1]
            if not first <= legacy_port <= first+10:
                break
        else:
            self.fail('无法获取隔离的旧入口测试端口。')
        self.registry = Registry(Path(self.directory.name) / 'leases.db', base_domain='example.com',
                                 public_domain='demo.example.com', first_port=first, last_port=first+10, legacy_port=legacy_port)
        self.addCleanup(self.registry.close)
        self.a = {'hostname': 'demo.example.com', 'path': '/web', 'owner': 'a'*32, 'device': '1'*32, 'mapping_id': 'one'}

    def test_two_clients_atomically_contend_for_same_address(self):
        b = {**self.a, 'owner': 'b'*32, 'device': '2'*32, 'mapping_id': 'two'}
        def acquire(value):
            try:
                return self.registry.acquire(value)
            except Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acquire, (self.a, b)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count('conflict'), 1)

    def test_mapping_cannot_become_public_before_proxy_listens(self):
        self.registry.acquire(self.a)
        with self.assertRaises(LeasePending):
            self.registry.update(self.a, 'activate')
        self.assertIsNone(self.registry.lookup('demo.example.com', '/web'))

    def test_second_instance_on_same_computer_cannot_release_first_instance(self):
        lease = self.registry.acquire(self.a)
        other = {**self.a, 'owner':'c'*32}
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', lease['port'])); listener.listen()
            self.registry.update(self.a, 'activate')
            self.assertTrue(self.registry.check(other)['pending'])
            with self.assertRaises(LeasePending): self.registry.acquire(other)
            with self.assertRaises(LeasePending): self.registry.update(other, 'release')
            self.assertEqual(self.registry.lookup('demo.example.com', '/web/a'), lease['port'])
        self.registry.update(self.a, 'release')
        self.assertIsInstance(self.registry.acquire(other)['port'], int)

    def test_existing_database_without_uniqueness_is_rejected(self):
        bad = Path(self.directory.name) / 'bad.sqlite'
        db = sqlite3.connect(bad)
        try:
            db.execute('CREATE TABLE leases (hostname TEXT,path TEXT,owner TEXT,device TEXT,mapping_id TEXT,port INTEGER,active INTEGER,expires REAL)')
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ValueError):
            Registry(bad, base_domain='example.com', public_domain='demo.example.com', legacy_port=self.registry.legacy_port)

    def test_route_activation_prefix_boundaries_and_expiry(self):
        root = {**self.a, 'path': '/', 'owner': 'c'*32, 'device':'3'*32, 'mapping_id': 'root'}
        a = self.registry.acquire(self.a); r = self.registry.acquire(root)
        self.assertIsNone(self.registry.lookup('demo.example.com', '/web/a'))
        with patch.object(self.registry, 'listener_active', return_value=True):
            self.registry.update(self.a, 'activate'); self.registry.update(root, 'activate')
        self.assertEqual(self.registry.lookup('demo.example.com', '/web/a?x=1'), a['port'])
        self.assertEqual(self.registry.lookup('demo.example.com', '/WEB%2Fa'), a['port'])
        self.assertEqual(self.registry.lookup('demo.example.com', '//web/a'), a['port'])
        self.assertEqual(self.registry.lookup('demo.example.com', '/web'), a['port'])
        self.assertEqual(self.registry.lookup('demo.example.com', '/website'), r['port'])
        self.assertEqual(self.registry.lookup('demo.example.com', '/web/../website'), r['port'])
        with self.assertRaises(Conflict):
            self.registry.acquire({**self.a, 'path': '/WEB', 'owner': 'b'*32, 'device':'2'*32})
        with self.assertRaises(Conflict):
            self.registry.update({**self.a, 'owner': 'b'*32, 'device':'2'*32}, 'release')
        with self.registry.connect() as db:
            db.execute('UPDATE leases SET expires=0 WHERE path=?', ('/web',))
        self.assertEqual(self.registry.lookup('demo.example.com', '/web/a'), r['port'])
        other = self.registry.acquire({**self.a, 'owner': 'b'*32, 'device':'2'*32})
        self.assertFalse(other['active'])

    def test_subdomain_certificate_and_legacy_test_boundary(self):
        domain = {**self.a, 'hostname': 'web.example.com', 'path': '/'}
        self.registry.acquire(domain)
        self.assertFalse(self.registry.certificate_allowed('web.example.com'))
        with patch.object(self.registry, 'listener_active', return_value=True):
            self.registry.update(domain, 'activate')
        self.assertTrue(self.registry.certificate_allowed('web.example.com'))
        self.assertFalse(self.registry.certificate_allowed('other.example.com'))
        self.registry.update(domain, 'release')
        self.assertFalse(self.registry.certificate_allowed('web.example.com'))
        with self.assertRaises(ValueError):
            self.registry.acquire({**self.a, 'path': '/test'})
        with self.assertRaises(ValueError):
            self.registry.acquire({**self.a, 'path': '/TEST'})

    def test_expired_lease_cannot_be_taken_while_old_port_is_still_listening(self):
        old = self.registry.acquire(self.a)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', old['port'])); listener.listen()
            with self.registry.connect() as db:
                db.execute('UPDATE leases SET expires=0')
            other = {**self.a, 'owner': 'b'*32, 'device':'2'*32}
            self.assertTrue(self.registry.check(other)['pending'])
            with self.assertRaises(LeasePending):
                self.registry.acquire(other)
        self.assertFalse(self.registry.check(other)['occupied'])
        self.assertIsInstance(self.registry.acquire(other)['port'], int)

    def test_released_lease_waits_for_the_old_listener_to_close(self):
        old = self.registry.acquire(self.a)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', old['port'])); listener.listen()
            self.registry.update(self.a, 'activate')
            self.registry.update(self.a, 'release')
            self.assertIsNone(self.registry.lookup('demo.example.com', '/web'))
            other = {**self.a, 'owner': 'b'*32, 'device':'2'*32}
            self.assertTrue(self.registry.check(other)['pending'])
            with self.assertRaises(LeasePending):
                self.registry.acquire(other)
        self.assertIsInstance(self.registry.acquire(other)['port'], int)

    def test_legacy_entry_and_new_leases_never_take_over_each_other(self):
        with socket.socket() as legacy:
            legacy.bind(('127.0.0.1', self.registry.legacy_port)); legacy.listen()
            self.assertTrue(self.registry.check(self.a)['pending'])
            self.assertEqual(self.registry.lookup('demo.example.com', '/web/a'), self.registry.legacy_port)
            with self.assertRaises(LeasePending):
                self.registry.acquire(self.a)
        self.registry.acquire(self.a)
        with socket.socket() as legacy:
            with self.assertRaises(OSError):
                legacy.bind(('127.0.0.1', self.registry.legacy_port))
        self.registry.update(self.a, 'release')
        with socket.socket() as legacy:
            legacy.bind(('127.0.0.1', self.registry.legacy_port))

    def test_legacy_subdomain_certificate_check_uses_existing_client(self):
        settings=load_settings(ROOT)
        with socket.socket() as reserved, socket.socket() as probe:
            reserved.bind(('127.0.0.1', self.registry.legacy_port))
            probe.bind(('127.0.0.1',0)); admin_port=probe.getsockname()[1]
        settings['LocalRouterPort']=self.registry.legacy_port
        settings['LocalRouterAdminPort']=admin_port
        item=validate_mapping({'name':'旧网站','port':8765,'path':'/','hostname':'web.example.com'},settings,mapping_id='legacy')
        config=Path(self.directory.name)/'legacy.Caddyfile'
        config.write_text(caddy_config([item],settings,Path(self.directory.name)),encoding='utf-8')
        process=subprocess.Popen([str(ROOT/'client/caddy.exe'),'run','--config',str(config),'--adapter','caddyfile'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.addCleanup(lambda:(process.terminate(),process.wait(timeout=10)))
        for attempt in range(80):
            try:
                with socket.create_connection(('127.0.0.1',self.registry.legacy_port),timeout=.2): break
            except OSError:
                if process.poll() is not None or attempt==79: raise
                time.sleep(.05)
        self.assertTrue(self.registry.certificate_allowed('web.example.com'))
        self.assertFalse(self.registry.certificate_allowed('other.example.com'))
        self.assertTrue(self.registry.check(self.a)['pending'])

    def test_registry_restart_preserves_legacy_winner_and_does_not_steal(self):
        lease=self.registry.acquire(self.a)
        with socket.socket() as active:
            active.bind(('127.0.0.1',lease['port'])); active.listen()
            self.registry.update(self.a,'activate')
            self.registry.close()
            with socket.socket() as legacy:
                legacy.bind(('127.0.0.1',self.registry.legacy_port)); legacy.listen()
                restarted=Registry(self.registry.db,base_domain='example.com',public_domain='demo.example.com',
                    first_port=self.registry.first_port,last_port=self.registry.last_port,legacy_port=self.registry.legacy_port)
                try:
                    self.assertEqual(restarted.lookup('demo.example.com','/web'),self.registry.legacy_port)
                    with self.assertRaises(LeasePending): restarted.update(self.a,'heartbeat')
                    self.assertTrue(restarted.check(self.a)['pending'])
                finally:
                    restarted.close()

    def test_caddy_forwards_only_active_lease_and_ignores_client_port_header(self):
        lease = self.registry.acquire(self.a)
        backend = http.server.ThreadingHTTPServer(('127.0.0.1', lease['port']), Backend)
        backend.marker = 'first'
        other_mapping = {**self.a, 'path': '/other', 'owner': 'b'*32, 'device':'2'*32, 'mapping_id':'other'}
        second_lease = self.registry.acquire(other_mapping)
        second = http.server.ThreadingHTTPServer(('127.0.0.1', second_lease['port']), Backend)
        second.marker = 'second'
        registry = Server(('127.0.0.1', 0), self.registry, 'a'*64)
        for server in (backend, second, registry):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: [(server.shutdown(), server.server_close()) for server in (backend, second, registry)])
        self.registry.update(self.a, 'activate')
        self.registry.update(other_mapping, 'activate')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); caddy_port = probe.getsockname()[1]
        config = Path(self.directory.name) / 'Caddyfile'
        config.write_text('''{
  admin off
  auto_https off
}
http://:%s {
  bind 127.0.0.1
  route {
    @registry path /__personal_web_tunnel/registry/health /__personal_web_tunnel/registry/lease/*
    handle @registry {
      uri strip_prefix /__personal_web_tunnel/registry
      reverse_proxy 127.0.0.1:%s
    }
    @test path /test /test/*
    handle @test {
      respond "legacy" 200
    }
    handle {
      route {
        request_header -X-Personal-Tunnel-Port
        forward_auth 127.0.0.1:%s {
          uri /lookup
          header_up X-Forwarded-Host {host}
          copy_headers X-Personal-Tunnel-Port
        }
        reverse_proxy 127.0.0.1:{http.request.header.X-Personal-Tunnel-Port} {
          header_up -X-Personal-Tunnel-Port
        }
      }
    }
  }
}
''' % (caddy_port, registry.server_port, registry.server_port), encoding='utf-8')
        process = subprocess.Popen([str(ROOT / 'client/caddy.exe'), 'run', '--config', str(config), '--adapter', 'caddyfile'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.addCleanup(lambda: (process.terminate(), process.wait(timeout=10)))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path, extra=None):
            headers = {'Host': 'demo.example.com', **(extra or {})}
            return opener.open(urllib.request.Request(f'http://127.0.0.1:{caddy_port}'+path, headers=headers), timeout=3).read().decode()
        for attempt in range(50):
            try:
                result = get('/test/a'); break
            except OSError:
                if process.poll() is not None or attempt == 49: raise
                time.sleep(.1)
        self.assertEqual(result, 'legacy')
        self.assertEqual(get('/%74est/a'), 'legacy')
        self.assertEqual(json.loads(get('/__personal_web_tunnel/registry/health'))['service'], 'personal-web-tunnel-coordination-v1')
        request = urllib.request.Request(f'http://127.0.0.1:{caddy_port}/__personal_web_tunnel/registry/lease/acquire',
            data=json.dumps(self.a).encode(), method='POST', headers={'Host':'demo.example.com', 'Authorization':'Bearer wrong'})
        with self.assertRaises(urllib.error.HTTPError) as unauthorized:
            opener.open(request, timeout=3)
        self.assertEqual(unauthorized.exception.code, 403)
        def signed_acquire(value):
            nonce=secrets.token_hex(16)
            health=json.loads(get('/__personal_web_tunnel/registry/health?nonce='+nonce))
            ticket=health['ticket']; self.assertEqual(health['proof'],sign('a'*64,'health',nonce,ticket))
            body=json.dumps(value).encode()
            headers={'Host':'demo.example.com','X-Personal-Tunnel-Ticket':ticket,
                     'X-Personal-Tunnel-Signature':sign('a'*64,'request',ticket,'/lease/acquire',body)}
            return urllib.request.Request(f'http://127.0.0.1:{caddy_port}/__personal_web_tunnel/registry/lease/acquire',data=body,method='POST',headers=headers)
        valid=signed_acquire(self.a)
        with opener.open(valid, timeout=3) as response:
            result=response.read(); self.assertEqual(json.loads(result)['port'],lease['port'])
            self.assertEqual(response.headers['X-Personal-Tunnel-Proof'], sign('a'*64,'response',valid.get_header('X-personal-tunnel-ticket'),'200',result))
        with self.assertRaises(urllib.error.HTTPError) as replay:
            opener.open(valid,timeout=3)
        self.assertEqual(replay.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            opener.open(signed_acquire({**self.a, 'owner': 'b'*32, 'device':'2'*32}),timeout=3)
        self.assertEqual(conflict.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as internal:
            get('/__personal_web_tunnel/registry/lookup')
        self.assertEqual(internal.exception.code, 404)
        self.assertEqual(get('/web/a?q=1', {'X-Personal-Tunnel-Port': '22', 'X-Forwarded-Host': 'evil.example', 'X-Forwarded-Uri': '/other'}), 'first:/web/a?q=1')
        self.assertEqual(get('/other/a'), 'second:/other/a')
        self.assertEqual(get('//web/a'), 'first:/web/a')
        with self.assertRaises(urllib.error.HTTPError) as missing:
            get('/website')
        self.assertEqual(missing.exception.code, 404)
        backend.shutdown(); backend.server_close()
        self.registry.update(self.a, 'release')
        with self.assertRaises(urllib.error.HTTPError) as removed:
            get('/web/a')
        self.assertEqual(removed.exception.code, 404)
        second.shutdown(); second.server_close()
        self.registry.update(other_mapping, 'release')
        legacy = http.server.ThreadingHTTPServer(('127.0.0.1', self.registry.legacy_port), Backend)
        legacy.marker = 'legacy-main'
        threading.Thread(target=legacy.serve_forever, daemon=True).start()
        self.addCleanup(legacy.server_close); self.addCleanup(legacy.shutdown)
        self.assertEqual(get('/old/a'), 'legacy-main:/old/a')
        with self.assertRaises(LeasePending):
            self.registry.acquire(self.a)


if __name__ == '__main__':
    unittest.main()
