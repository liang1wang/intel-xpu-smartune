import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, DatePicker, Divider, Empty, Popover, Segmented, Select, Space, Typography, message } from 'antd'
import { ReloadOutlined, SettingOutlined } from '@ant-design/icons'
import {
  Brush,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import dayjs, { type Dayjs } from 'dayjs'
import { api } from '../api/client'
import type { DynamicInfoData, HistoryData, HistoryRetentionData, HistorySnapshotItem, QmassaDevice } from '../api/types'
import { COLORS } from '../styles/theme'

const { Text, Title } = Typography

interface Props {
  active: boolean
}

type EngineKey = 'vcs' | 'vecs' | 'ccs' | 'rcs' | 'bcs'

interface PressureTrendPoint {
  timestamp: string
  ts: number
  systemPressure: number | null
  diskPressure: number | null
  networkPressure: number | null
}

interface CpuMemTrendPoint {
  timestamp: string
  ts: number
  cpuUtilization: number | null
  pCoreUtilization: number | null
  eCoreUtilization: number | null
  memoryUtilization: number | null
}

interface DiskTrendPoint {
  timestamp: string
  ts: number
  [diskName: string]: string | number | null
}

interface NetworkTrendPoint {
  timestamp: string
  ts: number
  [nicName: string]: string | number | null
}

interface NpuTrendPoint {
  timestamp: string
  ts: number
  npuUtilization: number | null
  npuFreqMhz: number | null
}

interface GpuTrendPoint {
  timestamp: string
  ts: number
  vcs: number | null
  vecs: number | null
  ccs: number | null
  rcs: number | null
  bcs: number | null
  gt0Freq: number | null
  gt1Freq: number | null
  gpuPower: number | null
  pkgPower: number | null
  memUsage: number | null
}

interface GpuTrendSeries {
  id: string
  label: string
  isIntegrated: boolean
  points: GpuTrendPoint[]
}

type RangePreset = '5m' | '15m' | '1h' | '6h' | '24h' | 'custom'

interface HistoryNetworkExtra {
  utilization_percent?: number | null
}

interface HistoryNpuSmiExtra {
  utilization_percent?: number | null
  parsed?: {
    utilization?: number | null
    utilization_percent?: number | null
  } | null
}

const LIMIT_OPTIONS = [50, 100, 200, 500]
const RANGE_PRESET_OPTIONS: Array<{ label: string; value: RangePreset }> = [
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
  { label: '6h', value: '6h' },
  { label: '24h', value: '24h' },
  { label: 'Custom', value: 'custom' },
]

const RANGE_SECONDS: Record<Exclude<RangePreset, 'custom'>, number> = {
  '5m': 5 * 60,
  '15m': 15 * 60,
  '1h': 60 * 60,
  '6h': 6 * 60 * 60,
  '24h': 24 * 60 * 60,
}

const SNAPSHOT_INTERVAL_SECONDS = 5
const HISTORY_LIMIT_CAP = 20000

function estimateRequiredLimit(
  rangePreset: RangePreset,
  customRange: [Dayjs | null, Dayjs | null] | null,
): number {
  let seconds = 0
  if (rangePreset === 'custom') {
    const start = customRange?.[0]
    const end = customRange?.[1]
    if (start && end && end.unix() >= start.unix()) {
      seconds = end.unix() - start.unix()
    }
  } else {
    seconds = RANGE_SECONDS[rangePreset]
  }

  if (seconds <= 0) return 100
  const estimated = Math.ceil(seconds / SNAPSHOT_INTERVAL_SECONDS) + 20
  return Math.max(100, Math.min(estimated, HISTORY_LIMIT_CAP))
}

function formatHistoryAxisTick(val: string | number): string {
  const text = String(val)
  if (text.length >= 11) return text.slice(0, 11) // MM-DD HH:mm
  return text
}

const DISK_COLORS = ['#e07b54', '#73bf69', COLORS.accent, COLORS.yellow, COLORS.red, '#b877db', '#56c8d8']
const NETWORK_COLORS = ['#73bf69', COLORS.accent, '#e07b54', COLORS.yellow, '#b877db', '#56c8d8', COLORS.red]

// Fixed per-metric colors for disk and network charts
const METRIC_COLORS = { util: '#56c8d8', read: '#73bf69', write: '#e07b54', rx: '#73bf69', tx: '#e07b54' }

const ENGINE_CURVE_META: Array<{ key: EngineKey; name: string; color: string }> = [
  { key: 'vcs', name: 'VCS %', color: COLORS.accent },
  { key: 'vecs', name: 'VECS %', color: COLORS.green },
  { key: 'ccs', name: 'CCS %', color: COLORS.yellow },
  { key: 'rcs', name: 'RCS %', color: COLORS.orange },
  { key: 'bcs', name: 'BCS %', color: COLORS.red },
]

function normalizePercent(value: unknown): number | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  if (value <= 1) return Math.max(0, Math.min(value * 100, 100))
  return Math.max(0, Math.min(value, 100))
}

