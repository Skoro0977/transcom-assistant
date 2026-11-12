"""Explicit real provider smoke check; sends synthetic text only."""
import json
import time

from apps.api.db import utcnow
from apps.api.gigachat import ProviderError, giga
from apps.api.settings import DATA, settings


def main():
    (DATA / 'local').mkdir(parents=True, exist_ok=True)
    report = {'checked_at': utcnow(), 'model': settings.gigachat_model, 'scope': settings.gigachat_scope,
              'oauth': False, 'models': False, 'generation': False, 'embeddings': False}
    start = time.monotonic()
    try:
        giga.token()
        report['oauth'] = True
        report['models'] = bool(giga.models().get('data'))
        answer = giga.chat('Ответь одним словом: работает.', 'Проверка связи учебного проекта.', max_tokens=80)
        report['generation'] = bool(answer.strip())
        vector = giga.embeddings(['Проверка поиска по проекту.'])[0]
        report['embeddings'] = True
        report['embedding_dimensions'] = len(vector)
    except ProviderError as error:
        report['error'] = str(error)
    report['seconds'] = round(time.monotonic() - start, 2)
    path = DATA / 'local' / 'gigachat-check.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(report[k] for k in ('oauth', 'models', 'generation', 'embeddings')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
