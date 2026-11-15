"""Explicit HTTP check of an already running fresh/local clone; no provider calls."""
import argparse
import csv
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    address = urlparse(args.url)
    if address.scheme != 'http' or address.hostname != '127.0.0.1' or address.username or address.password:
        raise SystemExit('The check only permits local HTTP on 127.0.0.1')
    csrf = None
    result = {}
    with httpx.Client(base_url=args.url, trust_env=False, timeout=5,
                      headers={'Origin': f'{address.scheme}://{address.netloc}'}) as client:
        try:
            for attempt in range(90):
                try:
                    response = client.get('/api/health')
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise SystemExit('Local server did not become ready')
            result['health'] = response.status_code
            result['frontend'] = client.get('/').status_code
            with (ROOT / 'data/local/accounts.csv').open(encoding='utf-8-sig', newline='') as stream:
                account = next(row for row in csv.DictReader(stream) if row['username'] == 'admin01')
            response = client.post('/api/auth/login', json={key: account[key] for key in ('username', 'password')})
            result['login'] = response.status_code
            response.raise_for_status()
            csrf = response.json()['csrf_token']
            response = client.get('/api/projects')
            response.raise_for_status()
            result['projects'] = len(response.json())
        finally:
            if csrf:
                result['logout'] = client.post('/api/auth/logout', headers={'X-CSRF-Token': csrf}).status_code
    print(json.dumps(result))
    return 0 if all(result.get(key) == 200 for key in ('health', 'frontend', 'login', 'logout')) and result.get('projects', 0) else 1


if __name__ == '__main__':
    raise SystemExit(main())
