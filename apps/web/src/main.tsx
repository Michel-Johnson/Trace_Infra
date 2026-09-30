import React from 'react';
import ReactDOM from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { HashRouter } from 'react-router-dom';
import { App } from './App';
import './workspace.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#5e6ad2', colorInfo: '#5e6ad2', colorText: '#25262b', colorTextSecondary: '#747780', colorBorder: '#e3e4ea', borderRadius: 6, fontSize: 14, controlHeight: 36, colorBgLayout: '#ffffff', fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' } }}>
    <AntApp><HashRouter><App /></HashRouter></AntApp>
  </ConfigProvider></React.StrictMode>,
);
