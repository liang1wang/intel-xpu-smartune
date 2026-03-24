#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import json
import threading
import time
from flask import Blueprint, request

import psutil

from db.DatabaseModel import MonitorSnapshot
from monitor import ResourceMonitor, PSIMonitor, NetworkMonitor, PressureAnalyzer
from monitor.system_info import collect_static_info, collect_dynamic_info
from utils.http_utils import RetCode, construct_response
from utils.logger import logger

monitor_bp = Blueprint('monitor', __name__, url_prefix='/monitor')

_resource_monitor = None
_network_monitor = None
_system_pressure_monitor = None


def _get_resource_monitor() -> ResourceMonitor:
    """Return the shared ResourceMonitor instance, creating it if needed."""
    global _resource_monitor
    if _resource_monitor is None:
        _resource_monitor = ResourceMonitor()
    return _resource_monitor


def _get_network_monitor() -> NetworkMonitor:
    """Return the shared NetworkMonitor instance, creating it if needed."""
    global _network_monitor
    if _network_monitor is None:
        _network_monitor = NetworkMonitor()
    return _network_monitor


def _get_system_pressure_monitor():
    """Return the shared SystemPressureMonitor instance, creating it if needed."""
    global _system_pressure_monitor
    if _system_pressure_monitor is None:
        from config.config import b_config
        _system_pressure_monitor = SystemPressureMonitor(b_config)
    return _system_pressure_monitor


def register_system_pressure_monitor(spm) -> None:
    """Register an externally-created SystemPressureMonitor instance as the shared singleton.

    Call this once during application startup (after the balancer's ControlManager is
    initialised) so that the monitor API endpoints and collect_dynamic_info always return
    the same pressure data as the balancer's own decision logic.
    """
    global _system_pressure_monitor
    _system_pressure_monitor = spm


