import React, { useState, useCallback } from 'react'
import {
  Table,
  Tag,
  Typography,
  Alert,
  Spin,
  Badge,
  Progress,
  Space,
  Row,
  Col,
} from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppResourceEntry, AppDiskIoEntry } from '../api/types'
import { usePolling } from '../hooks/usePolling'

const { Text, Title } = Typography

interface AppRow {
  key: string
  app_id: string
  app_name: string
  cpu_usage: number
  memory_mb: number
  io_read_rate: number
  io_write_rate: number
  score: number
}

interface DiskIoRow {
  key: string
  pid: number
  name: string
  app_name: string
  cmdline: string
  io_read_rate: number
  io_write_rate: number
  io_read_iops: number
  io_write_iops: number
  score: number
}

interface Props {
  active: boolean
}

function formatBytes(mb: number): string {
  if (mb < 1) return `${(mb * 1024).toFixed(0)} KB/s`
  return `${mb.toFixed(1)} MB/s`
}

export default function AppResources({ active }: Props) {
  const [rows, setRows] = useState<AppRow[]>([])
  const [diskRows, setDiskRows] = useState<DiskIoRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const [resourceData, diskData] = await Promise.all([
        api.getAppResourceStats(3),
        api.getAppDiskIoStats(3),
      ])

      const appRows: AppRow[] = resourceData.apps.map((entry: AppResourceEntry) => ({
        key: entry.app_id,
        app_id: entry.app_id,
        app_name: entry.app_name,
        cpu_usage: entry.cpu_usage,
        memory_mb: entry.memory_mb,
        io_read_rate: entry.io_read_rate,
        io_write_rate: entry.io_write_rate,
        score: entry.score,
      }))
      setRows(appRows)

      const dRows: DiskIoRow[] = diskData.apps.map((entry: AppDiskIoEntry, idx: number) => ({
        key: `${entry.pid ?? idx}`,
        pid: entry.pid ?? 0,
        name: entry.name ?? 'Unknown',
        app_name: entry.app_name ?? '',
        cmdline: entry.cmdline ?? '',
        io_read_rate: entry.io_read_rate ?? 0,
        io_write_rate: entry.io_write_rate ?? 0,
        io_read_iops: entry.io_read_iops ?? 0,
        io_write_iops: entry.io_write_iops ?? 0,
        score: entry.score ?? 0,
      }))
      setDiskRows(dRows)

      setError(null)
      setLastUpdated(new Date())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch data')
    } finally {
      setLoading(false)
    }
  }, [])

  usePolling(fetchData, 5000, active)

  const appColumns: ColumnsType<AppRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (name: string) => (
        <Text style={{ color: COLORS.accent, fontWeight: 500 }}>{name}</Text>
      ),
      sorter: (a, b) => a.app_name.localeCompare(b.app_name),
    },
    {
      title: 'CPU %',
      dataIndex: 'cpu_usage',
      key: 'cpu_usage',
      sorter: (a, b) => a.cpu_usage - b.cpu_usage,
      render: (v: number) => {
        const pct = v * 100
        const color = pct > 80 ? COLORS.red : pct > 50 ? COLORS.orange : COLORS.green
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Text style={{ color, fontSize: 12 }}>{pct.toFixed(1)}%</Text>
            <Progress
              percent={Math.min(pct, 100)}
              showInfo={false}
              strokeColor={color}
              trailColor={COLORS.border}
              size="small"
            />
          </Space>
        )
      },
    },
    {
      title: 'Memory (MB)',
      dataIndex: 'memory_mb',
      key: 'memory_mb',
      sorter: (a, b) => a.memory_mb - b.memory_mb,
      render: (v: number) => (
        <Text style={{ color: COLORS.text }}>{v.toFixed(1)}</Text>
      ),
    },
    {
      title: 'IO Read Rate',
      dataIndex: 'io_read_rate',
      key: 'io_read_rate',
      sorter: (a, b) => a.io_read_rate - b.io_read_rate,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>
      ),
    },
    {
      title: 'IO Write Rate',
      dataIndex: 'io_write_rate',
      key: 'io_write_rate',
      sorter: (a, b) => a.io_write_rate - b.io_write_rate,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>
      ),
    },
    {
      title: 'Score',
      dataIndex: 'score',
      key: 'score',
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.score - b.score,
      render: (v: number) => {
        const color = v > 80 ? COLORS.red : v > 50 ? COLORS.orange : v > 20 ? COLORS.yellow : COLORS.green
        return (
          <Tag style={{ color, borderColor: color, background: `${color}15`, fontSize: 12, fontWeight: 600 }}>
            {v.toFixed(1)}
          </Tag>
        )
      },
    },
  ]

  const diskColumns: ColumnsType<DiskIoRow> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: '28%',
      ellipsis: true,
      render: (v: string, row: DiskIoRow) => (
        <Text style={{ color: COLORS.accent, fontWeight: 500 }}>{v ?? row.name}</Text>
      ),
    },
    {
      title: 'IO Read',
      dataIndex: 'io_read_rate',
      key: 'io_read_rate',
      sorter: (a, b) => a.io_read_rate - b.io_read_rate,
      render: (v: number) => <Text style={{ color: COLORS.text }}>{formatBytes(v)}</Text>,
    },
    {
      title: 'Read IOPS',
      dataIndex: 'io_read_iops',
      key: 'io_read_iops',
      sorter: (a, b) => a.io_read_iops - b.io_read_iops,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{v.toFixed(0)}</Text>
      ),
    },
    {
      title: 'IO Write',
      dataIndex: 'io_write_rate',
      key: 'io_write_rate',
      sorter: (a, b) => a.io_write_rate - b.io_write_rate,
      render: (v: number) => <Text style={{ color: COLORS.textMuted }}>{formatBytes(v)}</Text>,
    },
    {
      title: 'Write IOPS',
      dataIndex: 'io_write_iops',
      key: 'io_write_iops',
      sorter: (a, b) => a.io_write_iops - b.io_write_iops,
      render: (v: number) => (
        <Text style={{ color: COLORS.textMuted }}>{v.toFixed(0)}</Text>
      ),
    },
    {
      title: 'Total I/O',
      key: 'total_io',
      defaultSortOrder: 'descend',
      sorter: (a, b) => (a.io_read_rate + a.io_write_rate) - (b.io_read_rate + b.io_write_rate),
      render: (_: unknown, row: DiskIoRow) => {
        const total = row.io_read_rate + row.io_write_rate
        const color = total > 100 ? COLORS.red : total > 50 ? COLORS.orange : COLORS.green
        return <Text style={{ color, fontWeight: 600 }}>{formatBytes(total)}</Text>
      },
    },
  ]

  const tableStyle = `
    .table-row-alt td { background: ${COLORS.rowAlt} !important; }
    .ant-table { background: transparent !important; }
    .ant-table-thead > tr > th {
      background: ${COLORS.headerBg} !important;
      color: ${COLORS.textMuted} !important;
      font-size: 11px !important;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid ${COLORS.border} !important;
    }
    .ant-table-tbody > tr > td {
      border-bottom: 1px solid ${COLORS.border}55 !important;
    }
    .ant-table-tbody > tr:hover > td {
      background: ${COLORS.rowAlt} !important;
    }
  `

  return (
    <div style={{ padding: '16px 0' }}>
      {error && (
        <Alert
          message="API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', marginBottom: 8, gap: 12 }}>
        {lastUpdated && (
          <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>
            <ReloadOutlined style={{ marginRight: 4 }} />
            Updated: {lastUpdated.toLocaleTimeString()}
          </Text>
        )}
        <Badge
          status="processing"
          color={COLORS.green}
          text={<Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Auto-refresh 5s</Text>}
        />
      </div>

      <Row gutter={[16, 16]}>
        {/* Top Resource Consumer */}
        <Col span={24}>
          <div
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
              padding: 16,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
              <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
                Top Resource Consumers
              </Title>
            </div>
            <Table
              columns={appColumns}
              dataSource={rows}
              loading={loading}
              size="small"
              pagination={false}
              tableLayout="fixed"
              rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
              style={{ color: COLORS.text }}
              locale={{
                emptyText: (
                  <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                    {loading ? <Spin /> : 'No app consumers data available'}
                  </div>
                ),
              }}
            />
            <style>{tableStyle}</style>
          </div>
        </Col>

        {/* Top Disk IO Consumer */}
        <Col span={24}>
          <div
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
              padding: 16,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
              <ThunderboltOutlined style={{ color: COLORS.orange }} />
              <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
                Top Disk I/O Consumer
              </Title>
            </div>
            <Table
              columns={diskColumns}
              dataSource={diskRows}
              loading={loading}
              size="small"
              pagination={false}
              tableLayout="fixed"
              rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
              style={{ color: COLORS.text }}
              locale={{
                emptyText: (
                  <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                    {loading ? <Spin /> : 'No disk I/O consumer data available'}
                  </div>
                ),
              }}
            />
          </div>
        </Col>
      </Row>
    </div>
  )
}
