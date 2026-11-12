"""Deterministic DOCX, no document/AI instructions or external resources executed."""
import io
import re
from datetime import datetime, timedelta, timezone

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

KINDS = {'requirement': 'Требование', 'stage': 'Этап', 'task': 'Задача', 'risk': 'Риск', 'event': 'Событие'}
FIELDS = {'title': 'Название', 'text': 'Текст', 'scope': 'Область', 'due_date': 'Срок',
    'source_date': 'Дата источника', 'owner': 'Ответственный', 'status': 'Действие записи',
    'stage_state': 'Состояние этапа', 'task_state': 'Состояние задачи', 'risk_state': 'Состояние риска',
    'category': 'Категория', 'priority': 'Приоритет', 'stage_id': 'Связанный этап',
    'requirement_id': 'Связанное требование', 'depends_on': 'Зависимые задачи', 'event_type': 'Вид события'}
LABELS = {'active': 'Действует', 'cancelled': 'Отменено', 'no_data': 'Нет данных', 'received': 'Получено',
    'correction': 'На доработке', 'customer_review': 'На проверке заказчиком', 'approved': 'Согласовано',
    'open': 'Открыто', 'in_progress': 'В работе', 'blocked': 'Заблокировано', 'done': 'Выполнено',
    'mitigation': 'Снижается', 'closed': 'Закрыто', 'critical': 'Критический', 'high': 'Высокий',
    'medium': 'Средний', 'low': 'Низкий', 'ready': 'Текст извлечен', 'needs_preparation': 'Нужна подготовка',
    'queued': 'В очереди', 'parsing': 'Обработка', 'failed': 'Ошибка', 'pending': 'Требует проверки',
    'accepted': 'Подтверждено', 'rejected': 'Отклонено'}


def clean(value):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]', '', '' if value is None else str(value)).replace('—', '-').replace('«', '"').replace('»', '"')


