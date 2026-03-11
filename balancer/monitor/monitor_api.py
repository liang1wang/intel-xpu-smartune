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


import threading
import time
from flask import Blueprint, jsonify

from monitor import ResourceMonitor, PSIMonitor, NetworkMonitor, PressureAnalyzer
from utils.http_utils import RetCode, construct_response
from utils.logger import logger

monitor_bp = Blueprint('monitor', __name__, url_prefix='/monitor')

_resource_monitor = None
_network_monitor = None


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
    Return disk I/O statistics for all physical disks.

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
            }
        }
    """
    try:
        monitor = _get_resource_monitor()
        disk_stats = monitor.get_disk_stats()
        return construct_response(
            data=disk_stats,
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
    Return current network utilization pressure (window average).

    Response data:
        {
            "rx": <float>,  # receive-side utilization (0-1, fraction of max bandwidth)
            "tx": <float>   # transmit-side utilization (0-1, fraction of max bandwidth)
        }
    """
    try:
        net_monitor = _get_network_monitor()
        pressure = net_monitor.get_current_pressure()
        return construct_response(
            data=pressure,
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
    Return PSI (Pressure Stall Information) for CPU, memory, and I/O.

    Response data:
        {
            "cpu": <float>,     # CPU pressure (0-1)
            "memory": <float>,  # memory pressure (0-1)
            "io": <float>       # I/O pressure (0-1)
        }
    """
    try:
        psi = PSIMonitor()
        pressure = psi.get_current_pressure()
        return construct_response(
            data=pressure,
            retmsg="Successfully retrieved PSI pressure"
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
                self._current_level, self.score, self.is_current_disk_io_stressed = self._update_pressure_level()
            finally:
                self._update_lock.release()

    def _update_pressure_level(self) -> tuple[str, float, bool]:
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
            return level, score, disk_io.get("is_stressed", False)
        except Exception as e:
            logger.error("Failed to update pressure level: %s", str(e))
            return "unknown", 0.0, False

    def get_current_pressure_level(self) -> tuple[str, bool]:
        """获取当前压力等级"""
        logger.debug("Current PSI level: %s (pressure: %.2f), disk io stressed: %s", self._current_level, self.score,
                     self.is_current_disk_io_stressed)
        return self._current_level, self.is_current_disk_io_stressed

    def update_network_pressure_level(self, network_data):
        """
        单独更新网络压力等级
        返回: (tx_level, rx_level)
        """
        try:
            tx_level = self.analyzer.get_pressure_level(network_data['tx'], self.config.network_thresholds)
            rx_level = self.analyzer.get_pressure_level(network_data['rx'], self.config.network_thresholds)
            return tx_level, rx_level
        except Exception as e:
            logger.error("Failed to update network pressure level: %s", str(e))
            return ("unknown", "unknown")
