"""Small server-only GigaChat adapter. No credentials or response bodies in errors."""
import math
import ssl
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx

from .settings import settings

API = 'https://api.giga.chat/v1'
OAUTH = 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'


class ProviderError(Exception):
    pass


class GigaChat:
    def __init__(self):
        self._token = ''
        self._expires = 0
        self._lock = threading.Lock()

    def _json(self, method, url, **kwargs):
        try:
            context = ssl.create_default_context()
            if settings.gigachat_ca_file:
                path = Path(settings.gigachat_ca_file).resolve()
                if path.drive and path.drive.upper() != 'D:':
                    raise ProviderError('Сертификат приложения должен находиться на D')
                context.load_verify_locations(cafile=str(path))
            with httpx.Client(verify=context, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(60, connect=10)) as client, \
                    client.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    return response.status_code, None
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ProviderError('Ответ GigaChat превышает допустимый размер')
                import json
                return 200, json.loads(raw)
        except (httpx.HTTPError, ssl.SSLError, OSError, ValueError) as error:
            raise ProviderError('Нет безопасного соединения с GigaChat: ' + type(error).__name__) from None

    def token(self, refresh=False):
        with self._lock:
            if self._token and self._expires > time.time() + 60 and not refresh:
                return self._token
            key = settings.gigachat_auth_key.get_secret_value()
            if not key:
                raise ProviderError('GigaChat не настроен: задайте GIGACHAT_AUTH_KEY в .env')
            status, data = self._json('POST', OAUTH, headers={'Authorization': 'Basic ' + key,
                                      'RqUID': str(uuid4()), 'Accept': 'application/json'},
                                      data={'scope': settings.gigachat_scope})
            if status != 200:
                raise ProviderError(f'GigaChat OAuth вернул HTTP {status}; проверьте ключ и scope')
            try:
                self._token = data['access_token']
                expires = float(data['expires_at'])
                self._expires = expires / 1000 if expires > 100_000_000_000 else expires
                if not isinstance(self._token, str) or not self._token:
                    raise ValueError()
            except (TypeError, ValueError, KeyError):
                self._token = ''
                raise ProviderError('Неверный ответ авторизации GigaChat') from None
            return self._token

    def request(self, method, path, payload=None):
        # path is supplied by this adapter only, never by a request or document.
        for attempt in range(2):
            token = self.token(refresh=attempt == 1)
            status, data = self._json(method, API + path, headers={'Authorization': 'Bearer ' + token},
                                      **({'json': payload} if payload is not None else {}))
            if status == 200:
                return data
            if status != 401 or attempt:
                raise ProviderError(f'GigaChat вернул HTTP {status}; данные не изменены')
        raise ProviderError('Ошибка авторизации GigaChat')

    def models(self):
        return self.request('GET', '/models')

    def chat(self, system: str, text: str, max_tokens=1800):
        if len(system) + len(text) > 60000:
            raise ProviderError('Контекст запроса слишком большой, выберите меньший набор источников')
        data = self.request('POST', '/chat/completions', {
            'model': settings.gigachat_model, 'messages': [{'role': 'system', 'content': system},
                                                         {'role': 'user', 'content': text}],
            'temperature': 0.1, 'max_tokens': max_tokens, 'stream': False})
        try:
            choice = data['choices'][0]
            content = choice['message']['content']
            if choice.get('finish_reason') == 'length' or not isinstance(content, str) or not content.strip():
                raise ValueError()
            return content
        except (TypeError, KeyError, IndexError, ValueError):
            raise ProviderError('GigaChat вернул неполный или некорректный ответ') from None

    def embeddings(self, texts: list[str]):
        if not 1 <= len(texts) <= 16 or any(not t or len(t) > 6000 for t in texts):
            raise ProviderError('Неверный размер пакета эмбеддингов')
        data = self.request('POST', '/embeddings', {'model': settings.gigachat_embedding_model, 'input': texts})
        try:
            rows = sorted(data['data'], key=lambda row: row['index'])
            if [r['index'] for r in rows] != list(range(len(texts))):
                raise ValueError()
            vectors = [r['embedding'] for r in rows]
            if (not vectors or not 1 <= len(vectors[0]) <= 8192
                    or any(len(v) != len(vectors[0]) or not all(isinstance(x, (int, float)) and math.isfinite(x)
                                                              for x in v) for v in vectors)):
                raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError):
            raise ProviderError('GigaChat вернул некорректные эмбеддинги') from None


giga = GigaChat()