function toNumber(value: unknown): number | null {
  if (typeof value !== 'number' || Number.isNaN(value)) return null
  return value
}

/** Shared tooltip formatter — limits displayed precision to `digits` decimal places */
function tooltipFmt(digits = 2) {
  return (val: number, name: string) => {
    if (typeof val !== 'number' || Number.isNaN(val)) return [val, name]
    return [Number(val.toFixed(digits)), name]
  }
}
const tooltipFmt2 = tooltipFmt(2)

function isNumber(value: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function toDynamicData(item: HistorySnapshotItem): DynamicInfoData | null {
  if (item.snapshot_type !== 'dynamic') return null
  if (!item.data || typeof item.data !== 'object' || Array.isArray(item.data)) return null
  return item.data as DynamicInfoData
}

function buildTimestamp(item: HistorySnapshotItem): { ts: number; label: string } {
  const tsSec = Number.isFinite(item.create_time) ? item.create_time : 0
  const label = tsSec > 0
    ? dayjs.unix(tsSec).format('MM-DD HH:mm:ss')
    : (item.collected_at || item.create_date || `#${item.id}`)
  return { ts: tsSec, label }
}

function getPressurePeak(dynamic: DynamicInfoData | null): number | null {
  const pressureCpu = normalizePercent(dynamic?.pressure?.cpu)
  const pressureMemory = normalizePercent(dynamic?.pressure?.memory)
  const pressureIo = normalizePercent(dynamic?.pressure?.io)
  const values = [pressureCpu, pressureMemory, pressureIo].filter(isNumber)
  if (!values.length) return null
  return Math.max(...values)
}

function getNetworkUsage(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic) return null
  const network = dynamic.network as DynamicInfoData['network'] & HistoryNetworkExtra
  // utilization_percent is already a percentage (0-100) from backend; use toNumber to avoid normalizePercent ×100
  const utilization = toNumber(network?.utilization_percent)
  if (utilization != null) return utilization
  // Fallback: NetworkMonitor rx/tx fractions (0-1) stored in the pressure section.
  const p = dynamic.pressure as { network_rx?: unknown; network_tx?: unknown } | undefined
  const rx = normalizePercent(p?.network_rx)
  const tx = normalizePercent(p?.network_tx)
  const vals = [rx, tx].filter((v): v is number => v != null)
  return vals.length > 0 ? Math.max(...vals) : null
}

function getNpuUsage(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & HistoryNpuSmiExtra
  const direct = normalizePercent(npuSmi.utilization_percent)
  if (direct != null) return direct
  return normalizePercent(npuSmi.parsed?.utilization_percent ?? npuSmi.parsed?.utilization)
}

function normalizeEngineUtil(device: QmassaDevice, key: EngineKey): number | null {
  return normalizePercent(device.engine_util?.[key])
}

function getFreq(device: QmassaDevice, targetName: 'gt0' | 'gt1'): number | null {
  const freq = (device.freqs || []).find((item) => item.name === targetName)
  if (!freq) return null
  return toNumber(freq.cur_mhz ?? freq.act_mhz)
}

function getGpuLabel(device: QmassaDevice, index: number): string {
  const type = `${device.dev_type || ''}`.toLowerCase()
  const role = type.includes('integrated') || type.includes('igpu')
    ? 'iGPU'
    : (type.includes('discrete') || type.includes('dgpu') ? 'dGPU' : `GPU${index}`)
  const pci = device.pci_dev ? ` (${device.pci_dev})` : ''
  return `${role}${pci}`
}

function buildPressureTrendPoints(items: HistorySnapshotItem[]): PressureTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    return {
      timestamp: label,
      ts,
      systemPressure: getPressurePeak(dynamic),
      diskPressure: normalizePercent(dynamic?.pressure?.io),
      networkPressure: getNetworkUsage(dynamic),
    }
  })
}

function buildCpuMemTrendPoints(items: HistorySnapshotItem[]): CpuMemTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    return {
      timestamp: label,
      ts,
      cpuUtilization: normalizePercent(dynamic?.cpu?.usage_total),
      pCoreUtilization: normalizePercent(dynamic?.cpu?.p_core_usage),
      eCoreUtilization: normalizePercent(dynamic?.cpu?.e_core_usage),
      memoryUtilization: normalizePercent(dynamic?.memory?.usage_percent),
    }
  })
}

