"""协作服务的单次挑战认证，避免向可能是旧网站的入口发送令牌。"""
import hashlib
import hmac
import re
import secrets
import time

SERVICE = 'personal-web-tunnel-coordination-v1'


def sign(token: str, label: str, *parts: str | bytes) -> str:
    values = [label.encode('ascii')]
    values.extend(value if isinstance(value, bytes) else value.encode('utf-8') for value in parts)
    return hmac.new(token.encode('ascii'), b'\n'.join(values), hashlib.sha256).hexdigest()


def issue_ticket(token: str) -> str:
    timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
    return timestamp + '.' + nonce + '.' + sign(token, 'ticket', timestamp, nonce)


def check_ticket(token: str, ticket: str) -> tuple[str, int] | None:
    match = re.fullmatch(r'(\d{10})\.([a-f0-9]{32})\.([a-f0-9]{64})', ticket)
    if not match:
        return None
    timestamp, nonce, proof = match.groups()
    now = time.time()
    if not now - 30 <= int(timestamp) <= now + 2:
        return None
    if not hmac.compare_digest(proof, sign(token, 'ticket', timestamp, nonce)):
        return None
    return nonce, int(timestamp) + 35
