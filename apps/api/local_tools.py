"""Bounded local-only OCR and legacy Word conversion."""
import json
import os
import struct
import subprocess
from contextlib import closing
from itertools import pairwise
from pathlib import Path

from .paths import is_storage_path
from .settings import DATA, ROOT, settings


def executable(value):
    path = Path(value).resolve()
    if not path.is_file() or not is_storage_path(path):
        raise ValueError('Local tool must exist on the configured local drive')
    return path


def ocr_image(image, output, timeout=45):
    from PIL import Image
    with Image.open(image) as picture:
        if picture.width * picture.height > 20_000_000:
            raise ValueError('OCR image pixel limit')
    target = output / 'ocr-result.json'
    cache = DATA / 'local' / 'ocr-cache'
    cache.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(executable(settings.ocr_node)), str(ROOT / 'tools/ocr/recognize.cjs'),
        str(image), str(target), str(cache)], cwd=ROOT, timeout=timeout, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode or not target.is_file() or target.stat().st_size > 12_000_000:
        raise ValueError('Local OCR failed')
    value = json.loads(target.read_text(encoding='utf-8'))
    target.unlink()
    return str(value['text']), float(value['confidence'])


def pdf_image(path, number, output):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(path) as document, closing(document[number - 1]) as page:
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            raise ValueError('Invalid PDF page size')
        scale = min(2.5, (12_000_000 / (width * height)) ** 0.5)
        bitmap = page.render(scale=scale, may_draw_forms=False)
        try:
            picture = bitmap.to_pil()
            try:
                target = output / 'ocr-page.png'
                picture.save(target)
                return target
            finally:
                picture.close()
        finally:
            bitmap.close()


def word_pieces(word, table):
    """MS-DOC 2.4.1: read only text pieces, never fields, objects or macros."""
    def u16(offset):
        return struct.unpack_from('<H', word, offset)[0]

    if len(word) < 426 or u16(0) != 0xA5EC or u16(2) < 0xC1 or u16(10) & 0x8100:
        raise ValueError('Unsupported or encrypted Word FIB')
    cursor = 34 + 2 * u16(32)
    cursor += 2 + 4 * u16(cursor)
    if u16(cursor) < 34:
        raise ValueError('Missing Word CLX')
    offset, size = struct.unpack_from('<II', word, cursor + 2 + 33 * 8)
    if size < 5 or offset + size > len(table):
        raise ValueError('Invalid Word CLX bounds')
    clx, cursor = table[offset:offset + size], 0
    while cursor < len(clx) and clx[cursor] == 1:
        cursor += 3 + struct.unpack_from('<H', clx, cursor + 1)[0]
    if cursor + 5 > len(clx) or clx[cursor] != 2:
        raise ValueError('Missing Word Pcdt')
    size = struct.unpack_from('<I', clx, cursor + 1)[0]
    pieces = clx[cursor + 5:cursor + 5 + size]
    if len(pieces) != size or size < 4 or (size - 4) % 12:
        raise ValueError('Invalid Word piece table')
    count = (size - 4) // 12
    if count > 100000:
        raise ValueError('Too many Word pieces')
    positions = struct.unpack_from('<' + 'I' * (count + 1), pieces)
    if positions[0] != 0 or positions[-1] > 2_000_000 or any(a > b for a, b in pairwise(positions)):
        raise ValueError('Word character limit')
    texts = []
    for index in range(count):
        pointer = struct.unpack_from('<I', pieces, 4 * (count + 1) + index * 8 + 2)[0]
        compressed = bool(pointer & 0x40000000)
        start = (pointer & 0x3FFFFFFF) // (2 if compressed else 1)
        length = (positions[index + 1] - positions[index]) * (1 if compressed else 2)
        if start + length > len(word):
            raise ValueError('Word piece outside stream')
        texts.append(word[start:start + length].decode('cp1252' if compressed else 'utf-16-le', errors='replace'))
    import re
    text = re.sub(r'\x13[^\x14\x15]*(?:\x14([^\x15]*))?\x15', lambda m: m[1] or '', ''.join(texts))
    return re.sub(r'[\x00-\x08\x0b\x0e-\x1f]', '', text.replace('\x07', '\t').replace('\r', '\n'))


def legacy_word(path):
    import olefile
    with olefile.OleFileIO(path) as office:
        word = office.openstream('WordDocument').read()
        if len(word) < 12:
            raise ValueError('Invalid Word stream')
        flags = struct.unpack_from('<H', word, 10)[0]
        table = office.openstream('1Table' if flags & 0x200 else '0Table').read()
    return word_pieces(word, table)
