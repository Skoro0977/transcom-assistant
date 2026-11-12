"""Read-only inventory. Source contents stay outside Git."""
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'source_archive'

def main():
    rows = []
    for path in sorted(SOURCE.rglob('*')):
        if path.is_file():
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            rows.append({'path': path.relative_to(SOURCE).as_posix(),
                         'bytes': path.stat().st_size, 'sha256': digest,
                         'extension': path.suffix.lower()})
    (ROOT / 'data' / 'source_inventory.json').write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'files': len(rows), 'bytes': sum(r['bytes'] for r in rows),
               'formats': dict(Counter(r['extension'] for r in rows)),
               'duplicate_copies': len(rows) - len({r['sha256'] for r in rows}),
               'categories': sorted(p.name for p in SOURCE.iterdir() if p.is_dir())}
    (ROOT / 'data' / 'inventory_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=True))

if __name__ == '__main__':
    main()
