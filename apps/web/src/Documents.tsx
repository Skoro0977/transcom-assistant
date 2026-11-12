import { useEffect, useRef, useState } from 'react';
import { Alert, App, Button, Card, Descriptions, Drawer, Empty, Input, Select, Space, Table, Tabs, Tag, Typography, Upload } from 'antd';
import { InboxOutlined, ReloadOutlined, DownloadOutlined, FileTextOutlined } from '@ant-design/icons';
import { api, post, stamp } from './api';

type Doc = { id: string; title: string; category: string; version: number; number: number;
  status: string; warning: string; size: number; created_at: string; lifecycle: string };
type Version = { id: string; number: number; filename: string; status: string; warning: string;
  text: string; text_truncated: boolean; metadata: Record<string, unknown>; created_at: string };
type Detail = { id: string; title: string; version: number; versions: Version[];
  attachments: { document_id: string; version_id: string; filename: string; parent_version_id: string }[];
  origins: { channel: string; source_name: string; received_at: string; version_id: string }[] };
export const statusLabels: Record<string, string> = { queued: 'В очереди', parsing: 'Разбор', ready: 'Текст извлечен',
  needs_preparation: 'Нужна подготовка', failed: 'Ошибка' };
export function Status({ value }: { value: string }) {
  return <Tag color={value === 'ready' ? 'green' : value === 'needs_preparation' ? 'gold' : 'blue'}>{statusLabels[value] || value}</Tag>;
}