function buildDiskTrendPoints(items: HistorySnapshotItem[]): { points: DiskTrendPoint[]; diskNames: string[] } {
  const diskNameSet = new Set<string>()
  const points = [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    const point: DiskTrendPoint = { timestamp: label, ts }

    const disk = dynamic?.disk as (DynamicInfoData['disk'] & {
      per_disk?: Record<string, { util?: number | null; read_mb?: number | null; write_mb?: number | null } | number | null>
    }) | undefined
    const perDisk = disk?.per_disk
    if (perDisk && typeof perDisk === 'object') {
      for (const [name, val] of Object.entries(perDisk)) {
        diskNameSet.add(name)
        if (val && typeof val === 'object' && ('util' in val || 'read_mb' in val)) {
          // New format
          point[`${name}:util`] = normalizePercent(val.util)
          point[`${name}:read`] = toNumber(val.read_mb)
          point[`${name}:write`] = toNumber(val.write_mb)
        } else {
          // Old format: single utilization number
          point[`${name}:util`] = typeof val === 'number' ? normalizePercent(val) : null
          point[`${name}:read`] = null
          point[`${name}:write`] = null
        }
      }
    } else if (disk?.disk_io) {
      for (const [name, diskData] of Object.entries(disk.disk_io)) {
        diskNameSet.add(name)
        point[`${name}:util`] = normalizePercent(diskData?.utilization)
        point[`${name}:read`] = toNumber(diskData?.read_kb_per_sec != null ? diskData.read_kb_per_sec / 1024 : null)
        point[`${name}:write`] = toNumber(diskData?.write_kb_per_sec != null ? diskData.write_kb_per_sec / 1024 : null)
      }
    }
    return point
  })
  return { points, diskNames: Array.from(diskNameSet).sort() }
}

function buildNetworkTrendPoints(items: HistorySnapshotItem[]): { points: NetworkTrendPoint[]; nicNames: string[] } {
  const nicNameSet = new Set<string>()
  const points = [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    const point: NetworkTrendPoint = { timestamp: label, ts }

    const network = dynamic?.network as (DynamicInfoData['network'] & {
      per_nic?: Record<string, { util?: number | null; rx_mbps?: number | null; tx_mbps?: number | null; rx?: number | null; tx?: number | null } | number | null>
    }) | undefined
    const perNic = network?.per_nic
    if (perNic && typeof perNic === 'object') {
      for (const [name, val] of Object.entries(perNic)) {
        nicNameSet.add(name)
        if (val && typeof val === 'object') {
          // per_nic.util is already a percentage (0-100) from backend; skip normalizePercent to avoid ×100 for values ≤1
          point[`${name}:util`] = toNumber(val.util) ?? normalizePercent(val.rx != null || val.tx != null ? Math.max(val.rx ?? 0, val.tx ?? 0) : null)
          point[`${name}:rx`] = toNumber(val.rx_mbps) ?? null
          point[`${name}:tx`] = toNumber(val.tx_mbps) ?? null
        } else {
          // Old format: single utilization number
          point[`${name}:util`] = typeof val === 'number' ? normalizePercent(val) : null
          point[`${name}:rx`] = null
          point[`${name}:tx`] = null
        }
      }
    } else {
      // Fallback: use aggregated utilization_percent as single line (already 0-100 from backend)
      const aggUtil = toNumber((network as DynamicInfoData['network'] & HistoryNetworkExtra)?.utilization_percent)
      if (aggUtil != null) {
        nicNameSet.add('total')
        point['total:util'] = aggUtil
        point['total:rx'] = null
        point['total:tx'] = null
      }
    }
    return point
  })
  return { points, nicNames: Array.from(nicNameSet).sort() }
}

function getNpuFreqMhz(dynamic: DynamicInfoData | null): number | null {
  if (!dynamic?.npu?.npu_smi) return null
  const npuSmi = dynamic.npu.npu_smi as DynamicInfoData['npu']['npu_smi'] & { frequency_mhz?: number | null; parsed?: Record<string, unknown> | null }
  // Try top-level first (from history payload), then parsed
  const direct = toNumber(npuSmi.frequency_mhz)
  if (direct != null) return direct
  return toNumber((npuSmi.parsed as Record<string, unknown> | null)?.frequency_mhz as number | null)
}

function buildNpuTrendPoints(items: HistorySnapshotItem[]): NpuTrendPoint[] {
  return [...items].reverse().map((item) => {
    const dynamic = toDynamicData(item)
    const { ts, label } = buildTimestamp(item)
    return {
      timestamp: label,
      ts,
      npuUtilization: getNpuUsage(dynamic),
      npuFreqMhz: getNpuFreqMhz(dynamic),
    }
  })
}

function isIntegratedGpu(device: QmassaDevice): boolean {
  const type = `${device.dev_type || ''}`.toLowerCase()
  return type.includes('integrated') || type.includes('igpu')
}

