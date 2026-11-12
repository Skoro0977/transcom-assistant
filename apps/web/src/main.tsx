import React from 'react';
import ReactDOM from 'react-dom/client';
import { ConfigProvider, Layout, Typography } from 'antd';
import ruRU from 'antd/locale/ru_RU';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={ruRU} theme={{token:{colorPrimary:'#176b64',borderRadius:10}}}>
    <Layout style={{minHeight:'100vh',padding:48}}><Typography.Title>Контур проекта</Typography.Title>
      <Typography.Paragraph>Сопровождение проектирования Транскоминжиниринг</Typography.Paragraph>
    </Layout>
  </ConfigProvider></React.StrictMode>,
);