export function Documents({ projectId, changed }: { projectId: string; changed: () => void }) {
  const { message } = App.useApp();
  const [items, setItems] = useState<Doc[]>([]), [total, setTotal] = useState(0);
  const [page, setPage] = useState(1), [lifecycle, setLifecycle] = useState('active');
  const [loading, setLoading] = useState(false), [refresh, setRefresh] = useState(0);
  const [detail, setDetail] = useState<Detail | null>(null), [revision, setRevision] = useState<number>();
  const [error, setError] = useState(''), [uploads, setUploads] = useState(0);
  const [filter, setFilter] = useState('');
  const openSerial = useRef(0);
  const busy = uploads > 0;
  useEffect(() => {
    let active = true; setLoading(true);
    api<{ items: Doc[]; total: number }>(`/api/projects/${projectId}/documents?limit=25&offset=${(page - 1) * 25}&lifecycle=${lifecycle}`)
      .then(data => { if (active) { setItems(data.items); setTotal(data.total); setError(''); } })
      .catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [projectId, page, lifecycle, refresh]);
  function reload() { setRefresh(n => n + 1); changed(); }
  async function open(id: string, versionId?: string) {
    const serial = ++openSerial.current;
    try { const data = await api<Detail>(`/api/documents/${id}`); if (serial !== openSerial.current) return;
      setDetail(data); setRevision((versionId ? data.versions.find(v => v.id === versionId) : data.versions[0])?.number); }
    catch (e) { message.error((e as Error).message); }
  }
  async function upload(file: File, target?: Detail) {
    if (file.size > 100 * 1024 * 1024) { message.error('Ограничение 100 МиБ'); return; }
    setUploads(n => n + 1);
    const form = new FormData(); form.append('file', file);
    const query = target ? `?document_id=${target.id}&expected_version=${target.version}` : '';
    try {
      const data = await api<{ duplicate: boolean }>(`/api/projects/${projectId}/documents${query}`, { method: 'POST', body: form });
      message.success(data.duplicate ? 'Повтор распознан, источник сохранен' : 'Файл принят в обработку');
      if (target) await open(target.id); reload();
    } catch (e) { message.error((e as Error).message); } finally { setUploads(n => n - 1); }
  }
  const selected = detail?.versions.find(v => v.number === revision);
  return <Space orientation="vertical" size={20} style={{ width: '100%' }}>
    {error && <Alert type="error" title={error} showIcon />}
    <Upload.Dragger disabled={busy || lifecycle !== 'active'} showUploadList={false} multiple
      beforeUpload={file => { void upload(file); return false; }}>
      <p className="ant-upload-drag-icon"><InboxOutlined /></p><Typography.Title level={5}>Добавьте документы и письма</Typography.Title>
      <Typography.Text type="secondary">PDF, Word, Excel, MSG, EML и изображения. Оригиналы сохраняются.</Typography.Text>
    </Upload.Dragger>
    <Card title={<Space><FileTextOutlined /> Библиотека источников <Tag>{total}</Tag></Space>}
      extra={<Button icon={<ReloadOutlined />} onClick={reload}>Обновить</Button>}>
      <div className="toolbar"><Select aria-label="Состояние архива" value={lifecycle} onChange={v => { setLifecycle(v); setPage(1); }}
        options={[{ value: 'active', label: 'В работе' }, { value: 'archived', label: 'Архив' }]} />
        <Input aria-label="Фильтр текущей страницы" value={filter} onChange={e => setFilter(e.target.value)} allowClear addonBefore="На странице" /></div>
      <Table rowKey="id" loading={loading} scroll={{ x: 850 }} dataSource={items.filter(d => d.title.toLowerCase().includes(filter.toLowerCase()))}
        pagination={{ current: page, pageSize: 25, total, showSizeChanger: false, onChange: setPage }} columns={[
          { title: 'Документ', dataIndex: 'title', render: (v: string, r: Doc) => <Button type="link" className="doc-title" onClick={() => void open(r.id)}>{v}</Button> },
          { title: 'Категория', dataIndex: 'category', width: 210 },
          { title: 'Редакция', dataIndex: 'number', width: 90 },
          { title: 'Обработка', dataIndex: 'status', width: 180, render: (v: string) => <Status value={v} /> },
          { title: 'Размер', dataIndex: 'size', width: 90, render: (v: number) => `${(v / 1048576).toFixed(1)} МиБ` },
        ]} />
    </Card>
    <Drawer title={detail?.title} open={!!detail} onClose={() => { openSerial.current++; setDetail(null); }} size={760}>
      {detail && selected ? <Space orientation="vertical" size={18} style={{ width: '100%' }}>
        <Space wrap><Select aria-label="Редакция документа" value={revision} onChange={setRevision} options={detail.versions.map(v => ({ value: v.number, label: `Редакция ${v.number} от ${stamp(v.created_at)}` }))} />
          <Button href={`/api/documents/${detail.id}/content?number=${revision}`} icon={<DownloadOutlined />}>Оригинал</Button>
          <Button onClick={() => void open(detail.id)} icon={<ReloadOutlined />}>Обновить</Button></Space>
        <Status value={selected.status} />
        <Button type="primary" disabled={selected.number !== detail.versions[0].number} onClick={() => void post<{ status: string }>(`/api/documents/${detail.id}/analyze`, { expected_version: detail.version }).then(result => message.success(result.status === 'done' ? 'Анализ уже выполнен. Откройте раздел проверки.' : 'Анализ поставлен в очередь. Предложения появятся в разделе проверки.')).catch(e => message.error(e.message))}>Проанализировать текущую редакцию через AI</Button>
        {selected.warning && <Alert title={selected.warning} type="warning" showIcon />}
        {selected.text_truncated && <Alert title="Показаны первые 200 000 символов. Полный документ доступен в оригинале." type="info" showIcon />}
        <Tabs items={[
          { key: 'text', label: 'Извлеченный текст', children: selected.text ? <pre className="source-text">{selected.text}</pre> : <Empty description="Текст пока не извлечен. Оригинал доступен для проверки." /> },
          { key: 'meta', label: 'Реквизиты и источник', children: <><Descriptions column={1} bordered size="small" items={Object.entries(selected.metadata).map(([k, v]) => ({ key: k, label: k, children: typeof v === 'string' ? v : JSON.stringify(v) }))} />
            <Typography.Title level={5}>Поступления этой редакции</Typography.Title>{detail.origins.filter(o => o.version_id === selected.id).map((o, i) => <p key={i}>{o.channel}: {o.source_name}<br /><Typography.Text type="secondary">{stamp(o.received_at)}</Typography.Text></p>)}</> },
          { key: 'files', label: 'Вложения', children: detail.attachments.filter(a => a.parent_version_id === selected.id).map(a => <p key={a.version_id}><Button type="link" onClick={() => void open(a.document_id, a.version_id)}>{a.filename}</Button></p>) },
        ]} />
        <Card size="small" title="Новая редакция">
          <Typography.Paragraph type="secondary">Загрузите измененный файл. Предыдущие редакции и оригиналы останутся в истории.</Typography.Paragraph>
          <Upload showUploadList={false} disabled={busy} beforeUpload={file => { void upload(file, detail); return false; }}><Button loading={busy}>Загрузить редакцию</Button></Upload>
          <Button style={{ marginLeft: 8 }} onClick={() => void post(`/api/documents/${detail.id}/reparse`, { expected_version: detail.version }).then(() => { message.success('Разбор поставлен в очередь'); reload(); return open(detail.id); }).catch(e => message.error(e.message))}>Повторить разбор</Button>
        </Card>
      </Space> : <Empty />}
    </Drawer>
  </Space>;
}
