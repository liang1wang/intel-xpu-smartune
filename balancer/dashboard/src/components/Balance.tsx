import React, { useState, useCallback, useEffect } from 'react'
import {
  Row,
  Col,
  Card,
  Table,
  Tag,
  Button,
  Select,
  Input,
  Typography,
  Alert,
  Space,
  Tooltip,
  Modal,
  message,
} from 'antd'
import {
  PlusOutlined,
  DeleteOutlined,
  ThunderboltOutlined,
  StopOutlined,
  HeartOutlined,
  ArrowUpOutlined,
  DatabaseOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { COLORS } from '../styles/theme'
import { api } from '../api/client'
import type { AppInfo } from '../api/types'
import { useAppEvents } from '../hooks/useAppEvents'

const { Text } = Typography
const { Option } = Select

const APP_STATUS = {
  RUNNING: 'running',
  STOPPED: 'stopped',
  LIMITED: 'limited',
  A_LIMITED: 'a_limited',
  PENDING: 'pending',
  NA: 'NA',
} as const

interface Props {
  active: boolean
}

const PRIORITY_OPTIONS = [
  { value: 'low', label: 'Low', color: COLORS.green },
  { value: 'medium', label: 'Medium', color: COLORS.yellow },
  { value: 'high', label: 'High', color: COLORS.orange },
  { value: 'critical', label: 'Critical', color: COLORS.red },
]

function priorityColor(p?: string): string {
  switch (p?.toLowerCase()) {
    case 'low': return COLORS.green
    case 'medium': return COLORS.yellow
    case 'high': return COLORS.orange
    case 'critical': return COLORS.red
    default: return COLORS.textMuted
  }
}

function PriorityTag({ priority }: { priority?: string }) {
  const color = priorityColor(priority)
  return (
    <Tag
      style={{
        color,
        borderColor: color,
        background: `${color}18`,
        fontSize: 11,
        fontWeight: 600,
        textTransform: 'uppercase',
      }}
    >
      {priority ?? 'N/A'}
    </Tag>
  )
}

export default function Balance({ active }: Props) {
  const [allApps, setAllApps] = useState<AppInfo[]>([])
  const [controlledApps, setControlledApps] = useState<AppInfo[]>([])
  const [pendingApps, setPendingApps] = useState<AppInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [messageApi, contextHolder] = message.useMessage()

  // Add app form state
  const [selectedAppId, setSelectedAppId] = useState<string>('')
  const [addPriority, setAddPriority] = useState<string>('medium')
  const [remark, setRemark] = useState('')
  const [adding, setAdding] = useState(false)

  // Per-row priority edit state
  const [rowPriorities, setRowPriorities] = useState<Record<string, string>>({})
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({})

  const fetchData = useCallback(async () => {
    try {
      const [apps, controlled, pending] = await Promise.allSettled([
        api.getApps(),
        api.getControlledApps(),
        api.getPendingApps(),
      ])

      if (apps.status === 'fulfilled') setAllApps(apps.value ?? [])
      if (controlled.status === 'fulfilled') {
        const ctrl = controlled.value ?? []
        setControlledApps(ctrl)
        const priorities: Record<string, string> = {}
        ctrl.forEach((a: AppInfo) => {
          // Normalise to lowercase so comparisons are case-insensitive.
          // The Python Streamlit side stores "Critical"/"High"/… (title-case);
          // the dashboard stores "critical"/"high"/… (lowercase).
          priorities[a.app_id] = (a.priority ?? 'medium').toLowerCase()
        })
        setRowPriorities((prev) => ({ ...prev, ...priorities }))
      }
      if (pending.status === 'fulfilled') setPendingApps(pending.value ?? [])

      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch app data')
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial data fetch on mount / when tab becomes active
  useEffect(() => {
    if (active) fetchData()
  }, [active, fetchData])

  // SSE: push updates from server instead of polling every 5 s
  useAppEvents(
    useCallback((event) => {
      if (event.purpose === 'app' && event.app_id) {
        // Update the app's status in the controlled list
        setControlledApps((prev) =>
          prev.map((app) =>
            app.app_id === event.app_id ? { ...app, status: event.status } : app
          )
        )

        // Show a toast that mirrors the Python register_notification() logic:
        //   - limited/a_limited → resource-limit warning
        //   - any other status change → generic status-updated info
        if (event.status === APP_STATUS.LIMITED || event.status === APP_STATUS.A_LIMITED) {
          messageApi.warning(
            `System busy: ${event.app_name} resource usage has been temporarily limited. It will be restored when resources become available.`
          )
        } else {
          const statusLabel: Record<string, string> = {
            running: 'Running',
            stopped: 'Stopped',
            pending: 'Pending',
          }
          const label = statusLabel[event.status] ?? event.status
          messageApi.info(`App ${event.app_name} status updated: ${label}`)
        }

        // When an app transitions away from pending (e.g. running, stopped, limited),
        // remove it from the pending queue immediately without waiting for fetchData()
        // to complete.  This prevents the card from showing a stale entry during the
        // async round-trip.
        // Note: the reverse (status === PENDING) is intentionally handled only by the
        // fetchData() call below, because adding to pendingApps requires a full AppInfo
        // object that the SSE payload does not carry.
        if (event.status !== APP_STATUS.PENDING) {
          setPendingApps((prev) => prev.filter((app) => app.app_id !== event.app_id))
        }

        // Full server sync – also re-fetches controlled and pending lists so any
        // remaining pending apps (or a newly pending app) are shown correctly.
        fetchData()
      } else if (event.purpose === 'notify') {
        // System-level notifications (no specific app_id)
        if (event.status === 'manual_app_limit_by_user') {
          messageApi.warning(
            'System busy: a critical app is running. Consider manually adjusting resource allocation.'
          )
        } else if (event.status === 'high_usage_by_multiple_instances') {
          messageApi.warning(
            'System busy: multiple apps are consuming high resources. Consider reducing the number of running apps.'
          )
        }
      }
    }, [messageApi, fetchData]),
    active
  )

  function withLoading(key: string, fn: () => Promise<void>) {
    return async () => {
      setActionLoading((prev) => ({ ...prev, [key]: true }))
      try {
        await fn()
        await fetchData()
      } finally {
        setActionLoading((prev) => ({ ...prev, [key]: false }))
      }
    }
  }

  const handleAdd = async () => {
    if (!selectedAppId) {
      messageApi.warning('Please select an application')
      return
    }
    const app = allApps.find((a) => a.app_id === selectedAppId)
    if (!app) return

    setAdding(true)
    try {
      await api.setToControl({
        app_id: app.app_id,
        app_name: app.app_name,
        priority: addPriority,
        controlled: true,
        remark,
        cmdline: app.cmdline ?? '',
        cgroup: 'user',
      })
      messageApi.success(`Added ${app.app_name} to control`)
      setSelectedAppId('')
      setRemark('')
      await fetchData()
    } catch (e: unknown) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to add app')
    } finally {
      setAdding(false)
    }
  }

  const handleRemove = (app: AppInfo) =>
    withLoading(`remove-${app.app_id}`, async () => {
      await api.removeFromControl({ app_id: app.app_id, app_name: app.app_name })
      messageApi.success(`Removed ${app.app_name}`)
    })()

  const handleUpdatePriority = (app: AppInfo) =>
    withLoading(`priority-${app.app_id}`, async () => {
      const p = rowPriorities[app.app_id] ?? app.priority ?? 'medium'
      await api.setPriority({ app_id: app.app_id, priority: p })
      messageApi.success(`Priority updated for ${app.app_name}`)
    })()

  const handleResourceLimit = (app: AppInfo) =>
    withLoading(`limit-${app.app_id}`, async () => {
      try {
        await api.resourceLimit({
          app_id: app.app_id,
          app_name: app.app_name,
          priority: rowPriorities[app.app_id] ?? app.priority ?? 'medium',
        })
        messageApi.success(`Resource limit applied to ${app.app_name}`)
      } catch (e: unknown) {
        messageApi.warning(
          `Cannot limit ${app.app_name}: resource usage is too low or the process is undetectable. Please choose another app.`
        )
      }
    })()

  const handleResourceRestore = (app: AppInfo) =>
    withLoading(`restore-${app.app_id}`, async () => {
      await api.resourceRestore({ app_id: app.app_id })
      messageApi.success(`Resources restored for ${app.app_name}`)
    })()

  const handleKeepAlive = (app: AppInfo) =>
    withLoading(`keepalive-${app.app_id}`, async () => {
      await api.setOomScore({ app_id: app.app_id })
      messageApi.success(`Keep-alive set for ${app.app_name}`)
    })()

  const handleCancelRelaunch = (app: AppInfo) =>
    withLoading(`cancel-${app.app_id}`, async () => {
      await api.cancelRelaunch({ app_id: app.app_id })
      messageApi.success(`Relaunch cancelled for ${app.app_name}`)
    })()

  const controlledColumns: ColumnsType<AppInfo> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      width: 150,
      render: (name: string, record) => {
        const displayName = name || record.app_id
        const tooltipContent = record.remark ? `${displayName} — ${record.remark}` : displayName
        return (
          <Tooltip title={tooltipContent}>
            <span style={{ color: COLORS.accent, fontWeight: 500 }}>
              {displayName}
            </span>
          </Tooltip>
        )
      },
    },
    {
      title: 'Priority',
      key: 'priority',
      width: 160,
      render: (_: unknown, record: AppInfo) => (
        <Select
          value={rowPriorities[record.app_id] ?? record.priority ?? 'medium'}
          onChange={(v) => setRowPriorities((prev) => ({ ...prev, [record.app_id]: v }))}
          size="small"
          style={{ width: 120 }}
          dropdownStyle={{ background: COLORS.panelBg }}
        >
          {PRIORITY_OPTIONS.map((opt) => (
            <Option key={opt.value} value={opt.value}>
              <span style={{ color: opt.color }}>{opt.label}</span>
            </Option>
          ))}
        </Select>
      ),
    },
    {
      title: 'Status',
      key: 'status',
      width: 100,
      align: 'center',
      render: (_: unknown, record: AppInfo) => {
        const s = record.status ?? APP_STATUS.NA
        if (s === APP_STATUS.RUNNING) return <Tag color="success">Running</Tag>
        if (s === APP_STATUS.STOPPED) return <Tag color="default">Stopped</Tag>
        if (s === APP_STATUS.LIMITED || s === APP_STATUS.A_LIMITED) return <Tag color="warning">Limited</Tag>
        if (s === APP_STATUS.PENDING) return <Tag color="processing">Pending</Tag>
        return <Tag color="default">NA</Tag>
      },
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
      width: 140,
      render: (v: string) => (
        <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{v || '—'}</Text>
      ),
    },
    {
      title: 'Actions',
      key: 'actions',
      width: 160,
      align: 'center',
      render: (_: unknown, record: AppInfo) => {
        const isPending = record.status === APP_STATUS.PENDING
        const isRunning = record.status === APP_STATUS.RUNNING
        const isCritical = (rowPriorities[record.app_id] ?? record.priority ?? '').toLowerCase() === 'critical'
        const isLimited = record.status === APP_STATUS.LIMITED || record.status === APP_STATUS.A_LIMITED

        return (
          <Space size={4} wrap={false}>
            <Tooltip title="Cancel Relaunch (only for pending)">
              <Button
                size="small"
                icon={<StopOutlined />}
                disabled={!isPending}
                loading={actionLoading[`cancel-${record.app_id}`]}
                onClick={() => handleCancelRelaunch(record)}
              >
                Cancel
              </Button>
            </Tooltip>

            <Tooltip title="Update Priority">
              <Button
                size="small"
                type="primary"
                icon={<ArrowUpOutlined />}
                loading={actionLoading[`priority-${record.app_id}`]}
                onClick={() => handleUpdatePriority(record)}
              >
                Priority
              </Button>
            </Tooltip>

            {isLimited ? (
              <Tooltip title="Restore Resources">
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={actionLoading[`restore-${record.app_id}`]}
                  onClick={() => handleResourceRestore(record)}
                >
                  Restore
                </Button>
              </Tooltip>
            ) : (
              <Tooltip title="Apply Resource Limit">
                <Button
                  size="small"
                  icon={<DatabaseOutlined />}
                  disabled={!isRunning}
                  loading={actionLoading[`limit-${record.app_id}`]}
                  onClick={() => handleResourceLimit(record)}
                >
                  Limit
                </Button>
              </Tooltip>
            )}

            <Tooltip title={isCritical && isRunning ? 'Keep Alive (OOM protect)' : 'Only available for Critical apps that are Running'}>
              <Button
                size="small"
                icon={<HeartOutlined />}
                disabled={!isCritical || !isRunning}
                loading={actionLoading[`keepalive-${record.app_id}`]}
                onClick={() => handleKeepAlive(record)}
                style={isCritical && isRunning ? { borderColor: COLORS.red, color: COLORS.red } : {}}
              >
                Keep Alive
              </Button>
            </Tooltip>

            <Tooltip title="Remove from Control">
              <Button
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={actionLoading[`remove-${record.app_id}`]}
                onClick={() => {
                  Modal.confirm({
                    title: `Remove ${record.app_name}?`,
                    content: 'This will remove the app from resource control.',
                    okText: 'Remove',
                    okType: 'danger',
                    onOk: () => handleRemove(record),
                  })
                }}
              >
                Remove
              </Button>
            </Tooltip>
          </Space>
        )
      },
    },
  ]

  const pendingColumns: ColumnsType<AppInfo> = [
    {
      title: 'App Name',
      dataIndex: 'app_name',
      key: 'app_name',
      render: (name: string) => <Text style={{ color: COLORS.text }}>{name}</Text>,
    },
    {
      title: 'Priority',
      dataIndex: 'priority',
      key: 'priority',
      render: (p: string) => <PriorityTag priority={p} />,
    },
    {
      title: 'Status',
      key: 'status',
      render: () => <Tag color="processing">Pending</Tag>,
    },
    {
      title: 'Remark',
      dataIndex: 'remark',
      key: 'remark',
      render: (v: string) => <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>{v ?? '—'}</Text>,
    },
  ]

  const uncontrolledApps = allApps.filter(
    (a) => !controlledApps.some((c) => c.app_id === a.app_id)
  )

  return (
    <div style={{ padding: '16px 0' }}>
      {contextHolder}

      {error && (
        <Alert
          message="API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      {/* Add App Section */}
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <PlusOutlined style={{ marginRight: 8, color: COLORS.accent }} />
            Add Application to Control
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 6,
          marginBottom: 12,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '16px' }}
      >
        <Row gutter={[12, 12]} align="middle">
          <Col xs={24} sm={10} md={8}>
            <div style={{ marginBottom: 4 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Application</Text>
            </div>
            <Select
              value={selectedAppId || undefined}
              onChange={setSelectedAppId}
              placeholder="Select application..."
              style={{ width: '100%' }}
              showSearch
              filterOption={(input, option) =>
                String(option?.children ?? '').toLowerCase().includes(input.toLowerCase())
              }
              dropdownStyle={{ background: COLORS.panelBg }}
              notFoundContent={
                <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>No apps found</Text>
              }
            >
              {uncontrolledApps.map((app) => (
                <Option key={app.app_id} value={app.app_id}>
                  {app.app_name}
                </Option>
              ))}
            </Select>
          </Col>

          <Col xs={24} sm={6} md={4}>
            <div style={{ marginBottom: 4 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Priority</Text>
            </div>
            <Select
              value={addPriority}
              onChange={setAddPriority}
              style={{ width: '100%' }}
              dropdownStyle={{ background: COLORS.panelBg }}
            >
              {PRIORITY_OPTIONS.map((opt) => (
                <Option key={opt.value} value={opt.value}>
                  <span style={{ color: opt.color }}>{opt.label}</span>
                </Option>
              ))}
            </Select>
          </Col>

          <Col xs={24} sm={10} md={8}>
            <div style={{ marginBottom: 4 }}>
              <Text style={{ color: COLORS.textMuted, fontSize: 11 }}>Remark</Text>
            </div>
            <Input
              value={remark}
              onChange={(e) => setRemark(e.target.value)}
              placeholder="Optional note..."
              style={{ background: COLORS.panelBg, borderColor: COLORS.border }}
            />
          </Col>

          <Col xs={24} sm={24} md={4}>
            <div style={{ marginBottom: 4, visibility: 'hidden' }}>
              <Text style={{ fontSize: 11 }}>action</Text>
            </div>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              loading={adding}
              onClick={handleAdd}
              block
            >
              Add to Control
            </Button>
          </Col>
        </Row>
      </Card>

      {/* Controlled Apps */}
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            Controlled Applications
            <Tag style={{ marginLeft: 8, fontSize: 11 }}>{controlledApps.length}</Tag>
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 6,
          marginBottom: 12,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '0' }}
      >
        <Table
          columns={controlledColumns}
          dataSource={controlledApps.map((a) => ({ ...a, key: a.app_id }))}
          loading={loading}
          size="small"
          pagination={false}
          scroll={{ x: 'max-content' }}
          rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
          locale={{
            emptyText: (
              <div style={{ padding: 30, color: COLORS.textMuted, textAlign: 'center' }}>
                No apps under control. Add one above.
              </div>
            ),
          }}
        />
      </Card>

      {/* Pending Queue – always shown so users can see the empty state (mirrors Python's pending_queue_holder) */}
      <Card
        title={
          <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
            <ThunderboltOutlined style={{ marginRight: 8, color: COLORS.yellow }} />
            Pending Queue
            <Tag color="processing" style={{ marginLeft: 8 }}>
              {pendingApps.length}
            </Tag>
          </Text>
        }
        style={{
          background: COLORS.panelBg,
          border: `1px solid ${COLORS.yellow}44`,
          borderRadius: 6,
        }}
        headStyle={{ borderBottom: `1px solid ${COLORS.border}`, padding: '8px 16px', minHeight: 40 }}
        bodyStyle={{ padding: '0' }}
      >
        {pendingApps.length === 0 ? (
          <div style={{ padding: 24, textAlign: 'center', color: COLORS.textMuted, fontStyle: 'italic' }}>
            🕊️ Pending queue is empty
          </div>
        ) : (
          <Table
            columns={pendingColumns}
            dataSource={pendingApps.map((a) => ({ ...a, key: a.app_id }))}
            size="small"
            pagination={false}
            rowClassName={(_, idx) => (idx % 2 === 1 ? 'table-row-alt' : '')}
          />
        )}
      </Card>

      <style>{`
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
      `}</style>
    </div>
  )
}
