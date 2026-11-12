import React, { useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { Alert, App, Avatar, Button, Card, ConfigProvider, Empty, Form, Input, Layout, Menu, Modal, Select, Space, Spin, Statistic, Tag, Typography } from 'antd';
import { ApartmentOutlined, FileTextOutlined, LogoutOutlined, PlusOutlined, ProjectOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import ruRU from 'antd/locale/ru_RU';
import { api, post, setSession, type Session, type Project } from './api';
import { Documents } from './Documents';
import { Review } from './Review';
import './styles.css';

type Overview = Project & { members: { id: string; username: string }[]; document_counts: Record<string, number>; gigachat_configured: boolean };
function Workspace() {
  const { message } = App.useApp();
  const [session, sessionState] = useState<Session | null>(null), [boot, setBoot] = useState(true);
  const [projects, setProjects] = useState<Project[]>([]), [projectId, setProjectId] = useState('');
  const [view, setView] = useState('overview'), [overview, setOverview] = useState<Overview | null>(null);
  const [refresh, setRefresh] = useState(0), [creating, setCreating] = useState(false), [error, setError] = useState('');
  const [logging, setLogging] = useState(false);
  function authenticated(value: Session | null) { setSession(value); sessionState(value); if (!value) { setProjects([]); setOverview(null); setProjectId(''); } }
  useEffect(() => {
    api<Session>('/api/auth/me').then(authenticated).catch(() => {}).finally(() => setBoot(false));
    const expired = () => authenticated(null); window.addEventListener('session-expired', expired);
    return () => window.removeEventListener('session-expired', expired);
  }, []);
  useEffect(() => {
    if (!session) return; let active = true;
    api<Project[]>('/api/projects').then(data => { if (active) { setProjects(data); setProjectId(id => data.some(p => p.id === id) ? id : data[0]?.id || ''); } }).catch(e => message.error(e.message));
    return () => { active = false; };
  }, [session, refresh]);
  useEffect(() => {
    if (!projectId) return; let active = true; setOverview(null);
    api<Overview>(`/api/projects/${projectId}/overview`).then(data => { if (active) setOverview(data); }).catch(e => message.error(e.message));
    return () => { active = false; };
  }, [projectId, refresh]);
  if (boot) return <div className="loading"><Spin size="large" /></div>;
  if (!session) return <div className="login-page"><div className="login-intro"><div className="brand-mark"><ApartmentOutlined /></div><Tag color="cyan">ТРАНСКОМИНЖИНИРИНГ</Tag><h1>Весь контекст<br />вашего проекта.</h1><p>Документы, переписка и история решений в одном рабочем пространстве.</p><div className="login-note"><SafetyCertificateOutlined /> Выводы AI проходят проверку руководителем проекта.</div></div>
    <Card className="login-card"><Typography.Title level={2}>Вход в систему</Typography.Title><Typography.Paragraph type="secondary">Используйте выданную учетную запись</Typography.Paragraph>
      {error && <Alert title={error} type="error" showIcon style={{ marginBottom: 20 }} />}
      <Form layout="vertical" onFinish={async values => { setLogging(true); try { authenticated(await post<Session>('/api/auth/login', values)); setError(''); } catch (e) { setError((e as Error).message); } finally { setLogging(false); } }}>
        <Form.Item name="username" label="Логин" rules={[{ required: true, message: 'Введите логин' }]}><Input autoComplete="username" size="large" maxLength={64} /></Form.Item>
        <Form.Item name="password" label="Пароль" rules={[{ required: true, message: 'Введите пароль' }]}><Input.Password autoComplete="current-password" size="large" maxLength={256} /></Form.Item>
        <Button block size="large" type="primary" htmlType="submit" loading={logging}>Войти</Button>
      </Form><Typography.Paragraph className="login-help" type="secondary">Самостоятельная регистрация отключена. Доступ выдает администратор.</Typography.Paragraph>
    </Card></div>;
  const counts = overview?.document_counts || {};
  return <><Layout className="workspace"><Layout.Sider width={248} breakpoint="lg" collapsedWidth={72} theme="dark">
    <div className="brand"><ApartmentOutlined /><span>Контур проекта<small>ТРАНСКОМИНЖИНИРИНГ</small></span></div>
    <div className="nav-caption">РАБОЧЕЕ ПРОСТРАНСТВО</div><Menu theme="dark" selectedKeys={[view]} onClick={({ key }) => setView(key)} items={[
      { key: 'overview', icon: <ProjectOutlined />, label: 'Обзор проекта' }, { key: 'documents', icon: <FileTextOutlined />, label: 'Документы и письма' },
      { key: 'review', icon: <SafetyCertificateOutlined />, label: 'Требует проверки' }, { key: 'requirements', icon: <ProjectOutlined />, label: 'Требования' },
    ]} /><div className="sider-footer"><Tag color="cyan">Локальный пилот</Tag><p>Источники и решения<br />сохраняются в истории</p></div>
  </Layout.Sider><Layout><Layout.Header className="header"><Space wrap><Typography.Text type="secondary">Объект</Typography.Text><Select aria-label="Выбор проекта" className="project-select" value={projectId || undefined} onChange={setProjectId} options={projects.map(p => ({ value: p.id, label: p.title }))} />
      <Button icon={<PlusOutlined />} onClick={() => setCreating(true)} aria-label="Создать проект" /></Space><Space><Avatar size="small">{session.user.username[0].toUpperCase()}</Avatar><span>{session.user.username}</span><Button type="text" icon={<LogoutOutlined />} onClick={() => void post('/api/auth/logout').then(() => authenticated(null)).catch(e => message.error(e.message))}>Выйти</Button></Space></Layout.Header>
    <Layout.Content className="content"><div className="page-title"><div><Typography.Text className="eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО ГИП</Typography.Text><Typography.Title level={2}>{({ overview: 'Обзор проекта', documents: 'Документы и письма', review: 'Требует проверки', requirements: 'Требования' } as Record<string,string>)[view]}</Typography.Title><Typography.Text type="secondary">{projects.find(p => p.id === projectId)?.title || 'Создайте первый проект'}</Typography.Text></div><Tag className="review-label" icon={<SafetyCertificateOutlined />} color="cyan">Решения подтверждает человек</Tag></div>
      {!projectId ? <Empty description="Доступных проектов пока нет"><Button type="primary" onClick={() => setCreating(true)}>Создать проект</Button></Empty> : view === 'documents' ? <Documents key={projectId} projectId={projectId} changed={() => setRefresh(n => n + 1)} /> : view === 'review' || view === 'requirements' ? <Review key={projectId + view} projectId={projectId} accepted={view === 'requirements'} /> : <>
        <div className="stats-grid"><Card><Statistic title="Источники в работе" value={Object.values(counts).reduce((a, b) => a + b, 0)} /></Card><Card><Statistic title="Текст извлечен" value={counts.ready || 0} /></Card><Card><Statistic title="Нужна подготовка" value={counts.needs_preparation || 0} /></Card><Card><Statistic title="Участники проекта" value={overview?.members.length || 0} /></Card></div>
        <div className="overview-grid"><Card title="Контекст проекта"><Typography.Title level={4}>{overview?.title}</Typography.Title><Typography.Paragraph>{overview?.description}</Typography.Paragraph><Alert type="info" showIcon title="Проверка оснований" description="Извлеченный текст сохраняет связь с оригиналом. Обработка файла сама по себе не подтверждает его требования." /><Button style={{ marginTop: 20 }} type="primary" onClick={() => setView('documents')}>Открыть библиотеку</Button></Card>
          <Card title="Рабочая среда"><p><Tag color={overview?.gigachat_configured ? 'green' : 'gold'}>{overview?.gigachat_configured ? 'GigaChat настроен' : 'GigaChat не настроен'}</Tag></p><p>Все времена отображаются по Москве.</p><p>Оригиналы и редакции сохраняются раздельно.</p></Card></div>
      </>}
    </Layout.Content></Layout></Layout>
    <Modal title="Новый объект" open={creating} footer={null} onCancel={() => setCreating(false)} destroyOnHidden><Form layout="vertical" onFinish={async values => { try { const p = await post<Project>('/api/projects', values); setProjectId(p.id); setRefresh(n => n + 1); setCreating(false); } catch (e) { message.error((e as Error).message); } }}>
      <Form.Item name="title" label="Название" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item><Form.Item name="description" label="Описание"><Input.TextArea maxLength={8000} rows={3} /></Form.Item><Button type="primary" htmlType="submit">Создать</Button></Form></Modal>
  </>;
}

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ConfigProvider locale={ruRU} theme={{ token: { colorPrimary: '#12746d', borderRadius: 10, fontFamily: 'Segoe UI, Arial, sans-serif', colorBgLayout: '#f3f6f8' }, components: { Menu: { darkItemBg: '#112d32', darkItemSelectedBg: '#245057' }, Layout: { siderBg: '#112d32' } } }}><App><Workspace /></App></ConfigProvider></React.StrictMode>);
