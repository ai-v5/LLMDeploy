"""Notification transport; credentials are loaded from a local file at send time."""
import http.client
import json
from pathlib import Path
from urllib.parse import urlsplit

from .tracker import redact


class Unavailable(Exception):
    """No bytes were sent; a later attempt is safe."""


class Rejected(Exception):
    """The service explicitly rejected the request."""


class Uncertain(Exception):
    """Delivery may have happened; automatic resend could duplicate it."""


class Xiaoluban:
    def __init__(self, settings):
        self.settings = settings

    def token(self):
        try:
            value = json.loads(Path(self.settings['token_file']).read_text(encoding='utf-8-sig'))
            token = value.get('token') if isinstance(value, dict) else None
            if not isinstance(token, str) or not token.strip():
                raise Unavailable('CREDENTIAL_MISSING')
            return token.strip()
        except (OSError, ValueError):
            raise Unavailable('CREDENTIAL_MISSING') from None

    def send(self, content):
        token = self.token()
        url = urlsplit(self.settings['endpoint'])
        connection_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(url.hostname, url.port, timeout=10)
        payload = json.dumps({'content': redact(content.replace(token, '[REDACTED]')), 'receiver': self.settings['receiver'],
                              'auth': token}, ensure_ascii=False).encode('utf-8')
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException):
                raise Unavailable('CONNECT_FAILED') from None
            try:
                connection.request('POST', url.path or '/', payload,
                                   {'Content-Type': 'application/json; charset=utf-8', 'Connection': 'close'})
                response = connection.getresponse()
                body = response.read(65537)
                if response.status in (400, 401, 403, 404, 405, 422, 429):
                    raise Rejected('HTTP_REJECTED')
                if not 200 <= response.status < 300 or len(body) > 65536:
                    raise Uncertain('HTTP_OUTCOME_UNCERTAIN')
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError):
                    raise Uncertain('HTTP_ACCEPTED_UNVERIFIED') from None
                if not isinstance(result, dict):
                    raise Uncertain('HTTP_ACCEPTED_UNVERIFIED')
                acknowledged = result.get('success') is True
                rejected = result.get('success') is False
                unknown = False
                for key in ('code', 'errcode'):
                    if key in result:
                        code = str(result[key]).lower()
                        if code in ('0', '200', 'ok', 'success'):
                            acknowledged = True
                        elif code in ('400', '401', '403', '404', '405', '422', '429'):
                            rejected = True
                        else:
                            unknown = True
                if unknown or (acknowledged and rejected):
                    raise Uncertain('APPLICATION_OUTCOME_UNCERTAIN')
                if rejected:
                    raise Rejected('APPLICATION_REJECTED')
                if not acknowledged:
                    raise Uncertain('HTTP_ACCEPTED_UNVERIFIED')
                return 'SERVICE_ACCEPTED'
            except (OSError, http.client.HTTPException):
                # Do not log request/response bodies or exception text containing credentials.
                raise Uncertain('DELIVERY_UNCERTAIN') from None
        finally:
            connection.close()


def build_backend(settings):
    if settings is None:
        return None
    if settings.get('type') != 'xiaoluban':
        raise ValueError('UNSUPPORTED_BACKEND')
    return Xiaoluban(settings)
