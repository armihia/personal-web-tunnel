"""服务器端协作登记：原子占用公网地址并分配仅限本机的代理端口。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hmac
import json
import posixpath
import re
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.collaboration_auth import SERVICE, check_ticket, issue_ticket, sign

class Conflict(ValueError):
    pass


class LeaseExpired(ValueError):
    pass


class LeasePending(ValueError):
    pass


class Registry:
    def __init__(self, db: Path, *, base_domain: str, public_domain: str,
                 first_port: int = 30000, last_port: int = 30255, ttl: int = 45,
                 test_port: int = 18080, legacy_port: int = 18081):
        self.db = db
        self.base_domain = base_domain.lower()
        self.public_domain = public_domain.lower()
        self.first_port, self.last_port, self.ttl, self.test_port, self.legacy_port = first_port, last_port, ttl, test_port, legacy_port
        if not (1024 <= first_port <= last_port <= 65535 and 10 <= ttl <= 300 and
                1024 <= test_port <= 65535 and 1024 <= legacy_port <= 65535 and
                test_port != legacy_port and not first_port <= legacy_port <= last_port and
                not first_port <= test_port <= last_port):
            raise ValueError('协作端口范围或租约期限不正确。')
        self.lock = threading.RLock()
        self.guard = None
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS leases (
                hostname TEXT NOT NULL, path TEXT NOT NULL, owner TEXT NOT NULL, device TEXT NOT NULL,
                mapping_id TEXT NOT NULL, port INTEGER NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL,
                PRIMARY KEY (hostname, path))''')
            db.execute('CREATE TABLE IF NOT EXISTS used_challenges (nonce TEXT PRIMARY KEY, expires REAL NOT NULL)')
            challenge_columns = {row['name']: row for row in db.execute('PRAGMA table_info(used_challenges)')}
            if 'nonce' not in challenge_columns or challenge_columns['nonce']['pk'] != 1 or 'expires' not in challenge_columns:
                raise ValueError('协作认证记录缺少必要的唯一约束，拒绝启动。')
            db.execute('SELECT hostname,path,owner,device,mapping_id,port,active,expires FROM leases LIMIT 0')
            unique_fields = set()
            for index in db.execute('PRAGMA index_list(leases)').fetchall():
                if index['unique']:
                    name = index['name'].replace('"', '""')
                    unique_fields.add(tuple(row['name'] for row in db.execute('PRAGMA index_info("'+name+'")')))
            if ('hostname', 'path') not in unique_fields or ('port',) not in unique_fields:
                raise ValueError('协作数据库缺少必要的唯一约束，拒绝启动。')
            if db.execute('SELECT 1 FROM leases LIMIT 1').fetchone():
                self._claim_guard()

    def consume_challenge(self, nonce: str, expires: int) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM used_challenges WHERE expires <= ?', (time.time(),))
            try:
                db.execute('INSERT INTO used_challenges VALUES (?,?)', (nonce, expires))
            except sqlite3.IntegrityError:
                return False
            return True

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def validate(self, value: dict, *, allow_test: bool = False) -> tuple[str, str, str, str, str]:
        if not isinstance(value, dict):
            raise ValueError('请求格式不正确。')
        host, path, owner, device, mapping_id = (value.get(k, '') for k in ('hostname', 'path', 'owner', 'device', 'mapping_id'))
        if not all(isinstance(v, str) for v in (host, path, owner, device, mapping_id)):
            raise ValueError('映射字段格式不正确。')
        host, path = host.lower().rstrip('.'), path.lower()
        domain_pattern = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.' + re.escape(self.base_domain)
        if host != self.public_domain and not re.fullmatch(domain_pattern, host):
            raise ValueError('独立域名必须是受管理的一级子域名。')
        if path != '/' and not re.fullmatch(r'/[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', path):
            raise ValueError('映射路径格式不正确。')
        if host != self.public_domain and path != '/':
            raise ValueError('独立域名使用根路径。')
        if host == self.public_domain and path == '/test' and not allow_test:
            raise ValueError('/test 继续由兼容入口独立管理。')
        if not re.fullmatch(r'[a-f0-9]{32}', owner) or not re.fullmatch(r'[a-f0-9]{32}', device) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', mapping_id):
            raise ValueError('客户端或映射标识不正确。')
        return host, path, owner, device, mapping_id

    @staticmethod
    def port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', port))
                return True
            except OSError:
                return False

    @staticmethod
    def listener_active(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(.25)
            return probe.connect_ex(('127.0.0.1', port)) == 0

    def _claim_guard(self) -> bool:
        if self.guard is not None:
            return True
        guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            guard.bind(('127.0.0.1', self.legacy_port))
            self.guard = guard
            return True
        except OSError:
            guard.close()
            return False

    def close(self):
        with self.lock:
            if self.guard is not None:
                self.guard.close()
                self.guard = None

    def _purge(self, db, now):
        for expired in db.execute('SELECT hostname, path, port FROM leases WHERE expires <= ?', (now,)).fetchall():
            if not self.listener_active(expired['port']):
                db.execute('DELETE FROM leases WHERE hostname=? AND path=?', (expired['hostname'], expired['path']))

    def reap(self):
        with self.lock:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                self._purge(db, time.time())
                occupied = db.execute('SELECT 1 FROM leases LIMIT 1').fetchone() is not None
            if not occupied:
                self.close()

    def check(self, value: dict) -> dict:
        host, path, owner, device, mapping_id = self.validate(value, allow_test=True)
        if host == self.public_domain and path == '/test':
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(.25)
                occupied = probe.connect_ex(('127.0.0.1', self.test_port)) == 0
            return {'occupied': occupied}
        with self.lock:
            if self.guard is None and not self.port_available(self.legacy_port):
                return {'occupied': False, 'pending': True}
            with self.connect() as db:
                current = db.execute('SELECT owner, device, mapping_id, port, expires FROM leases WHERE hostname=? AND path=?',
                                     (host, path)).fetchone()
            if current and current['expires'] <= time.time():
                if not self.listener_active(current['port']):
                    current = None
                elif current['owner'] != owner or current['device'] != device or current['mapping_id'] != mapping_id:
                    return {'occupied': False, 'pending': True}
            if current and current['device'] == device and current['owner'] != owner:
                return {'occupied': False, 'pending': True}
            return {'occupied': bool(current and (current['owner'] != owner or current['device'] != device or current['mapping_id'] != mapping_id))}

    def acquire(self, value: dict) -> dict:
        host, path, owner, device, mapping_id = self.validate(value)
        now = time.time()
        with self.lock:
            try:
                with self.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._purge(db, now)
                    if not self._claim_guard():
                        raise LeasePending('旧版客户端正在使用共享入口，暂时无法核对逐条映射。')
                    old = db.execute('SELECT * FROM leases WHERE hostname=? AND path=?', (host, path)).fetchone()
                    if old:
                        if old['owner'] != owner or old['device'] != device or old['mapping_id'] != mapping_id:
                            if old['expires'] <= now or old['device'] == device:
                                raise LeasePending('原连接正在释放，请稍后重试。')
                            raise Conflict('该公网地址已由其他电脑使用。')
                        db.execute('UPDATE leases SET expires=? WHERE hostname=? AND path=?', (now+self.ttl, host, path))
                        result = {'port': old['port'], 'active': bool(old['active']), 'ttl': self.ttl}
                    else:
                        used = {row['port'] for row in db.execute('SELECT port FROM leases')}
                        port = next((port for port in range(self.first_port, self.last_port+1)
                                     if port not in used and self.port_available(port)), None)
                        if port is None:
                            raise RuntimeError('服务器协作代理端口已用完。')
                        db.execute('INSERT INTO leases VALUES (?,?,?,?,?,?,?,?)', (host, path, owner, device, mapping_id, port, 0, now+self.ttl))
                        result = {'port': port, 'active': False, 'ttl': self.ttl}
                return result
            finally:
                # 只有数据库提交后才释放入口，避免旧连接与新登记同时成功。
                with self.connect() as db:
                    occupied = db.execute('SELECT 1 FROM leases LIMIT 1').fetchone() is not None
                if not occupied:
                    self.close()

    def update(self, value: dict, action: str) -> None:
        host, path, owner, device, mapping_id = self.validate(value)
        now = time.time()
        with self.lock:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM leases WHERE hostname=? AND path=?', (host, path)).fetchone()
                if not current or current['expires'] <= now:
                    raise LeaseExpired('协作登记已过期，请重新申请。')
                if current['owner'] != owner or current['device'] != device or current['mapping_id'] != mapping_id:
                    if current['device'] == device:
                        raise LeasePending('本机另一运行实例仍在使用该地址。')
                    raise Conflict('该公网地址已由其他电脑使用。')
                if action == 'release':
                    if not self.listener_active(current['port']):
                        db.execute('DELETE FROM leases WHERE hostname=? AND path=?', (host, path))
                    else:
                        # 连接仍在释放时保留占用记录，避免旧会话和新持有者同时存活。
                        db.execute('UPDATE leases SET active=0, expires=0 WHERE hostname=? AND path=?', (host, path))
                else:
                    if not self._claim_guard():
                        raise LeasePending('旧版客户端正在使用共享入口，暂时无法核对逐条映射。')
                    if action == 'activate' and not self.listener_active(current['port']):
                        raise LeasePending('协作映射的服务器端口尚未就绪。')
                    db.execute('UPDATE leases SET active=?, expires=? WHERE hostname=? AND path=?',
                               (1 if action == 'activate' else current['active'], now+self.ttl, host, path))
                remaining = db.execute('SELECT 1 FROM leases LIMIT 1').fetchone() is not None
            if not remaining:
                self.close()

    def lookup(self, hostname: str, uri: str) -> int | None:
        host = hostname.split(':', 1)[0].lower().rstrip('.')
        try:
            if not uri.startswith('/'):
                return None
            raw = unquote(uri.partition('?')[0], errors='strict')
            if '\x00' in raw:
                return None
            path = '/' + posixpath.normpath('/' + raw.lstrip('/')).lstrip('/').lower()
        except (UnicodeError, ValueError):
            return None
        with self.lock:
            if self.guard is None:
                return self.legacy_port if not self.port_available(self.legacy_port) else None
            with self.connect() as db:
                rows = db.execute('SELECT path, port FROM leases WHERE hostname=? AND active=1 AND expires>?',
                                  (host, time.time())).fetchall()
            matches = [row for row in rows if row['path'] == '/' or path == row['path'] or path.startswith(row['path']+'/')]
            return max(matches, key=lambda row: len(row['path']))['port'] if matches else None

    def certificate_allowed(self, hostname: str) -> bool:
        with self.lock:
            if self.guard is None:
                if self.port_available(self.legacy_port):
                    return False
                connection = HTTPConnection('127.0.0.1', self.legacy_port, timeout=2)
                try:
                    connection.request('GET', '/__personal_web_tunnel/certificate-permission?' + urlencode({'domain': hostname}))
                    response = connection.getresponse()
                    response.read(8192)
                    return response.status == 200
                except (OSError, ValueError, HTTPException):
                    return False
                finally:
                    connection.close()
            with self.connect() as db:
                return db.execute('SELECT 1 FROM leases WHERE hostname=? AND path=? AND active=1 AND expires>?',
                                  (hostname.lower().rstrip('.'), '/', time.time())).fetchone() is not None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address, registry: Registry, token: str):
        super().__init__(address, Handler)
        self.registry, self.token = registry, token
        self.reaper_stop = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True)
        self.reaper.start()

    def _reap(self):
        while not self.reaper_stop.wait(5):
            try:
                self.registry.reap()
            except (OSError, sqlite3.Error):
                pass

    def server_close(self):
        self.reaper_stop.set()
        self.reaper.join(timeout=6)
        super().server_close()
        self.registry.close()


class Handler(BaseHTTPRequestHandler):
    server: Server
    def setup(self):
        super().setup()
        self.connection.settimeout(8)
        self.response_ticket = None
    def log_message(self, *args):
        pass

    def send_json(self, status: int, value: dict, extra: dict | None = None):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if self.response_ticket:
            self.send_header('X-Personal-Tunnel-Proof', sign(self.server.token, 'response', self.response_ticket, str(status), body))
        for key, val in (extra or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.response_ticket = None
        try:
            if self.path not in ('/lease/check', '/lease/acquire', '/lease/activate', '/lease/heartbeat', '/lease/release'):
                self.send_json(404, {'error': '接口不存在。'})
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8192:
                raise ValueError('请求长度不正确。')
            raw = self.rfile.read(length)
            ticket = self.headers.get('X-Personal-Tunnel-Ticket', '')
            verified = check_ticket(self.server.token, ticket)
            proof = self.headers.get('X-Personal-Tunnel-Signature', '')
            if (not verified or not re.fullmatch(r'[a-f0-9]{64}', proof) or
                not hmac.compare_digest(proof, sign(self.server.token, 'request', ticket, self.path, raw)) or
                not self.server.registry.consume_challenge(*verified)):
                self.send_json(403, {'error': '认证失败。'})
                return
            self.response_ticket = ticket
            value = json.loads(raw)
            action = self.path.rsplit('/', 1)[-1]
            if action == 'check':
                result = self.server.registry.check(value)
            elif action == 'acquire':
                result = self.server.registry.acquire(value)
            else:
                self.server.registry.update(value, action)
                result = {}
            self.send_json(200, {'ok': True, **result})
        except Conflict as exc:
            self.send_json(409, {'error': str(exc)})
        except LeaseExpired as exc:
            self.send_json(410, {'error': str(exc)})
        except LeasePending as exc:
            self.send_json(423, {'error': str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {'error': str(exc)})
        except Exception:
            self.send_json(503, {'error': '协作登记暂不可用。'})

    def do_GET(self):
        self.response_ticket = None
        parsed = urlsplit(self.path)
        if parsed.path == '/lookup':
            port = self.server.registry.lookup(self.headers.get('X-Forwarded-Host', ''), self.headers.get('X-Forwarded-Uri', '/'))
            if port:
                self.send_json(200, {'ok': True}, {'X-Personal-Tunnel-Port': port})
            else:
                self.send_json(404, {'error': '没有已发布的映射。'})
        elif parsed.path == '/certificate-permission':
            host = parse_qs(parsed.query).get('domain', [''])[0]
            self.send_json(200 if self.server.registry.certificate_allowed(host) else 403, {})
        elif parsed.path == '/health':
            try:
                with self.server.registry.connect() as db:
                    db.execute('SELECT hostname, path FROM leases LIMIT 1').fetchone()
                result = {'ok': True, 'service': SERVICE}
                nonce = parse_qs(parsed.query).get('nonce', [''])[0]
                if re.fullmatch(r'[a-f0-9]{32}', nonce):
                    ticket = issue_ticket(self.server.token)
                    result.update(ticket=ticket, proof=sign(self.server.token, 'health', nonce, ticket))
                self.send_json(200, result)
            except sqlite3.Error:
                self.send_json(503, {'ok': False, 'error': '协作数据库不可用。'})
        else:
            self.send_json(404, {'error': '接口不存在。'})


def main():
    parser = argparse.ArgumentParser(description='内网穿透协作登记服务')
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--base-domain', required=True)
    parser.add_argument('--public-domain', required=True)
    parser.add_argument('--port', type=int, default=18083)
    parser.add_argument('--first-port', type=int, default=30000)
    parser.add_argument('--last-port', type=int, default=30255)
    parser.add_argument('--test-port', type=int, default=18080)
    parser.add_argument('--legacy-port', type=int, default=18081)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding='utf-8-sig').strip()
    if not re.fullmatch(r'[a-fA-F0-9]{64}', token):
        raise ValueError('认证令牌格式不正确。')
    registry = Registry(args.database, base_domain=args.base_domain, public_domain=args.public_domain,
                        first_port=args.first_port, last_port=args.last_port, test_port=args.test_port,
                        legacy_port=args.legacy_port)
    Server(('127.0.0.1', args.port), registry, token).serve_forever()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        try:
            with Path(__file__).with_name('collaboration-registry.log').open('a', encoding='utf-8') as stream:
                traceback.print_exc(file=stream)
        except OSError:
            pass
        raise
