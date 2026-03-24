import axios from 'axios'
import type {
  ApiResponse,
  NetworkData,
  PressureData,
  TopConsumersData,
  TopDiskIoConsumersData,
  ProcessListData,
  AppListData,
  StaticInfoData,
  DynamicInfoData,
  HistoryData,
  HistoryQueryOptions,
  SetControlPayload,
  AppIdPayload,
  SetPriorityPayload,
  ResourceLimitPayload,
} from './types'

const client = axios.create({
  baseURL: '/api',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
})

async function get<T>(url: string): Promise<T> {
  const res = await client.get<ApiResponse<T>>(url)
  if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
  return res.data.data
}

async function post<T>(url: string, body: object = {}): Promise<T> {
  const res = await client.post<ApiResponse<T>>(url, body)
  if (res.data.retcode !== 0) throw new Error(res.data.retmsg)
  return res.data.data
}

export const api = {
  getNetwork: () => get<NetworkData>('/monitor/network'),
  getPressure: () => get<PressureData>('/monitor/pressure'),
  getTopConsumers: () => get<TopConsumersData>('/monitor/top_consumers'),
  getTopDiskIoConsumers: () => get<TopDiskIoConsumersData>('/monitor/top_disk_io_consumers'),
  getProcesses: () => get<ProcessListData>('/monitor/processes'),
  getStaticInfo: () => get<StaticInfoData>('/monitor/static_info'),
  refreshStaticInfo: () => get<StaticInfoData>('/monitor/static_info?force_refresh=1'),
  getDynamicInfo: () => get<DynamicInfoData>('/monitor/dynamic_info'),
  getHistory: (options: HistoryQueryOptions = {}) => {
    const snapshotType = options.snapshotType ?? 'dynamic'
    const limit = Math.max(1, Math.min(options.limit ?? 100, 20000))
    const params = new URLSearchParams({
      snapshot_type: snapshotType,
      limit: String(limit),
    })

    if (typeof options.startTime === 'number' && Number.isFinite(options.startTime)) {
      params.set('start_time', String(Math.floor(options.startTime)))
    }
    if (typeof options.endTime === 'number' && Number.isFinite(options.endTime)) {
      params.set('end_time', String(Math.floor(options.endTime)))
    }

    return get<HistoryData>(`/monitor/history?${params.toString()}`)
  },

  getApps: () => post<AppListData>('/app/get_apps'),
  getControlledApps: () => post<AppListData>('/app/get_controlled_app'),
  // The server returns retcode=404 (NOT_EXISTING, "No pending apps found") when the
  // pending queue is empty, which makes post() throw.  Treat that specific case as an
  // empty list so the UI clears the pending queue card when the last app goes running.
  // Other errors (network failures, server errors) are re-thrown so callers can handle them.
  getPendingApps: () =>
    post<AppListData>('/app/get_pending_app').catch((e: Error) => {
      if (e.message === 'No pending apps found') return [] as AppListData
      throw e
    }),

  setToControl: (payload: SetControlPayload) =>
    post<void>('/app/set_to_control', payload),
  removeFromControl: (payload: AppIdPayload) =>
    post<void>('/app/remove_from_control', payload),
  setPriority: (payload: SetPriorityPayload) =>
    post<void>('/app/set_priority', payload),
  setOomScore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/set_oom_score', payload),
  cancelRelaunch: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/cancel_relaunch', payload),
  resourceLimit: (payload: ResourceLimitPayload) =>
    post<void>('/app/resource_limit', payload),
  resourceRestore: (payload: Pick<AppIdPayload, 'app_id'>) =>
    post<void>('/app/resource_restore', payload),
}