def moscow_stamp(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')


def table_text(value):
    # Word and LibreOffice may treat an entire underscore-separated filename as
    # one word and paint it over adjacent cells. Optional break opportunities
    # preserve the visible filename without shrinking the table font.
    text = clean(value)
    text = re.sub(r'([_/.\\-])', lambda match: match[0] + '\u200b', text)
    return re.sub(r'[^\s\u200b]{22,}', lambda match: '\u200b'.join(
        match[0][i:i + 20] for i in range(0, len(match[0]), 20)), text)


def docx_bytes(summary):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = section.right_margin = Cm(2)
    for name in ('Normal', 'Title', 'Subtitle', 'Heading 1', 'Heading 2'):
        style = doc.styles[name]
        style.font.name = 'Arial'
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(11 if name == 'Normal' else 22 if name == 'Title' else 15 if name == 'Heading 1' else 12)
        style.paragraph_format.space_after = Pt(8)
        for border in style.element.xpath('.//w:pBdr'):
            border.getparent().remove(border)
    doc.styles['Normal'].paragraph_format.line_spacing = 1.15
    doc.core_properties.author = 'Контур проекта'
    doc.core_properties.title = 'Состояние и изменения проекта'
    doc.add_paragraph('Состояние и изменения проекта', 'Title')
    doc.add_paragraph(clean(summary['project']['title']), 'Subtitle')
    period = summary['period']
    basis = {'registered': 'регистрации', 'source': 'источника', 'confirmed': 'подтверждения'}[period['basis']]
    doc.add_paragraph(f'Период по дате {basis}: {period["start"] or "без ограничения"} - {period["end"] or "без ограничения"}.')
    doc.add_paragraph(f'Проверка сроков на {summary["as_of"]}.')
    doc.add_paragraph('Сформировано ' + moscow_stamp(summary['generated_at']))
    doc.add_paragraph(clean(summary['notice']))

    def para(value):
        doc.add_paragraph(clean(value))

    def table(headers, rows, widths):
        t = doc.add_table(rows=1, cols=len(headers))
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.autofit = False
        for col, width in zip(t.columns, widths):
            col.width = Cm(width)
        for i, text in enumerate(headers):
            t.rows[0].cells[i].text = text
        repeat = OxmlElement('w:tblHeader')
        t.rows[0]._tr.get_or_add_trPr().append(repeat)
        for row in rows:
            cells = t.add_row().cells
            for cell, text in zip(cells, row):
                cell.text = table_text(text)
        for n, row in enumerate(t.rows):
            row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
            for cell, width in zip(row.cells, widths):
                cell.width = Cm(width)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                properties = cell._tc.get_or_add_tcPr()
                borders = OxmlElement('w:tcBorders')
                for side in ('top', 'left', 'bottom', 'right'):
                    edge = OxmlElement('w:' + side)
                    for key, value in (('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')):
                        edge.set(qn('w:' + key), value)
                    borders.append(edge)
                properties.append(borders)
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), 'E8EDF2' if n == 0 else 'F7F9FB' if n % 2 == 0 else 'FFFFFF')
                properties.append(shade)
                margins = OxmlElement('w:tcMar')
                for side in ('top', 'left', 'bottom', 'right'):
                    margin = OxmlElement('w:' + side)
                    margin.set(qn('w:w'), '100')
                    margin.set(qn('w:type'), 'dxa')
                    margins.append(margin)
                properties.append(margins)
                for paragraph in cell.paragraphs:
                    paragraph.paragraph_format.space_after = Pt(4)
                    if width <= 4:
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in paragraph.runs:
                        run.font.size = Pt(10)
                        run.font.bold = n == 0
        doc.add_paragraph()

    doc.add_heading('Полнота и обработка документов', 1)
    para('Активных документов: ' + str(sum(summary['document_counts'].values())) +
         '. Нужна подготовка или обработка: ' + str(len(summary['preparation'])) + '.')
    para('Обязательные категории: ' + (', '.join(summary['required_categories']) or 'Перечень не задан'))
    para('Отсутствуют категории: ' + (', '.join(summary['missing_categories']) or 'Не выявлены по заданному перечню'))
    if summary['preparation']:
        table(['Документ', 'Обработка', 'Причина'], [(r['title'], LABELS.get(r['status'], r['status']), r['warning'])
            for r in summary['preparation']], [8, 3, 6])
    doc.add_heading('Текущее подтвержденное состояние', 1)
    if not summary['state']:
        para('Подтвержденных предметных записей пока нет.')
    titles = {row['id']: row['data']['title'] for row in summary['state']}

    def value(key, item):
        if item is None:
            return 'Нет данных'
        if key in ('stage_id', 'requirement_id'):
            return titles.get(item, 'Нет данных')
        if key == 'depends_on':
            return ', '.join(titles.get(i, 'Нет данных') for i in item) or 'Нет'
        return LABELS.get(str(item), str(item)) if item is not None else 'Нет данных'

    def facts(data):
        for key, item in data.items():
            if key != 'title' and item not in ('', None, []):
                para(FIELDS.get(key, key) + ': ' + value(key, item))

    for row in summary['state']:
        doc.add_heading(clean(KINDS.get(row['kind'], row['kind']) + ' ' + row['data']['title']), 2)
        para('Редакция ' + str(row['version']))
        facts(row['data'])
    doc.add_heading('Изменения за период', 1)
    if not summary['changes']:
        para('Подтвержденных изменений в выбранном периоде нет.')
    for row in summary['changes']:
        doc.add_heading(clean(row['data']['title']), 2)
        para(f'Редакция {row["number"]}. {moscow_stamp(row["created_at"])}. {row["author"]}. Основание решения: {row["reason"] or "не указано"}.')
        for key, change in row['changes'].items():
            para(f'{FIELDS.get(key, key)}: {value(key, change["before"])} -> {value(key, change["after"])}')
        for evidence in row['evidence']:
            if evidence.get('removed'):
                para('Источник окончательно удален, цитата недоступна.')
            else:
                para('Основание: ' + evidence.get('title', 'Документ') + ' / ред. ' + str(evidence.get('number') or '?') +
                     '. ' + evidence.get('locator', '') + '. ' + evidence.get('quote', ''))
    doc.add_heading('Предложения и противоречия за период', 1)
    if summary['proposals']:
        table(['Запись', 'Проверка', 'Изменение или противоречие'], [(r['title'], LABELS[r['status']],
            'Да' if r['conflict'] else 'Нет') for r in summary['proposals']], [9, 4, 4])
    else:
        para('Предложений в выбранной выборке нет.')
    doc.add_heading('Поступления документов за период', 1)
    if summary['receipts']:
        table(['Документ и редакция', 'Дата регистрации', 'Дата источника'],
            [(r['title'] + ' / ' + str(r['number']), datetime.fromisoformat(r['registered_at']).astimezone(
                timezone(timedelta(hours=3))).date().isoformat(), r['source_date'] or 'Неизвестна')
             for r in summary['receipts']], [10, 3.5, 3.5])
    else:
        para('Поступлений в выбранной выборке нет.')
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()
