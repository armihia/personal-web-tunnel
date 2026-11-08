"""经服务器 HTTPS 入口登记映射；使用服务器 IP 直连并校验证书。"""
from __future__ import annotations

import http.client
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import time
import uuid

from common.collaboration_auth import SERVICE, sign


class AddressConflict(RuntimeError):
    pass


class LeaseExpired(RuntimeError):
    pass


class LeasePending(RuntimeError):
    pass


class RegistryUnavailable(RuntimeError):
    pass


class DirectHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        raw = socket.create_connection((self.ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def machine_owner(fallback: Path) -> str:
    """不同电脑各自保存随机身份，不放入项目发布包。"""
    local = os.environ.get('LOCALAPPDATA')
    directory = Path(local) / 'PersonalWebTunnel' if local else fallback
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'collaboration-owner.txt'
    try:
        with path.open('x', encoding='ascii') as stream:
            stream.write(uuid.uuid4().hex)
    except FileExistsError:
        pass
    for attempt in range(20):
        owner = path.read_text(encoding='ascii').strip()
        if re.fullmatch(r'[a-f0-9]{32}', owner):
            return owner
        if attempt < 19:
            time.sleep(.05)
    raise ValueError('本机协作身份文件不正确。')


class CollaborationClient:
    def __init__(self, settings: dict, token_path: Path, *, connection_factory=DirectHTTPS):
        self.domain = settings['PublicDomain']
        self.ip = settings['DirectServerIP']
        self.token_path = token_path
        self.connection_factory = connection_factory

    def _exchange(self, method: str, path: str, body: bytes | None, headers: dict) -> tuple[int, bytes, str]:
        connection = self.connection_factory(self.domain, self.ip, 3)
        try:
            connection.request(method, '/__personal_web_tunnel/registry/' + path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(8193)
            if len(raw) > 8192:
                raise RegistryUnavailable('服务器协作响应过大。')
            return response.status, raw, response.getheader('X-Personal-Tunnel-Proof', '')
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise RegistryUnavailable('无法连接服务器协作服务。') from exc
        finally:
            connection.close()

    def request(self, action: str, payload: dict | None = None) -> dict:
        if action not in {'health', 'lease/check', 'lease/acquire', 'lease/activate', 'lease/heartbeat', 'lease/release'}:
            raise ValueError('不支持的协作操作。')
        try:
            token = self.token_path.read_text(encoding='utf-8-sig').strip()
        except OSError as exc:
            raise RegistryUnavailable('客户端令牌不可用。') from exc
        if not re.fullmatch(r'[a-fA-F0-9]{64}', token):
            raise RegistryUnavailable('客户端令牌不可用。')
        nonce = secrets.token_hex(16)
        status, raw, _ = self._exchange('GET', 'health?nonce=' + nonce, None, {'Accept': 'application/json'})
        try:
            value = json.loads(raw.decode('utf-8'))
            if not isinstance(value, dict):
                raise RegistryUnavailable('服务器协作响应格式不正确。')
            ticket, proof = value.get('ticket', ''), value.get('proof', '')
            if (status != 200 or not value.get('ok') or value.get('service') != SERVICE or
                not isinstance(ticket, str) or not re.fullmatch(r'\d{10}\.[a-f0-9]{32}\.[a-f0-9]{64}', ticket) or
                not isinstance(proof, str) or not re.fullmatch(r'[a-f0-9]{64}', proof) or
                not hmac.compare_digest(proof, sign(token, 'health', nonce, ticket))):
                raise RegistryUnavailable('无法验证服务器协作服务身份。')
            if action == 'health':
                return {'ok': True}
            if payload is None:
                raise ValueError('协作操作缺少映射参数。')
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            internal_path = '/' + action
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                       'X-Personal-Tunnel-Ticket': ticket,
                       'X-Personal-Tunnel-Signature': sign(token, 'request', ticket, internal_path, body)}
            status, raw, response_proof = self._exchange('POST', action, body, headers)
            if (not isinstance(response_proof, str) or not re.fullmatch(r'[a-f0-9]{64}', response_proof) or
                not hmac.compare_digest(response_proof, sign(token, 'response', ticket, str(status), raw))):
                raise RegistryUnavailable('服务器协作响应认证失败。')
            value = json.loads(raw.decode('utf-8'))
            if status == 409:
                raise AddressConflict('该公网地址已由其他电脑使用。')
            if status == 410:
                raise LeaseExpired('协作登记已过期，正在重新申请。')
            if status == 423:
                raise LeasePending('旧连接或兼容入口尚未释放，暂时无法核对。请更新旧版客户端或等待断开。')
            if status != 200 or not isinstance(value, dict) or not value.get('ok'):
                raise RegistryUnavailable('服务器协作服务未就绪。')
            return value
        except (AddressConflict, LeaseExpired, LeasePending):
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise RegistryUnavailable('无法连接服务器协作服务。') from exc