function buildGpuTrendSeries(items: HistorySnapshotItem[]): GpuTrendSeries[] {
  const seriesMap = new Map<string, GpuTrendSeries>()

  for (const item of [...items].reverse()) {
    const dynamic = toDynamicData(item)
    if (!dynamic) continue
    const devices = dynamic.gpu?.qmassa?.parsed?.devices || []
    const { ts, label } = buildTimestamp(item)

    devices.forEach((device, index) => {
      const id = device.pci_dev || `${device.dev_type || 'gpu'}-${index}`
      const seriesLabel = getGpuLabel(device, index)
      const integrated = isIntegratedGpu(device)

      if (!seriesMap.has(id)) {
        seriesMap.set(id, { id, label: seriesLabel, isIntegrated: integrated, points: [] })
      }

      // iGPU uses system memory; dGPU uses dedicated VRAM (keyed by card index in vram map)
      const vramEntry = dynamic.gpu?.vram?.[`card${index}`]
      const memUsage = integrated
        ? normalizePercent(dynamic?.memory?.usage_percent)
        : normalizePercent(vramEntry?.usage_percent ?? dynamic?.memory?.usage_percent)

      const point: GpuTrendPoint = {
        timestamp: label,
        ts,
        vcs: normalizeEngineUtil(device, 'vcs'),
        vecs: normalizeEngineUtil(device, 'vecs'),
        ccs: normalizeEngineUtil(device, 'ccs'),
        rcs: normalizeEngineUtil(device, 'rcs'),
        bcs: normalizeEngineUtil(device, 'bcs'),
        gt0Freq: getFreq(device, 'gt0'),
        gt1Freq: getFreq(device, 'gt1'),
        gpuPower: toNumber(device.power_w?.gpu ?? null),
        pkgPower: toNumber(device.power_w?.pkg ?? null),
        memUsage,
      }

      seriesMap.get(id)?.points.push(point)
    })
  }

  return Array.from(seriesMap.values()).filter((series) => series.points.length > 0)
}

function LegendToggleItem({
  value,
  color,
  hidden,
  onClick,
  dasharray,
}: {
  value: string
  color: string
  hidden: boolean
  onClick: () => void
  dasharray?: string
}) {
  const lineColor = hidden ? COLORS.border : color
  return (
    <span
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        cursor: 'pointer',
        marginRight: 12,
        opacity: hidden ? 0.35 : 1,
        userSelect: 'none',
      }}
    >
      <svg width={24} height={6} style={{ verticalAlign: 'middle' }}>
        <line x1={0} y1={3} x2={24} y2={3} stroke={lineColor} strokeWidth={2.5} strokeDasharray={dasharray} />
      </svg>
      <span style={{ color: hidden ? 'rgba(174,191,223,0.3)' : COLORS.textMuted, fontSize: 11, textDecoration: hidden ? 'line-through' : 'none' }}>{value}</span>
    </span>
  )
}

