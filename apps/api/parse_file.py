"""Isolated file parser. Does not run macros, scripts or external requests."""
import email
import json
import re
import subprocess
import sys
import zipfile
from email import policy
from pathlib import Path
from time import monotonic
from uuid import uuid4

MAX_TEXT = 2_000_000
MAX_EXPANDED = 200 * 1024 * 1024


def parse(path: Path, output: Path):
    started = monotonic()
    suffix = path.suffix.lower()
    segments, metadata, attachments, warnings = [], {}, [], []
    length = 0

    def add(locator, text, quoted=False):
        nonlocal length
        text = str(text or '').replace('\x00', '').strip()
        if not text:
            return
        remaining = MAX_TEXT - length
        if remaining <= 0:
            if 'Текст превышает предел извлечения' not in warnings:
                warnings.append('Текст превышает предел извлечения')
            return
        if len(text) > remaining:
            warnings.append('Текст превышает предел извлечения')
        segments.append({'locator': locator, 'text': text[:remaining], 'quoted': quoted})
        length += min(len(text), remaining)

    def attachment(name, content):
        if len(attachments) >= 100 or sum(a['bytes'] for a in attachments) + len(content) > MAX_EXPANDED:
            warnings.append('Объем или число вложений превышает предел')
            return
        name = re.sub(r'[\x00-\x1f]', '', str(name or 'attachment.bin').replace('\\', '/').rsplit('/', 1)[-1])[:240]
        target = output / (uuid4().hex + Path(name).suffix.lower())
        target.write_bytes(content)
        attachments.append({'name': name, 'path': str(target), 'bytes': len(content)})

    def mail_body(text):
        match = re.search(r'(?im)^(?:From:|От:|[-_]{4,}.*(?:сообщ|message)|On .+ wrote:)', text)
        cut = match.start() if match else len(text)
        add('Тело письма', text[:cut])
        if cut < len(text):
            add('Цитируемая переписка', text[cut:], quoted=True)

    if suffix in {'.docx', '.xlsx'}:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (len(members) > 20000 or sum(p.file_size for p in members) > MAX_EXPANDED
                    or any(p.file_size > max(1, p.compress_size) * 1000 for p in members)):
                raise ValueError('Office archive expansion limit')
            if any('vbaProject' in p.filename or '/embeddings/' in p.filename for p in members):
                metadata['active_content_ignored'] = True

    if suffix == '.pdf':
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF is encrypted')
        if len(reader.pages) > 1000:
            raise ValueError('PDF page limit')
        scans = []
        for number, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ''
            except PyPdfError:
                warnings.append(f'Страница {number} требует отдельной подготовки: предел безопасного разбора')
                continue
            add(f'PDF, страница {number}', text)
            if len(text.strip()) < 20:
                scans.append(number)
        metadata.update(pages=len(reader.pages), scan_pages=scans)
        if scans:
            from .local_tools import ocr_image, pdf_image
            recognized = []
            for number in scans[:20]:
                remaining = 180 - (monotonic() - started)
                if remaining < 5:
                    warnings.append('Достигнут общий бюджет OCR, сохранен частичный текст')
                    break
                try:
                    image = pdf_image(path, number, output)
                    text, confidence = ocr_image(image, output, timeout=min(45, remaining))
                    add(f'PDF OCR, страница {number}, уверенность {confidence:.0f}%', text)
                    if text.strip():
                        recognized.append(number)
                    if confidence < 65:
                        warnings.append(f'OCR страницы {number} требует сверки, уверенность {confidence:.0f}%')
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    warnings.append(f'OCR страницы {number} не выполнен, доступен оригинал')
            metadata['ocr_pages'] = recognized
            metadata['ocr_review_required'] = bool(recognized)
            metadata['scan_pages'] = [n for n in scans if n not in recognized]
            if metadata['scan_pages']:
                warnings.append(f'Осталось страниц без текста: {len(metadata["scan_pages"])}')
    elif suffix == '.doc':
        from .local_tools import legacy_word
        add('DOC, текстовые блоки Word', legacy_word(path))
        metadata['legacy_policy'] = 'Текстовые блоки Word97+, без выполнения полей, объектов и макросов; сложная верстка требует сверки'
    elif suffix == '.docx':
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = Document(path)
        for number, child in enumerate(document.element.body.iterchildren(), 1):
            if child.tag.endswith('}p'):
                add(f'DOCX, блок {number}', Paragraph(child, document).text)
            elif child.tag.endswith('}tbl'):
                rows = Table(child, document).rows
                for row_number, row in enumerate(rows, 1):
                    add(f'DOCX, таблица {number}, строка {row_number}', ' | '.join(c.text for c in row.cells))
    elif suffix == '.xlsx':
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        formulas = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        missing_cache = 0
        try:
            for sheet in workbook.worksheets:
                count = 0
                for row_number, (row, formula_row) in enumerate(
                        zip(sheet.iter_rows(), formulas[sheet.title].iter_rows(), strict=True), 1):
                    count += len(row)
                    if count > 250000:
                        warnings.append('Лист превышает предел извлечения ячеек')
                        break
                    values = [f'{c.coordinate}: {c.value}' for c in row if c.value is not None]
                    missing_cache += sum(f.data_type == 'f' and c.value is None
                                         for c, f in zip(row, formula_row, strict=True))
                    if values:
                        add(f'XLSX, лист {sheet.title}, строка {row_number}', ' | '.join(values))
            metadata['sheets'] = workbook.sheetnames
            metadata['formula_policy'] = 'Только сохраненные значения, формулы не вычисляются'
            metadata['formulas_without_cache'] = missing_cache
            if missing_cache:
                warnings.append(f'Нет сохраненного результата формул: {missing_cache} ячеек')
        finally:
            workbook.close()
            formulas.close()
    elif suffix == '.xls':
        import xlrd
        workbook = xlrd.open_workbook(path, on_demand=True)
        try:
            for sheet in workbook.sheets():
                if sheet.nrows * sheet.ncols > 250000:
                    warnings.append('Лист превышает предел извлечения ячеек')
                    continue
                for number in range(sheet.nrows):
                    values = [f'{index + 1}: {cell.value}' for index, cell in enumerate(sheet.row(number)) if cell.ctype != xlrd.XL_CELL_EMPTY]
                    add(f'XLS, лист {sheet.name}, строка {number + 1}', ' | '.join(values))
            metadata['formula_policy'] = 'Только сохраненные значения XLS, формулы и макросы не выполняются'
        finally:
            workbook.release_resources()
    elif suffix == '.msg':
        import extract_msg
        message = extract_msg.openMsg(str(path))
        try:
            metadata.update(subject=str(message.subject or ''), sender=str(message.sender or ''),
                            recipients=str(message.to or ''), cc=str(message.cc or ''),
                            date=str(message.date or ''), message_id=str(message.header.get('Message-ID', '')),
                            in_reply_to=str(message.header.get('In-Reply-To', '')),
                            references=str(message.header.get('References', '')))
            add('Тема письма', metadata['subject'])
            mail_body(str(message.body or ''))
            for item in message.attachments:
                value = item.data
                name = item.longFilename or item.shortFilename or 'attachment.bin'
                if isinstance(value, bytes):
                    attachment(name, value)
                elif hasattr(value, 'exportBytes'):
                    attachment(str(name).removesuffix('.msg') + '.msg', value.exportBytes())
                else:
                    warnings.append('Не удалось извлечь один из типов вложений')
        finally:
            message.close()
    elif suffix == '.eml':
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        metadata.update(subject=str(message.get('Subject', '')), sender=str(message.get('From', '')),
                        recipients=str(message.get('To', '')), date=str(message.get('Date', '')),
                        message_id=str(message.get('Message-ID', '')),
                        in_reply_to=str(message.get('In-Reply-To', '')), references=str(message.get('References', '')))
        add('Тема письма', metadata['subject'])
        body = message.get_body(preferencelist=('plain', 'html'))
        if body:
            text = body.get_content()
            if body.get_content_type() == 'text/html':
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(text, 'html.parser')
                for tag in soup(['script', 'style', 'iframe', 'object']):
                    tag.decompose()
                text = soup.get_text('\n')
            mail_body(str(text))
        for item in message.iter_attachments():
            if item.get_content_type() == 'message/rfc822':
                for nested in item.get_payload():
                    attachment(item.get_filename() or 'message.eml', nested.as_bytes())
            else:
                attachment(item.get_filename(), item.get_payload(decode=True) or b'')
    elif suffix in {'.txt', '.csv', '.log', '.rtf'}:
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1251', errors='replace')
        if suffix == '.rtf':
            from striprtf.striprtf import rtf_to_text
            text = rtf_to_text(text, encoding='cp1251', errors='replace')
        add('Текст файла', text)
    elif suffix in {'.png', '.jpg', '.jpeg'}:
        from .local_tools import ocr_image
        text, confidence = ocr_image(path, output)
        add(f'OCR изображения, уверенность {confidence:.0f}%', text)
        metadata.update(ocr_confidence=confidence, ocr_review_required=True)
        if confidence < 65:
            warnings.append('Распознанный текст требует сверки с оригиналом')
    elif suffix == '.kml':
        from defusedxml.ElementTree import parse as xml_parse
        root = xml_parse(path).getroot()
        for number, place in enumerate(root.iter('{http://www.opengis.net/kml/2.2}Placemark'), 1):
            values = [element.text for element in place.iter() if element.tag.rsplit('}', 1)[-1] in {'name', 'description'} and element.text]
            add(f'KML, объект {number}', '\n'.join(values))
        warnings.append('Извлечены подписи KML; геометрия и инженерные параметры не проверены')
    elif suffix == '.dxf':
        raw = path.read_bytes()
        if raw.startswith(b'AutoCAD Binary DXF'):
            raise ValueError('Binary DXF requires preparation')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1251', errors='replace')
        lines, entity, values, number = text.splitlines(), '', [], 0
        for index in range(0, len(lines) - 1, 2):
            code, value = lines[index].strip(), lines[index + 1].strip()
            if code == '0':
                if values:
                    number += 1
                    add(f'DXF, подпись {number}', ''.join(values).replace('\\P', '\n'))
                entity, values = value, []
            elif entity in {'TEXT', 'MTEXT', 'ATTRIB', 'ATTDEF'} and code in {'1', '3'}:
                values.append(value)
        if values:
            add(f'DXF, подпись {number + 1}', ''.join(values).replace('\\P', '\n'))
        warnings.append('Извлечены только подписи DXF; геометрия, масштаб и инженерные параметры не проверены')
    else:
        warnings.append('Нужна отдельная подготовка/OCR или просмотр инженерного исходника')
    return {'segments': segments, 'metadata': metadata, 'attachments': attachments,
            'warning': '; '.join(warnings), 'status': 'ready' if segments and not warnings else 'needs_preparation'}


def main():
    from .parser_limits import limit_memory
    limit_memory()
    path, output = Path(sys.argv[1]), Path(sys.argv[2])
    output.mkdir(exist_ok=True)
    result = parse(path, output)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
