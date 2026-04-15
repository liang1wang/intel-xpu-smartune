import React, { useState, useCallback } from 'react'
import { Tabs, Layout, Typography, Space, Badge } from 'antd'
import {
  DashboardOutlined,
  AppstoreOutlined,
  NodeIndexOutlined,
  ControlOutlined,
  LineChartOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons'
import SystemOverview from './components/SystemOverview'
import AppResources from './components/AppResources'
import Processes from './components/Processes'
import Balance from './components/Balance'
import HistoryDashboard from './components/HistoryDashboard'
import About from './components/About'
import { COLORS } from './styles/theme'

const { Header, Content } = Layout

export default function App() {
  const [activeTab, setActiveTab] = useState('1')

  const tabs = [
    {
      key: '1',
      label: (
        <Space>
          <DashboardOutlined />
          System Overview
        </Space>
      ),
      children: <SystemOverview active={activeTab === '1'} />,
    },
    {
      key: '2',
      label: (
        <Space>
          <AppstoreOutlined />
          App Resources
        </Space>
      ),
      children: <AppResources active={activeTab === '2'} />,
    },
    {
      key: '3',
      label: (
        <Space>
          <NodeIndexOutlined />
          Processes
        </Space>
      ),
      children: <Processes active={activeTab === '3'} />,
    },
    {
      key: '4',
      label: (
        <Space>
          <LineChartOutlined />
          History
        </Space>
      ),
      children: <HistoryDashboard active={activeTab === '4'} />,
    },
    {
      key: '5',
      label: (
        <Space>
          <ControlOutlined />
          Balancer
        </Space>
      ),
      children: <Balance active={activeTab === '5'} />,
    },
    {
      key: '6',
      label: (
        <Space>
          <InfoCircleOutlined />
          About
        </Space>
      ),
      children: <About active={activeTab === '6'} />,
    },
  ]

  return (
    <Layout style={{ minHeight: '100vh', background: COLORS.bg }}>
      <Header
        style={{
          background: COLORS.headerBg,
          borderBottom: `1px solid ${COLORS.border}`,
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          gap: 16,
          position: 'sticky',
          top: 0,
          zIndex: 100,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <div
            style={{
              width: 32,
              height: 32,
              background: `linear-gradient(135deg, ${COLORS.accent} 0%, #3a6fd8 100%)`,
              borderRadius: 6,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <DashboardOutlined style={{ color: '#fff', fontSize: 16 }} />
          </div>
          <Typography.Title
            level={4}
            style={{ color: COLORS.text, margin: 0, fontWeight: 600 }}
          >
            Intel XPU SmarTune
          </Typography.Title>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <Badge status="processing" color={COLORS.green} />
          <Typography.Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
            Dynamic
          </Typography.Text>
        </div>
      </Header>

      <Content style={{ padding: '0 16px 16px', background: COLORS.bg }}>
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={tabs}
          size="large"
          style={{ color: COLORS.text }}
          tabBarStyle={{
            marginBottom: 0,
            paddingTop: 8,
            background: COLORS.bg,
            borderBottom: `1px solid ${COLORS.border}`,
            position: 'sticky',
            top: 64,
            zIndex: 99,
          }}
        />
      </Content>
    </Layout>
  )
}
