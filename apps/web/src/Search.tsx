import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Checkbox, Empty, Form, Input, Select, Space, Tag, Typography } from 'antd';
import { SearchOutlined, ReloadOutlined } from '@ant-design/icons';
import { api, post, stamp } from './api';
import { EvidenceList, type Evidence } from './Review';

type Hit = { id: string; text: string; title: string; locator: string; kind: string; number: number;
  version_id?: string; source_date: string | null; recorded_at: string; quoted: boolean; evidence?: Evidence[] };
type Results = { items: Hit[]; warning: string; mode: string; date_basis: string };
type Index = { states: Record<string, number>; cloud_indexing_enabled: boolean };
export function Search({ projectId }: { projectId: string }) {
  const { message } = App.useApp(); const [form] = Form.useForm();
  const [index, setIndex] = useState<Index | null>(null), [result, setResult] = useState<Results | null>(null);
  const [loading, setLoading] = useState(false), [error, setError] = useState('');
  function status() { void api<Index>(`/api/projects/${projectId}/index-status`).then(setIndex).catch(e => setError(e.message)); }
  useEffect(status, [projectId]);
  return <Space orientation="vertical" size={20} style={{ width: '100%' }}>
    {error && <Alert type="error" title={error} showIcon />}
    <Card title="Поиск в документах и подтвержденных решениях" extra={<Button icon={<ReloadOutlined />} onClick={status}>Обновить состояние</Button>}>
      {!index?.cloud_indexing_enabled && <Alert type="info" showIcon title="Облачная индексация отключена в настройках сервера. Локальный поиск по словам доступен." style={{ marginBottom: 20 }} />}
      <Form form={form} disabled={loading} layout="vertical" initialValues={{ mode: 'current', basis: 'recorded', semantic: false }} onValuesChange={() => setResult(null)} onFinish={async values => {
        setLoading(true); setError('');
        const params = new URLSearchParams({ q: values.q.trim(), mode: values.mode, basis: values.basis, semantic: String(!!values.semantic) });
        if (values.start) params.set('start', values.start); if (values.end) params.set('end', values.end);
        try { setResult(await api<Results>(`/api/projects/${projectId}/search?${params}`)); } catch (e) { setError((e as Error).message); } finally { setLoading(false); }
      }}>
        <Form.Item name="q" label="Что найти" rules={[{ required: true, whitespace: true, min: 2 }]}><Input maxLength={2000} size="large" /></Form.Item>
        <Space wrap align="start"><Form.Item name="mode" label="Редакции"><Select style={{ width: 240 }} options={[{ value: 'current', label: 'Текущие редакции' }, { value: 'history', label: 'Вся история активных источников' }]} /></Form.Item>
          <Form.Item name="basis" label="Период по дате"><Select style={{ width: 240 }} options={[{ value: 'recorded', label: 'Поступления / подтверждения' }, { value: 'source', label: 'Документа / решения' }]} /></Form.Item>
          <Form.Item name="start" label="С"><Input type="date" /></Form.Item><Form.Item name="end" label="По"><Input type="date" /></Form.Item></Space>
        <Form.Item name="semantic" valuePropName="checked"><Checkbox disabled={!index?.states.ready}>Добавить смысловой поиск через GigaChat</Checkbox></Form.Item>
        <Space wrap><Button type="primary" htmlType="submit" icon={<SearchOutlined />} loading={loading}>Найти</Button><Button onClick={() => void post(`/api/projects/${projectId}/reindex`).then(() => { message.success('Локальные фрагменты обновлены. Облачная обработка зависит от настройки сервера.'); status(); }).catch(e => message.error(e.message))}>Обновить индекс</Button></Space>
      </Form>
    </Card>
    {result?.warning && <Alert title={result.warning} type="warning" showIcon />}
    {result && !result.items.length && <Empty description="По этим условиям оснований не найдено" />}
    {result?.items.map(hit => <Card key={hit.id} title={hit.title} extra={<Tag color={hit.kind === 'accepted' ? 'green' : 'blue'}>{hit.kind === 'accepted' ? 'Подтвержденное решение' : 'Источник'} · ред. {hit.number}</Tag>}>
      <Typography.Paragraph type="secondary">{hit.locator}</Typography.Paragraph>{hit.quoted && <Tag color="orange">Цитата старой переписки</Tag>}<pre className="source-text">{hit.text}</pre>
      <Space wrap style={{ marginTop: 16 }}><Typography.Text type="secondary">Дата источника: {hit.source_date || 'не установлена'}. Зарегистрировано: {stamp(hit.recorded_at)}</Typography.Text>{hit.version_id && <Button href={`/api/document-versions/${hit.version_id}/content`}>Оригинал этой редакции</Button>}</Space>
      {hit.evidence && <EvidenceList items={hit.evidence} />}
    </Card>)}
  </Space>;
}
