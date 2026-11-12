import time

from pydantic import SecretStr

from apps.api.gigachat import GigaChat
from apps.api.settings import settings


def test_expired_provider_token_is_refreshed_once(monkeypatch):
    monkeypatch.setattr(settings, 'gigachat_auth_key', SecretStr('synthetic-test-key'))
    adapter = GigaChat()
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        if url.endswith('/oauth'):
            return 200, {'access_token': 'synthetic-token', 'expires_at': (time.time() + 1800) * 1000}
        if len(calls) == 2:
            return 401, None
        return 200, {'data': []}

    monkeypatch.setattr(adapter, '_json', request)
    assert adapter.models() == {'data': []}
    assert len(calls) == 4
    assert sum(url.endswith('/oauth') for url in calls) == 2


def test_standalone_check_creates_report_directory(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import check_gigachat
    monkeypatch.setattr(check_gigachat, 'DATA', tmp_path)
    monkeypatch.setattr(check_gigachat, 'giga', SimpleNamespace(
        token=lambda: None, models=lambda: {'data': [{'id': 'synthetic'}]},
        chat=lambda *args, **kwargs: 'работает', embeddings=lambda _: [[0.1, 0.2]]))
    assert check_gigachat.main() == 0
    assert (tmp_path / 'local' / 'gigachat-check.json').is_file()