@monitor_bp.route('/cpu', methods=['GET', 'POST'])
def get_cpu():
    """
    Return current CPU usage statistics.

    Response data:
        {
            "count": <int>,        # number of logical CPU cores
            "usage": <float>,      # overall CPU usage in percent (0-100)
            "available": <float>,  # remaining CPU capacity in percent (0-100)
            "is_busy": <bool>      # True when usage exceeds the configured threshold
        }
    """
    try:
        monitor = _get_resource_monitor()
        usage = monitor.get_resource_usage()
        return construct_response(
            data=usage['cpu'],
            retmsg="Successfully retrieved CPU usage"
        )
    except Exception as e:
        logger.error(f"get_cpu failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/memory', methods=['GET'])
def get_memory():
    """
    Return current memory usage statistics.

    Response data:
        {
            "total_gb": <float>,        # total physical memory in GB
            "usage": <float>,           # memory usage in percent (0-100)
            "available_ratio": <float>, # fraction of memory still available (0-1)
            "is_busy": <bool>           # True when usage exceeds the configured threshold
        }
    """
    try:
        monitor = _get_resource_monitor()
        usage = monitor.get_resource_usage()
        return construct_response(
            data=usage['memory'],
            retmsg="Successfully retrieved memory usage"
        )
    except Exception as e:
        logger.error(f"get_memory failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/disk', methods=['GET'])
def get_disk():
    """
    Return disk I/O statistics and stress assessment.

    Response data:
        {
            "disk_io": {
                "<device>": {
                    "utilization": <float>,       # disk utilization in percent (0-100)
                    "is_busy": <bool>,            # True when utilization exceeds threshold
                    "read_kb_per_sec": <float>,   # read throughput in KB/s
                    "write_kb_per_sec": <float>   # write throughput in KB/s
                },
                ...
            },
            "is_stressed": <bool>,          # True when disk I/O is under stress
            "stressed_disks": [<str>, ...], # list of stressed device names
            "iowait": <float>               # system iowait percentage
        }
    """
    try:
        monitor = _get_resource_monitor()
        disk_stress = monitor.is_disk_io_stressed()
        disk_stats = monitor.get_disk_stats()
        return construct_response(
            data={
                **disk_stats,
                'is_stressed': disk_stress['is_stressed'],
                'stressed_disks': disk_stress['stressed_disks'],
                'iowait': disk_stress['iowait'],
            },
            retmsg="Successfully retrieved disk I/O stats"
        )
    except Exception as e:
        logger.error(f"get_disk failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/network', methods=['GET'])
def get_network():
    """
    Return current network utilization with pressure levels.

    Response data:
        {
            "rx": <float>,       # receive-side utilization (0-1, fraction of max bandwidth)
            "tx": <float>,       # transmit-side utilization (0-1, fraction of max bandwidth)
            "rx_level": <str>,   # pressure level for rx: "low"/"medium"/"high"/"critical"
            "tx_level": <str>    # pressure level for tx: "low"/"medium"/"high"/"critical"
        }
    """
    try:
        net_monitor = _get_network_monitor()
        network_data = net_monitor.get_current_pressure()
        spm = _get_system_pressure_monitor()
        tx_level, rx_level, tx_val, rx_val = spm.update_network_pressure_level(network_data)
        return construct_response(
            data={
                'rx': rx_val,
                'tx': tx_val,
                'rx_level': rx_level,
                'tx_level': tx_level,
            },
            retmsg="Successfully retrieved network pressure"
        )
    except Exception as e:
        logger.error(f"get_network failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/pressure', methods=['GET'])
def get_pressure():
    """
    Return overall system pressure level with PSI metrics.

    Response data:
        {
            "level": <str>,              # overall level: "low"/"medium"/"high"/"critical"
            "score": <float>,            # composite pressure score (0-1)
            "is_disk_io_stressed": <bool>,
            "cpu": <float>,              # CPU PSI pressure (0-1)
            "memory": <float>,           # memory PSI pressure (0-1)
            "io": <float>                # I/O PSI pressure (0-1)
        }
    """
    try:
        spm = _get_system_pressure_monitor()
        level, score, is_disk_io_stressed, psi_data = spm.get_current_pressure_level()
        return construct_response(
            data={
                'level': level,
                'score': score,
                'is_disk_io_stressed': is_disk_io_stressed,
                'cpu': psi_data.get('cpu', 0.0),
                'memory': psi_data.get('memory', 0.0),
                'io': psi_data.get('io', 0.0),
            },
            retmsg="Successfully retrieved system pressure"
        )
    except Exception as e:
        logger.error(f"get_pressure failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/summary', methods=['GET'])
def get_summary():
    """
    Return a combined snapshot of all system resource statistics.

    Response data:
        {
            "cpu": { ... },      # same as GET /monitor/cpu
            "memory": { ... },   # same as GET /monitor/memory
            "disk": { ... },     # same as GET /monitor/disk
            "network": { ... },  # same as GET /monitor/network
            "pressure": { ... }  # same as GET /monitor/pressure
        }
    """
    try:
        monitor = _get_resource_monitor()
        net_monitor = _get_network_monitor()

        resource_usage = monitor.get_resource_usage()
        disk_stats = monitor.get_disk_stats()
        net_pressure = net_monitor.get_current_pressure()

        try:
            psi_pressure = PSIMonitor().get_current_pressure()
        except Exception as psi_err:
            logger.warning(f"PSI unavailable: {psi_err}")
            psi_pressure = {'cpu': 0.0, 'memory': 0.0, 'io': 0.0}

        summary = {
            'cpu': resource_usage['cpu'],
            'memory': resource_usage['memory'],
            'disk': disk_stats,
            'network': net_pressure,
            'pressure': psi_pressure,
        }
        return construct_response(
            data=summary,
            retmsg="Successfully retrieved system summary"
        )
    except Exception as e:
        logger.error(f"get_summary failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/top_consumers', methods=['GET'])
def get_top_consumers():
    """
    Return the top resource-consuming processes along with their matched application info.

    Response data:
        [
            {
                "process": {
                    "pid": <int>,
                    "name": <str>,
                    "cmdline": <str>,
                    "score": <float>,
                    "cpu_avg": <float>,
                    "mem_rss": <float>,
                    "io_read_rate": <float>
                },
                "app": <dict|null>  # matched desktop/systemd/cgroup app info
            },
            ...
        ]
    """
    try:
        monitor = _get_resource_monitor()
        consumers, reach_threshold = monitor.get_top_resource_consumers()
        return construct_response(
            data={
                'consumers': consumers,
                'reach_threshold': reach_threshold,
            },
            retmsg="Successfully retrieved top resource consumers"
        )
    except Exception as e:
        logger.error(f"get_top_consumers failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/top_disk_io_consumers', methods=['GET'])
def get_top_disk_io_consumers():
    """
    Return the top disk I/O-consuming processes along with their matched application info.

    Response data:
        {
            "consumers": [
                {
                    "process": {
                        "pid": <int>,
                        "name": <str>,
                        "cmdline": <str>,
                        "score": <float>,
                        "io_read_rate": <float>,   # MB/s
                        "io_write_rate": <float>   # MB/s
                    },
                    "app": <dict|null>
                },
                ...
            ]
        }
    """
    try:
        monitor = _get_resource_monitor()
        consumers = monitor.get_top_disk_io_consumers()
        return construct_response(
            data={'consumers': consumers},
            retmsg="Successfully retrieved top disk I/O consumers"
        )
    except Exception as e:
        logger.error(f"get_top_disk_io_consumers failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/processes', methods=['GET'])
def get_processes():
    """
    Return a list of all running processes sorted by CPU usage, similar to the top command.

    Response data:
        {
            "count": <int>,
            "processes": [
                {
                    "pid": <int>,
                    "name": <str>,
                    "username": <str>,
                    "cpu_percent": <float>,    # CPU usage percent
                    "memory_percent": <float>, # memory usage percent
                    "mem_rss_kb": <float>,     # resident set size in KB
                    "status": <str>,           # process status (running/sleeping/...)
                    "cmdline": <str>           # full command line
                },
                ...
            ]
        }
    """
    try:
        procs = []
        attrs = ['pid', 'name', 'username', 'cpu_percent', 'memory_percent',
                 'status', 'cmdline', 'memory_info']
        for p in psutil.process_iter(attrs):
            try:
                info = p.info
                mem_rss_kb = round(info['memory_info'].rss / 1024, 0) if info.get('memory_info') else 0
                cmdline_parts = info.get('cmdline') or []
                cmdline = ' '.join(cmdline_parts) if cmdline_parts else (info.get('name') or '')
                procs.append({
                    'pid': info['pid'],
                    'name': info.get('name') or '',
                    'username': info.get('username') or '',
                    'cpu_percent': round(info.get('cpu_percent') or 0, 1),
                    'memory_percent': round(info.get('memory_percent') or 0, 2),
                    'mem_rss_kb': mem_rss_kb,
                    'status': info.get('status') or '',
                    'cmdline': cmdline,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
        return construct_response(
            data={'count': len(procs), 'processes': procs},
            retmsg="Successfully retrieved process list"
        )
    except Exception as e:
        logger.error(f"get_processes failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/static_info', methods=['GET'])
def get_static_info():
    """
    Return static system configuration info.

    Response data:
        {
            "bios": { ... },
            "os": { ... },
            "driver": { ... },
            "cpu": { ... },
            "memory": { ... },
            "io": { ... },
            "gpu": { ... },
            "npu": { ... },
            "collected_at": <str>
        }
    """
    try:
        force_raw = (request.args.get('force_refresh') or '').strip().lower()
        force_refresh = force_raw in {'1', 'true', 'yes', 'y', 'on'}
        data = collect_static_info(force_refresh=force_refresh)
        return construct_response(
            data=data,
            retmsg="Successfully retrieved static system info"
        )
    except Exception as e:
        logger.error(f"get_static_info failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/dynamic_info', methods=['GET'])
def get_dynamic_info():
    """
    Return dynamic system metrics snapshot.

    Response data:
        {
            "cpu": { ... },
            "memory": { ... },
            "pressure": { ... },
            "network": { ... },
            "disk": { ... },
            "gpu": { ... },
            "npu": { ... },
            "collected_at": <str>
        }
    """
    try:
        monitor = _get_resource_monitor()
        spm = _get_system_pressure_monitor()
        net = _get_network_monitor()
        data = collect_dynamic_info(resource_monitor=monitor, system_pressure_monitor=spm, network_monitor=net)
        return construct_response(
            data=data,
            retmsg="Successfully retrieved dynamic system info"
        )
    except Exception as e:
        logger.error(f"get_dynamic_info failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


@monitor_bp.route('/history', methods=['GET'])
def get_history():
    try:
        snapshot_type = (request.args.get('snapshot_type') or '').strip().lower()
        if snapshot_type in ('', 'all'):
            snapshot_type = None
        elif snapshot_type not in ('static', 'dynamic'):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="snapshot_type must be one of: static, dynamic, all"
            )

        limit_raw = request.args.get('limit', '100')
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="limit must be an integer"
            )

        limit = max(1, min(limit, 20000))

        start_raw = (request.args.get('start_time') or '').strip()
        end_raw = (request.args.get('end_time') or '').strip()

        start_time = None
        end_time = None

        if start_raw:
            try:
                start_time = int(start_raw)
            except (TypeError, ValueError):
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="start_time must be a unix timestamp (seconds)"
                )

        if end_raw:
            try:
                end_time = int(end_raw)
            except (TypeError, ValueError):
                return construct_response(
                    data={},
                    retcode=RetCode.ARGUMENT_ERROR,
                    retmsg="end_time must be a unix timestamp (seconds)"
                )

        if start_time is not None and end_time is not None and start_time > end_time:
            return construct_response(
                data={},
                retcode=RetCode.ARGUMENT_ERROR,
                retmsg="start_time must be less than or equal to end_time"
            )

        rows = MonitorSnapshot.query_recent(
            snapshot_type=snapshot_type,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )

        items = []
        for row in rows:
            payload = None
            if row.data_json:
                try:
                    payload = json.loads(row.data_json)
                except Exception:
                    payload = row.data_json

            items.append({
                'id': row.id,
                'snapshot_type': row.snapshot_type,
                'source': row.source,
                'collected_at': row.collected_at,
                'create_time': row.create_time,
                'update_time': row.update_time,
                'create_date': str(row.create_date) if row.create_date else None,
                'update_date': str(row.update_date) if row.update_date else None,
                'data': payload,
            })

        return construct_response(
            data={
                'snapshot_type': snapshot_type or 'all',
                'limit': limit,
                'start_time': start_time,
                'end_time': end_time,
                'count': len(items),
                'items': items,
            },
            retmsg="Successfully retrieved monitor history"
        )
    except Exception as e:
        logger.error(f"get_history failed: {str(e)}")
        return construct_response(
            data={},
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg=str(e)
        )


class SystemPressureMonitor:
    """ Manages overall system pressure state based on PSI and resource usage,
    with auto-refresh and disk I/O stress tracking."""
    def __init__(self, config):
        self.config = config
        self.psi = PSIMonitor()
        self.res = ResourceMonitor()
        self.analyzer = PressureAnalyzer(config)

        self._current_level = None
        self.is_current_disk_io_stressed = False
        self.score = 0.0
        self._psi_data = {}
        self._last_update_time = 0
        self._CACHE_TTL = config.regular_update_sys_pressure_time
        self._is_limited_app_dominant = False
        self._update_lock = threading.Lock()

        self._start_auto_refresh()

    def set_limited_app_dominant(self, is_dominant: bool):
        """设置受限应用是否占主导状态"""
        if self._is_limited_app_dominant != is_dominant:
            self._is_limited_app_dominant = is_dominant

    def _start_auto_refresh(self):
        """启动定时更新system压力状态"""
        def refresh_loop():
            while True:
                time.sleep(self._CACHE_TTL * 0.9)
                self._safe_update()

        threading.Thread(target=refresh_loop, daemon=True).start()

    def _safe_update(self):
        """线程安全的更新操作"""
        if self._update_lock.acquire(blocking=False):
            try:
                self._current_level, self.score, self.is_current_disk_io_stressed, self._psi_data = self._update_pressure_level()
            finally:
                self._update_lock.release()

    def _update_pressure_level(self) -> tuple[str, float, bool, dict]:
        """更新压力等级（使用内部状态）"""
        try:
            psi_data = self.psi.get_current_pressure()
            usage_data = self.res.get_resource_usage()
            disk_io = self.res.is_disk_io_stressed()
            score = self.analyzer.calculate_pressure_score(
                psi_data,
                usage_data,
                self._is_limited_app_dominant
            )
            logger.debug(f"disk_io={disk_io}")
            level = self.analyzer.get_pressure_level(score, self.config.thresholds)
            self._last_update_time = time.time()
            return level, score, disk_io.get("is_stressed", False), psi_data
        except Exception as e:
            logger.error("Failed to update pressure level: %s", str(e))
            return "unknown", 0.0, False, {}

    def get_current_pressure_level(self) -> tuple:
        """获取当前压力等级
        返回: (level, score, is_disk_io_stressed, psi_data)
        """
        logger.debug("Current PSI level: %s (pressure: %.2f), disk io stressed: %s", self._current_level, self.score,
                     self.is_current_disk_io_stressed)
        return self._current_level, self.score, self.is_current_disk_io_stressed, self._psi_data

    def update_network_pressure_level(self, network_data):
        """
        单独更新网络压力等级
        返回: (tx_level, rx_level, tx_value, rx_value)
        """
        try:
            tx_level = self.analyzer.get_pressure_level(network_data['tx'], self.config.network_thresholds)
            rx_level = self.analyzer.get_pressure_level(network_data['rx'], self.config.network_thresholds)
            return tx_level, rx_level, network_data['tx'], network_data['rx']
        except Exception as e:
            logger.error("Failed to update network pressure level: %s", str(e))
            return "unknown", "unknown", 0.0, 0.0