function GpuHistoryCard({ series }: { series: GpuTrendSeries }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const toggle = useCallback((key: string) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const memLabel = series.isIntegrated ? 'Sys Mem %' : 'VRAM %'

  const engineLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
    ...ENGINE_CURVE_META.map((e) => ({ key: e.key, name: e.name, color: e.color, yAxisId: 'util' })),
    { key: 'memUsage', name: memLabel, color: COLORS.yellow, dasharray: '6 3', yAxisId: 'util' },
    { key: 'gt0Freq', name: 'GT0 MHz', color: COLORS.text, dasharray: '6 4', yAxisId: 'freq' },
    { key: 'gt1Freq', name: 'GT1 MHz', color: COLORS.textMuted, dasharray: '4 4', yAxisId: 'freq' },
  ]

  const pkgPowerLabel = series.isIntegrated ? 'Pkg Power W' : 'Card Power W'
  const powerLines: Array<{ key: string; name: string; color: string; dasharray?: string }> = [
    { key: 'gpuPower', name: 'GPU Power W', color: COLORS.accent },
    { key: 'pkgPower', name: pkgPowerLabel, color: COLORS.orange, dasharray: '5 3' },
  ]

  return (
    <Card
      style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginBottom: 16 }}
      bodyStyle={{ padding: 16 }}
    >
      {/* Chart 1: Engine utilization + Mem% + Frequency */}
      <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
        {series.label} — Engine Utilization &amp; GT Frequency
      </Text>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
        {engineLines.map((l) => (
          <LegendToggleItem
            key={l.key}
            value={l.name}
            color={l.color}
            hidden={hidden.has(l.key)}
            onClick={() => toggle(l.key)}
            dasharray={l.dasharray}
          />
        ))}
      </div>
      <div style={{ width: '100%', height: 300 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series.points} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              minTickGap={36}
              tickFormatter={formatHistoryAxisTick}
            />
            <YAxis
              yAxisId="util"
              domain={[0, 100]}
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              tickFormatter={(val: string | number) => `${val}%`}
            />
            <YAxis
              yAxisId="freq"
              orientation="right"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
            />
            <Tooltip
              contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
              formatter={tooltipFmt2}
            />
            {engineLines.map((l) => (
              <Line
                key={l.key}
                type="monotone"
                yAxisId={l.yAxisId}
                dataKey={l.key}
                name={l.name}
                stroke={l.color}
                strokeDasharray={l.dasharray}
                dot={false}
                strokeWidth={2}
                hide={hidden.has(l.key)}
              />
            ))}
            <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Chart 2: Power */}
      <Text style={{ color: COLORS.textMuted, display: 'block', marginTop: 16, marginBottom: 6 }}>
        {series.label} — Power (W)
      </Text>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
        {powerLines.map((l) => (
          <LegendToggleItem
            key={l.key}
            value={l.name}
            color={l.color}
            hidden={hidden.has(l.key)}
            onClick={() => toggle(l.key)}
            dasharray={l.dasharray}
          />
        ))}
      </div>
      <div style={{ width: '100%', height: 180 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series.points} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
            <XAxis
              dataKey="timestamp"
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              minTickGap={36}
              tickFormatter={formatHistoryAxisTick}
            />
            <YAxis
              tick={{ fill: COLORS.textMuted, fontSize: 11 }}
              tickFormatter={(val: string | number) => `${val}W`}
            />
            <Tooltip
              contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
              formatter={(val: number, name: string) => [`${typeof val === 'number' ? val.toFixed(2) : val} W`, name]}
            />
            {powerLines.map((l) => (
              <Line
                key={l.key}
                type="monotone"
                dataKey={l.key}
                name={l.name}
                stroke={l.color}
                strokeDasharray={l.dasharray}
                dot={false}
                strokeWidth={2}
                hide={hidden.has(l.key)}
              />
            ))}
            <Brush dataKey="timestamp" height={20} stroke={COLORS.accent} travellerWidth={8} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

export default function HistoryDashboard({ active }: Props) {
  const [limit, setLimit] = useState<number>(100)
  const [rangePreset, setRangePreset] = useState<RangePreset>('15m')
  const [customRange, setCustomRange] = useState<[Dayjs | null, Dayjs | null] | null>(null)
  const [history, setHistory] = useState<HistoryData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchAt, setLastFetchAt] = useState<string | null>(null)

  // Retention settings state
  const [retention, setRetention] = useState<HistoryRetentionData | null>(null)
  const [pendingRetentionDays, setPendingRetentionDays] = useState<number | null>(null)
  const [savingRetention, setSavingRetention] = useState(false)
  const [messageApi, messageContextHolder] = message.useMessage()

  const customRangeReady = useMemo(() => {
    if (rangePreset !== 'custom') return true
    const start = customRange?.[0]
    const end = customRange?.[1]
    return Boolean(start && end && end.unix() >= start.unix())
  }, [rangePreset, customRange])

  const fetchHistory = useCallback(async () => {
    if (!active) return
    if (rangePreset === 'custom' && !customRangeReady) return
    setLoading(true)
    setHistory(null)

    const nowSec = Math.floor(Date.now() / 1000)
    let startTime: number | null = null
    let endTime: number | null = null

    if (rangePreset === 'custom') {
      const start = customRange?.[0]
      const end = customRange?.[1]
      if (start && end) {
        startTime = start.unix()
        endTime = end.unix()
      }
    } else {
      const preset = rangePreset as Exclude<RangePreset, 'custom'>
      const seconds = RANGE_SECONDS[preset]
      startTime = nowSec - seconds
      endTime = nowSec
    }

    const queryLimit = Math.max(limit, estimateRequiredLimit(rangePreset, customRange))

    try {
      const data = await api.getHistory({
        snapshotType: 'dynamic',
        limit: queryLimit,
        startTime,
        endTime,
      })
      setHistory(data)
      setError(null)
      setLastFetchAt(dayjs().format('YYYY-MM-DD HH:mm:ss'))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch history data')
    } finally {
      setLoading(false)
    }
  }, [active, limit, rangePreset, customRange, customRangeReady])

  useEffect(() => {
    if (!active) return
    if (rangePreset === 'custom' && !customRangeReady) return
    fetchHistory()
  }, [active, fetchHistory, rangePreset, customRangeReady])

  const fetchRetention = useCallback(async () => {
    if (!active) return
    try {
      const data = await api.getHistoryRetention()
      setRetention(data)
      setPendingRetentionDays(data.retention_days)
    } catch {
      // Non-critical — ignore errors silently
    }
  }, [active])

  useEffect(() => {
    if (!active) return
    fetchRetention()
  }, [active, fetchRetention])

  const handleSaveRetention = useCallback(async () => {
    if (pendingRetentionDays === null) return
    setSavingRetention(true)
    try {
      const result = await api.setHistoryRetention(pendingRetentionDays)
      setRetention((prev) => prev ? { ...prev, retention_days: result.retention_days } : prev)
      const msg = result.deleted
        ? `Retention set to ${result.retention_days} day(s). ${result.deleted} old record(s) deleted.`
        : `Retention set to ${result.retention_days} day(s).`
      messageApi.success(msg)
      // Refresh history so the UI reflects any newly-removed rows.
      fetchHistory()
    } catch (e: unknown) {
      messageApi.error(e instanceof Error ? e.message : 'Failed to save retention setting')
    } finally {
      setSavingRetention(false)
    }
  }, [pendingRetentionDays, messageApi, fetchHistory])

  const dynamicItems = useMemo<HistorySnapshotItem[]>(
    () => (history?.items ?? []).filter((item: HistorySnapshotItem) => item.snapshot_type === 'dynamic'),
    [history],
  )

  const pressureTrendPoints = useMemo(() => buildPressureTrendPoints(dynamicItems), [dynamicItems])
  const cpuMemTrendPoints = useMemo(() => buildCpuMemTrendPoints(dynamicItems), [dynamicItems])
  const { points: diskTrendPoints, diskNames } = useMemo(() => buildDiskTrendPoints(dynamicItems), [dynamicItems])
  const { points: networkTrendPoints, nicNames } = useMemo(() => buildNetworkTrendPoints(dynamicItems), [dynamicItems])
  const npuTrendPoints = useMemo(() => buildNpuTrendPoints(dynamicItems), [dynamicItems])
  const gpuTrendSeries = useMemo(() => buildGpuTrendSeries(dynamicItems), [dynamicItems])

  const hasNpuData = useMemo(() => npuTrendPoints.some((p) => p.npuUtilization != null || p.npuFreqMhz != null), [npuTrendPoints])

  const [pressureHidden, setPressureHidden] = useState<Set<string>>(new Set())
  const togglePressure = useCallback((key: string) => {
    setPressureHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [cpuMemHidden, setCpuMemHidden] = useState<Set<string>>(new Set())
  const toggleCpuMem = useCallback((key: string) => {
    setCpuMemHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [diskHidden, setDiskHidden] = useState<Set<string>>(new Set())
  const toggleDisk = useCallback((key: string) => {
    setDiskHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [networkHidden, setNetworkHidden] = useState<Set<string>>(new Set())
  const toggleNetwork = useCallback((key: string) => {
    setNetworkHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const rangeHint = useMemo(() => {
    if (rangePreset !== 'custom') return `Range: ${rangePreset}`
    const start = customRange?.[0]
    const end = customRange?.[1]
    if (!start || !end) return 'Range: custom (select start/end)'
    return `Range: ${start.format('MM-DD HH:mm')} ~ ${end.format('MM-DD HH:mm')}`
  }, [rangePreset, customRange])

  return (
    <div style={{ padding: '16px 0' }}>
      {messageContextHolder}
      {error && (
        <Alert
          message="History API Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
        <Title level={5} style={{ color: COLORS.text, margin: 0 }}>
          History Trends
        </Title>

        <Space size={12} wrap>
          <Segmented
            options={RANGE_PRESET_OPTIONS}
            value={rangePreset}
            onChange={(val: string | number) => setRangePreset(val as RangePreset)}
          />

          {rangePreset === 'custom' && (
            <DatePicker.RangePicker
              showTime={{ format: 'HH:mm' }}
              allowClear
              format="MM-DD HH:mm"
              value={customRange}
              onCalendarChange={(values) => {
                // Auto-apply as soon as both dates are selected, no need to click OK
                if (values?.[0] && values?.[1]) {
                  setCustomRange([values[0], values[1]])
                }
              }}
              onChange={(values) => {
                setCustomRange(values ? [values[0], values[1]] : null)
              }}
            />
          )}

          <Select
            value={limit}
            onChange={setLimit}
            style={{ width: 120 }}
            options={LIMIT_OPTIONS.map((val) => ({ label: `${val} rows`, value: val }))}
          />

          <Button icon={<ReloadOutlined />} onClick={() => fetchHistory()} disabled={rangePreset === 'custom' && !customRangeReady}>
            Manual Refresh
          </Button>

          <Popover
            trigger="click"
            title="History Data Retention"
            content={
              <div style={{ minWidth: 260 }}>
                <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 8 }}>
                  Snapshots older than the selected period are automatically deleted
                  (once at startup and then every hour). Changing the setting triggers
                  an immediate cleanup.
                </Text>
                <Divider style={{ margin: '8px 0' }} />
                <Space>
                  <Text style={{ color: COLORS.text }}>Retain for:</Text>
                  <Select
                    value={pendingRetentionDays ?? retention?.retention_days ?? 3}
                    onChange={setPendingRetentionDays}
                    style={{ width: 110 }}
                    options={Array.from(
                      { length: (retention?.max_days ?? 7) - (retention?.min_days ?? 1) + 1 },
                      (_, i) => {
                        const d = (retention?.min_days ?? 1) + i
                        return { value: d, label: `${d} day${d === 1 ? '' : 's'}` }
                      },
                    )}
                  />
                  <Button
                    type="primary"
                    size="small"
                    loading={savingRetention}
                    disabled={
                      pendingRetentionDays === null ||
                      pendingRetentionDays === retention?.retention_days
                    }
                    onClick={handleSaveRetention}
                  >
                    Save
                  </Button>
                </Space>
              </div>
            }
          >
            <Button icon={<SettingOutlined />} />
          </Popover>
        </Space>
      </div>

      <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 12 }}>
        {rangeHint}{lastFetchAt ? ` | Last fetch: ${lastFetchAt}` : ''}{retention ? ` | Retention: ${retention.retention_days} day${retention.retention_days === 1 ? '' : 's'}` : ''}
      </Text>

      {/* System Pressure */}
      <Card
        style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6 }}
        bodyStyle={{ padding: 16 }}
      >
        <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
          System Pressure
        </Text>
        {pressureTrendPoints.length > 0 && (() => {
          const lines: Array<{ key: string; name: string; color: string; dasharray?: string }> = [
            { key: 'systemPressure', name: 'System Pressure %', color: COLORS.accent },
            { key: 'diskPressure', name: 'Disk Pressure %', color: COLORS.red, dasharray: '5 3' },
            { key: 'networkPressure', name: 'Network Pressure %', color: COLORS.green, dasharray: '5 3' },
          ]
          return (
            <>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                {lines.map((l) => (
                  <LegendToggleItem
                    key={l.key}
                    value={l.name}
                    color={l.color}
                    hidden={pressureHidden.has(l.key)}
                    onClick={() => togglePressure(l.key)}
                    dasharray={l.dasharray}
                  />
                ))}
              </div>
              <div style={{ width: '100%', height: 280 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={pressureTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                    <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="timestamp"
                      tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                      minTickGap={36}
                      tickFormatter={formatHistoryAxisTick}
                    />
                    <YAxis
                      domain={[0, 100]}
                      tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                      tickFormatter={(val: string | number) => `${val}%`}
                    />
                    <Tooltip
                      contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                      formatter={tooltipFmt2}
                    />
                    {lines.map((l) => (
                      <Line
                        key={l.key}
                        type="monotone"
                        dataKey={l.key}
                        name={l.name}
                        stroke={l.color}
                        strokeDasharray={l.dasharray}
                        dot={false}
                        strokeWidth={2}
                        hide={pressureHidden.has(l.key)}
                      />
                    ))}
                    <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </>
          )
        })()}
        {loading && (
          <div style={{ color: COLORS.textMuted, paddingTop: 48, textAlign: 'center' }}>Loading history...</div>
        )}
        {!loading && pressureTrendPoints.length === 0 && (
          <Empty description="No history data" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        )}
      </Card>

      {/* CPU & Memory Utilization */}
      {!loading && cpuMemTrendPoints.length > 0 && (
        <Card
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            CPU &amp; Memory Utilization
          </Text>
          {(() => {
            const lines: Array<{ key: string; name: string; color: string; dasharray?: string }> = [
              { key: 'cpuUtilization', name: 'CPU Total %', color: COLORS.accent },
              { key: 'pCoreUtilization', name: 'P-Core %', color: COLORS.orange, dasharray: '6 3' },
              { key: 'eCoreUtilization', name: 'E-Core %', color: COLORS.green, dasharray: '6 3' },
              { key: 'memoryUtilization', name: 'Memory %', color: COLORS.yellow },
            ]
            return (
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {lines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={cpuMemHidden.has(l.key)}
                      onClick={() => toggleCpuMem(l.key)}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 280 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={cpuMemTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {lines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                          hide={cpuMemHidden.has(l.key)}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
            )
          })()}
        </Card>
      )}

      {/* Disk — Utilization & Bandwidth per device */}
      {!loading && diskNames.length > 0 && diskNames.map((diskName) => {
        const diskLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
          { key: `${diskName}:util`, name: 'Util %', color: METRIC_COLORS.util, yAxisId: 'util' },
          { key: `${diskName}:read`, name: 'Read MB/s', color: METRIC_COLORS.read, yAxisId: 'bw' },
          { key: `${diskName}:write`, name: 'Write MB/s', color: METRIC_COLORS.write, yAxisId: 'bw' },
        ]
        return (
        <Card
          key={`disk-${diskName}`}
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            Disk I/O ({diskName})
          </Text>
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {diskLines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={diskHidden.has(l.key)}
                      onClick={() => toggleDisk(l.key)}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 300 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={diskTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        yAxisId="util"
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                      />
                      <YAxis
                        yAxisId="bw"
                        orientation="right"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => typeof val === 'number' ? val.toFixed(1) : `${val}`}
                        label={{ value: 'MB/s', angle: -90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {diskLines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          yAxisId={l.yAxisId}
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                          hide={diskHidden.has(l.key)}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
        </Card>
        )
      })}

      {/* Network — Utilization & Bandwidth per NIC */}
      {!loading && nicNames.length > 0 && nicNames.map((nicName) => {
        const nicLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
          { key: `${nicName}:util`, name: 'Util %', color: METRIC_COLORS.util, yAxisId: 'util' },
          { key: `${nicName}:rx`, name: 'RX Mbps', color: METRIC_COLORS.rx, yAxisId: 'bw' },
          { key: `${nicName}:tx`, name: 'TX Mbps', color: METRIC_COLORS.tx, yAxisId: 'bw' },
        ]
        return (
        <Card
          key={`net-${nicName}`}
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            Network I/O ({nicName})
          </Text>
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {nicLines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={networkHidden.has(l.key)}
                      onClick={() => toggleNetwork(l.key)}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 300 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={networkTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        yAxisId="util"
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                      />
                      <YAxis
                        yAxisId="bw"
                        orientation="right"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => typeof val === 'number' ? val.toFixed(1) : `${val}`}
                        label={{ value: 'Mbps', angle: -90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {nicLines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          yAxisId={l.yAxisId}
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                          hide={networkHidden.has(l.key)}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
        </Card>
        )
      })}

      {/* NPU Utilization & Frequency */}
      {!loading && hasNpuData && (
        <Card
          style={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, borderRadius: 6, marginTop: 16 }}
          bodyStyle={{ padding: 16 }}
        >
          <Text style={{ color: COLORS.textMuted, display: 'block', marginBottom: 6 }}>
            NPU — Utilization &amp; Frequency
          </Text>
          {(() => {
            const npuLines: Array<{ key: string; name: string; color: string; dasharray?: string; yAxisId: string }> = [
              { key: 'npuUtilization', name: 'NPU Utilization %', color: COLORS.accent, yAxisId: 'util' },
              { key: 'npuFreqMhz', name: 'NPU Freq MHz', color: COLORS.orange, dasharray: '6 3', yAxisId: 'freq' },
            ]
            return (
              <>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 0', marginBottom: 8 }}>
                  {npuLines.map((l) => (
                    <LegendToggleItem
                      key={l.key}
                      value={l.name}
                      color={l.color}
                      hidden={false}
                      onClick={() => {}}
                      dasharray={l.dasharray}
                    />
                  ))}
                </div>
                <div style={{ width: '100%', height: 260 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={npuTrendPoints} margin={{ top: 4, right: 20, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke={`${COLORS.border}99`} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="timestamp"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        minTickGap={36}
                        tickFormatter={formatHistoryAxisTick}
                      />
                      <YAxis
                        yAxisId="util"
                        domain={[0, 100]}
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        tickFormatter={(val: string | number) => `${val}%`}
                      />
                      <YAxis
                        yAxisId="freq"
                        orientation="right"
                        tick={{ fill: COLORS.textMuted, fontSize: 11 }}
                        label={{ value: 'MHz', angle: -90, position: 'insideRight', fill: COLORS.textMuted, fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: COLORS.panelBg, border: `1px solid ${COLORS.border}`, color: COLORS.text }}
                        formatter={tooltipFmt2}
                      />
                      {npuLines.map((l) => (
                        <Line
                          key={l.key}
                          type="monotone"
                          yAxisId={l.yAxisId}
                          dataKey={l.key}
                          name={l.name}
                          stroke={l.color}
                          strokeDasharray={l.dasharray}
                          dot={false}
                          strokeWidth={2}
                        />
                      ))}
                      <Brush dataKey="timestamp" height={24} stroke={COLORS.accent} travellerWidth={8} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </>
            )
          })()}
        </Card>
      )}

      {/* GPU History */}
      <div style={{ marginTop: 16 }}>
        {loading ? (
          <Card
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
            }}
            bodyStyle={{ padding: 16 }}
          >
            <div style={{ color: COLORS.textMuted, textAlign: 'center' }}>Loading GPU history...</div>
          </Card>
        ) : gpuTrendSeries.length === 0 ? (
          <Card
            style={{
              background: COLORS.panelBg,
              border: `1px solid ${COLORS.border}`,
              borderRadius: 6,
            }}
            bodyStyle={{ padding: 16 }}
          >
            <Empty description="No GPU history data" image={Empty.PRESENTED_IMAGE_SIMPLE} />
          </Card>
        ) : (
          gpuTrendSeries.map((series: GpuTrendSeries) => (
            <GpuHistoryCard key={series.id} series={series} />
          ))
        )}
      </div>
    </div>
  )
}
