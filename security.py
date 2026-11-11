"""Request bounds enforced before multipart parsing; private responses never cached."""
import os
import base64
import secrets
from starlette.responses import JSONResponse

MAX_REQUEST = 5 * 1024 * 1024


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        pilot_user, pilot_password = os.getenv('PRIVATE_PILOT_USER', ''), os.getenv('PRIVATE_PILOT_PASSWORD', '')
        # /verify only shows signed report facts, so QR codes work for supervisors without the pilot login.
        if (pilot_user or pilot_password) and scope['path'] not in ('/health', '/verify', '/privacy'):
            expected = b'Basic ' + base64.b64encode((pilot_user + ':' + pilot_password).encode())
            if (not pilot_user or len(pilot_password) < 16 or
                    not secrets.compare_digest(headers.get(b'authorization', b''), expected)):
                return await JSONResponse({'detail': 'Private pilot access required', 'code': 'auth_required'}, 401,
                    headers={'WWW-Authenticate': 'Basic realm="PlagIQ private pilot"', 'Cache-Control':'no-store'})(scope, receive, send)
        if scope['method'] in ('POST', 'DELETE'):
            origin = headers.get(b'origin')
            host = headers.get(b'host', b'')
            allowed = os.environ.get('PUBLIC_ORIGIN', '').encode()
            if origin and origin not in (allowed, b'http://' + host, b'https://' + host):
                return await JSONResponse({'detail': 'Foreign origin rejected', 'code': 'origin_rejected'}, 403)(scope, receive, send)
            if headers.get(b'sec-fetch-site') == b'cross-site':
                return await JSONResponse({'detail': 'Cross-site request rejected', 'code': 'origin_rejected'}, 403)(scope, receive, send)
        # Buffer only the bounded raw body, before FastAPI parses files/fields.
        if scope['method'] == 'POST':
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                size += len(message.get('body', b''))
                if size > MAX_REQUEST:
                    return await JSONResponse({'detail': 'Request exceeds 5 MB', 'code': 'request_too_large'}, 413)(scope, receive, send)
                chunks.append(message)
                if not message.get('more_body', False):
                    break
            async def bounded_receive():
                return chunks.pop(0) if chunks else await receive()
        else:
            bounded_receive = receive

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = [(k, v) for k, v in message['headers'] if k.lower() != b'cache-control'] + [
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'), (b'x-frame-options', b'DENY'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
            await send(message)
        await self.app(scope, bounded_receive, private_send)
