import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Empty, Form, Input, Modal, Pagination, Select, Space, Tag, Typography } from 'antd';
import { CheckOutlined, CloseOutlined, EditOutlined, ReloadOutlined } from '@ant-design/icons';
import { api, post, stamp } from './api';

export type Evidence = { version_id: string; document_id: string; locator: string; quote: string };
export type Fact = { title: string; text: string; scope: string; due_date: string | null;
  source_date: string | null; owner: string; status: string };
export type Accepted = { id: string; version: number; kind: string; data: Fact; evidence: Evidence[] };
type Proposal = Accepted & { target_id: string | null; base_version: number; conflict: boolean; note: string;
  origin: string; created_at: string; status: string; reason: string };
type Job = { id: string; kind: string; status: string; cursor: number; error: string };
export function EvidenceList({ items }: { items: Evidence[] }) {
  return <>{!items.length && <Typography.Text type="secondary">Ручное решение, документальное основание не указано</Typography.Text>}{items.map((e, i) => <div className="evidence" key={i}><Typography.Text strong>{e.locator}</Typography.Text><blockquote>{e.quote}</blockquote><a href={`/api/document-versions/${e.version_id}/content`}>Оригинал этой редакции</a></div>)}</>;
}
export function FactView({ data }: { data: Fact }) { return <><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{data.text}</Typography.Paragraph><Space wrap>{data.scope && <Tag>{data.scope}</Tag>}{data.due_date && <Tag color="blue">Срок: {data.due_date}</Tag>}{data.source_date && <Tag>Дата источника: {data.source_date}</Tag>}{data.owner && <Tag>Ответственный: {data.owner}</Tag>}{data.status === 'cancelled' && <Tag color="red">Отменено</Tag>}</Space></>; }
export function Review({ projectId, accepted = false }: { projectId: string; accepted?: boolean }) {
  const { message } = App.useApp(); const [form] = Form.useForm();
  const [items, setItems] = useState<Proposal[]>([]), [records, setRecords] = useState<Accepted[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]), [status, setStatus] = useState('pending');
  const [selected, setSelected] = useState<string[]>([]), [refresh, setRefresh] = useState(0);
  const [page, setPage] = useState(1), [total, setTotal] = useState(0), [error, setError] = useState('');
  const [editing, setEditing] = useState<Proposal | null>(null), [create, setCreate] = useState(false), [saving, setSaving] = useState(false);
  const [decision, setDecision] = useState<{ proposal?: Proposal; value: string } | null>(null), [reason, setReason] = useState('');
  function reload() { setRefresh(n => n + 1); setSelected([]); }
  useEffect(() => {
    let active = true;
    Promise.all([api<{ items: Proposal[]; total: number }>(`/api/projects/${projectId}/proposals?status=${status}&limit=25&offset=${(page - 1) * 25}`),
      api<Accepted[]>(`/api/projects/${projectId}/requirements`), api<Job[]>(`/api/projects/${projectId}/jobs`)])
      .then(([p, r, j]) => { if (active) { setItems(p.items); setTotal(p.total); setRecords(r); setJobs(j); setError(''); } })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId, status, page, refresh]);
  async function decide() {
    if (!decision) return; setSaving(true);
    try {
      if (decision.proposal) await post(`/api/proposals/${decision.proposal.id}/decision`, { decision: decision.value, expected_version: decision.proposal.version, reason });
      else await post(`/api/projects/${projectId}/decisions`, { decision: decision.value, reason, items: items.filter(p => selected.includes(p.id)).map(p => ({ id: p.id, expected_version: p.version })) });
      message.success('Решение сохранено'); setDecision(null); reload();
    } catch (e) { message.error((e as Error).message); } finally { setSaving(false); }
  }
  return <Space orientation="vertical" size={18} style={{ width: '100%' }}>
    {error && <Alert type="error" title={error} showIcon />}
    <div className="toolbar"><Button icon={<ReloadOutlined />} onClick={reload}>Обновить</Button><Button type="primary" onClick={() => { setEditing(null); form.resetFields(); setCreate(true); }}>Предложить требование</Button>
      {!accepted && <><Select aria-label="Состояние проверки" value={status} onChange={v => { setStatus(v); setPage(1); setSelected([]); }} options={[{ value: 'pending', label: 'Требует проверки' }, { value: 'accepted', label: 'Подтверждено' }, { value: 'rejected', label: 'Отклонено' }]} />
        {status === 'pending' && <Button disabled={!selected.length} onClick={() => { setReason(''); setDecision({ value: 'accepted' }); }}>Подтвердить выбранные ({selected.length})</Button>}</>}
    </div>
    {!accepted && jobs.filter(j => j.status !== 'done' || j.error).map(j => <Alert key={j.id} showIcon type={j.status === 'failed' ? 'error' : 'info'} title={`Анализ: ${j.status}, обработано фрагментов: ${j.cursor}`} description={j.error} />)}
    {(accepted ? records : items).length === 0 && <Empty description={accepted ? 'Подтвержденных требований пока нет' : 'Предложений в этой выборке нет'} />}
    {accepted ? records.map(r => <Card key={r.id} title={r.data.title} extra={<Tag>Редакция {r.version}</Tag>}><FactView data={r.data} /><EvidenceList items={r.evidence} /></Card>) : items.map(p => <Card key={p.id} title={<Space>{status === 'pending' && <Checkbox aria-label={`Выбрать ${p.data.title}`} disabled={p.conflict} checked={selected.includes(p.id)} onChange={e => setSelected(ids => e.target.checked ? [...ids, p.id] : ids.filter(id => id !== p.id))} />}<span>{p.data.title}</span></Space>} extra={<Tag color={p.conflict ? 'orange' : 'blue'}>{p.conflict ? 'Есть противоречие / изменение' : p.origin === 'gigachat' ? 'Предложение AI' : 'Ручное предложение'}</Tag>}>
      <Typography.Text type="secondary">{stamp(p.created_at)}</Typography.Text><FactView data={p.data} />
      {p.target_id && <Card size="small" title="Было" style={{ marginTop: 16 }}>{records.find(r => r.id === p.target_id && r.version === p.base_version)?.data.text || 'Базовая редакция уже изменилась. Сверьте актуальные данные перед исправлением предложения.'}<Tag>Базовая редакция {p.base_version}</Tag></Card>}
      {p.note && <Alert title={p.note} type="info" style={{ marginTop: 16 }} />}<EvidenceList items={p.evidence} />
      {status === 'pending' ? <Space wrap style={{ marginTop: 16 }}><Button type="primary" icon={<CheckOutlined />} onClick={() => { setReason(''); setDecision({ proposal: p, value: 'accepted' }); }}>Подтвердить</Button><Button icon={<EditOutlined />} onClick={() => { setEditing(p); form.setFieldsValue({ ...p.data, target_id: p.target_id, note: p.note }); setCreate(true); }}>Исправить</Button><Button danger icon={<CloseOutlined />} onClick={() => { setReason(''); setDecision({ proposal: p, value: 'rejected' }); }}>Отклонить</Button></Space> : <p>Причина решения: {p.reason || 'Не указана'}</p>}
    </Card>)}
    {!accepted && <Pagination current={page} total={total} pageSize={25} showSizeChanger={false} onChange={v => { setPage(v); setSelected([]); }} />}
    <Modal title={decision?.value === 'accepted' ? 'Подтвердить изменение' : 'Отклонить предложение'} open={!!decision} onCancel={() => setDecision(null)} confirmLoading={saving} onOk={() => void decide()} okText="Сохранить решение"><Typography.Paragraph>{decision?.proposal?.conflict ? 'Это изменение или противоречие. Проверьте обе редакции и основание перед решением.' : 'Изменение будет записано в историю от вашего имени.'}</Typography.Paragraph><Input.TextArea aria-label="Причина решения" value={reason} maxLength={4000} onChange={e => setReason(e.target.value)} rows={3} /></Modal>
    <Modal title="Проект требования" open={create} onCancel={() => setCreate(false)} footer={null} destroyOnHidden><Form form={form} layout="vertical" initialValues={{ status: 'active' }} onFinish={async values => { setSaving(true); try { const target = records.find(r => r.id === values.target_id);
      const body = { kind: 'requirement', target_id: target?.id || null, base_version: target?.version || 0, conflict: !!target, note: values.note || '', data: { title: values.title, text: values.text || '', scope: values.scope || '', owner: values.owner || '', due_date: values.due_date || null, source_date: values.source_date || null, status: values.status || 'active' } };
      if (editing) await api(`/api/proposals/${editing.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...body, expected_version: editing.version }) }); else await post(`/api/projects/${projectId}/proposals`, body);
      setCreate(false); reload(); } catch (e) { message.error((e as Error).message); } finally { setSaving(false); } }}>
      <Form.Item name="target_id" label="Изменяемое требование (без выбора создается отдельное)"><Select allowClear options={records.map(r => ({ value: r.id, label: `${r.data.title} (ред. ${r.version})` }))} /></Form.Item>
      <Form.Item name="title" label="Название" rules={[{ required: true }]}><Input maxLength={500} /></Form.Item><Form.Item name="text" label="Требование"><Input.TextArea rows={4} maxLength={12000} /></Form.Item><Form.Item name="scope" label="Область применения"><Input maxLength={1000} /></Form.Item><Form.Item name="due_date" label="Срок"><Input type="date" /></Form.Item><Form.Item name="source_date" label="Дата источника"><Input type="date" /></Form.Item><Form.Item name="owner" label="Ответственный"><Input maxLength={200} /></Form.Item><Form.Item name="status" label="Состояние"><Select options={[{ value: 'active', label: 'Действует' }, { value: 'cancelled', label: 'Отменено' }]} /></Form.Item><Form.Item name="note" label="Пояснение"><Input.TextArea maxLength={4000} /></Form.Item><Button htmlType="submit" type="primary" loading={saving}>Сохранить предложение</Button></Form></Modal>
  </Space>;
}
