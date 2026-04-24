# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import signal
from multiprocessing import JoinableQueue
from threading import Event, Timer
from typing import Any, List, Set, Union

import psutil
from bcc import BPF
from controller.controlManager import ControlManager
from utils import app_utils
from utils.logger import logger


# 定义与BPF代码中相同的常量
COMM_LEN = 32
PY_MAX_FILE_LEN = 64


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class AppIntercept(metaclass=SingletonMeta):
    def __init__(self, c_src_file: str = "bpf_event.c"):
        self.bpf = BPF(src_file=c_src_file, cflags=["-Wno-duplicate-decl-specifier"])
        self.controlManager = ControlManager()
        self.monitored_apps: Set[str] = set()
        self.handled_processes: Set[int] = set()  # 初始化已处理进程集合
        self.controlled_app_map = []
        self._app_map_index = {}  # 把map做成索引形式，方便查找
        self.relaunch_apps = {}
        self.app_pending_queue = JoinableQueue(1000000)
        self.monitored_app_launched = {}  # 当前已经启动的监控app
        self.pending_exit_events = {}  # 待处理的退出事件（PID: Timer）
        # Fast-lookup structures for get_main_process (rebuilt by _rebuild_match_cache)
        self._comm_to_app: dict = {}          # comm_lower -> app_name  (O(1) bpf_name exact match)
        self._filename_exe_to_app: dict = {}  # exe_lower  -> app_name  (filename path match)
        self._fuzzy_fragments: list = []      # [(fragment_lower, app_name)]  (fuzzy fallback)
        self._quick_filter: frozenset = frozenset()  # union of all above for pre-filtering

        # Event-driven critical-mode flag.  Set by _on_critical_state_changed()
        # when the system pressure monitor transitions into "critical" state;
        # cleared when it leaves.  Used in print_event to avoid issuing SIGSTOP
        # unless the system is actually under critical pressure.
        self._system_critical = Event()
        self.controlManager.register_critical_state_listener(self._on_critical_state_changed)
        # Seed the flag from the current (possibly already cached) pressure level
        # so that apps detected before the first monitor callback are handled
        # correctly at startup.  The tuple is (level, score, is_disk_io_stressed).
        initial_level, *_ = self.controlManager.get_current_pressure_level()
        if initial_level == "critical":
            self._system_critical.set()

    def rebuild_controlled_map(self):
        self.controlled_app_map = app_utils.get_controlled_apps()
        self._rebuild_index()
        self._rebuild_match_cache()

    def _on_critical_state_changed(self, is_critical: bool) -> None:
        """Callback invoked by SystemPressureMonitor when pressure enters or leaves critical.

        Sets or clears the _system_critical event so that print_event can decide
        whether to issue SIGSTOP without polling the pressure monitor on every
        BPF exec event.
        """
        if is_critical:
            self._system_critical.set()
            logger.info("System pressure entered critical – low-priority app launches will be intercepted")
        else:
            self._system_critical.clear()
            logger.info("System pressure left critical – low-priority app launch interception disabled")

    def _rebuild_index(self):
        self._app_map_index = {
            app["app_name"].lower(): app
            for app in (self.controlled_app_map or [])
            if app.get("app_name") and app["app_name"].strip()
        }

    def _rebuild_match_cache(self) -> None:
        """Pre-build fast-lookup structures used by get_main_process.

        Called whenever monitored_apps or the app config changes so that the
        hot-path (print_event → get_main_process) never rebuilds these dicts
        itself.
        """
        cnf_appname = self.controlManager.config.monitor_apps
        app_executables = {
            item['name']: item.get('bpf_name', []) for item in cnf_appname
        }

        comm_to_app: dict[str, str] = {}
        filename_exe_to_app: dict[str, str] = {}
        fuzzy_fragments: list[tuple[str, str]] = []

        for app in self.monitored_apps:
            for exe in app_executables.get(app, []):
                exe_lower = exe.lower()
                comm_to_app[exe_lower] = app
                filename_exe_to_app[exe_lower] = app
            app_lower = app.lower()
            fuzzy_fragments.append((app_lower.replace(" ", "-"), app))
            if " " in app_lower:
                fuzzy_fragments.append((app_lower, app))

        self._comm_to_app = comm_to_app
        self._filename_exe_to_app = filename_exe_to_app
        self._fuzzy_fragments = fuzzy_fragments
        # Any string whose presence in comm or filename justifies a full match check
        self._quick_filter = frozenset(
            set(comm_to_app.keys()) |
            set(filename_exe_to_app.keys()) |
            {f for f, _ in fuzzy_fragments}
        )

    def trace_print(self) -> None:
        self.bpf.trace_print()

    def get_main_process(self, comm: str, filename: str) -> tuple[bool, str]:
        """Check whether this execve event is the main process of a monitored app.

        Uses pre-built lookup tables (_comm_to_app, _filename_exe_to_app,
        _fuzzy_fragments) so no config access or dict construction occurs on the
        hot path.
        """
        comm_lower = comm.lower()
        filename_lower = filename.lower()

        # O(1) comm exact match – covers process-title bpf_name entries
        # (e.g. comm="mybench" while filename="python")
        if comm_lower in self._comm_to_app:
            return True, self._comm_to_app[comm_lower]

        # All remaining checks require the executable to live under a known
        # /bin/ path or to be launched via bash, to avoid false positives on
        # interpreter argv[0].
        is_bin_path = any(x in filename_lower for x in ('/bin/', '/usr/bin/', '/snap/bin/'))
        is_bash_launch = (comm_lower == 'bash')
        if not is_bin_path and not is_bash_launch:
            return False, ""

        # Exact filename-path match against bpf_name entries
        # (e.g. /usr/bin/llama-server with bpf_name=["llama-server"])
        for exe, app in self._filename_exe_to_app.items():
            if f"/{exe}" in filename_lower or filename_lower.endswith(f"/{exe}"):
                return True, app

        # Fuzzy app-name match (app name appears as a substring of the path)
        for fragment, app in self._fuzzy_fragments:
            if fragment in filename_lower:
                return True, app

        return False, ""

    def is_process_alive(self, pid):
        try:
            # 检查 /proc/[pid]/status 是否存在
            with open(f"/proc/{pid}/status") as f:
                return True
        except FileNotFoundError:
            return False


    def handle_exit_event(self, pid, app_id, app_name, old_comm, old_filename):
        """延迟检查进程是否真正退出"""
        if self.is_process_alive(pid):
            logger.debug(f"[Delay Check] PID={pid} still alive, not exiting normally.")
            return

        logger.debug(f"Monitored process terminated: PID={pid}, app={app_name}")
        app_utils.callback_manager.send_callback_notification({
            'app_id': app_id,
            'app_name': app_name,
            'status': "stopped",
            'purpose': "app"
        }, True)
        del self.monitored_app_launched[pid]

        # 清理 pending_exit_events
        if pid in self.pending_exit_events:
            del self.pending_exit_events[pid]


    def print_event(self, cpu: int, data: Any, size: int) -> None:
        event = self.bpf["events"].event(data)
        filename = event.filename.decode('utf-8', 'ignore')
        comm = event.comm.decode('utf-8', 'ignore')
        pid = event.pid
        type = event.type

        # logger.debug(f"*** Event: PID={pid}, type={type} COMM={comm}, FILENAME={filename} ***")

        if type == 0: # 启动事件
            comm_lower = comm.lower()
            filename_lower = filename.lower()
            # Fast pre-filter: skip the vast majority of unrelated BPF exec events
            # without entering get_main_process at all.
            if not (comm_lower in self._comm_to_app or
                    any(c in filename_lower or c in comm_lower
                        for c in self._quick_filter)):
                return

            is_main_process, app_name = self.get_main_process(comm, filename)
            # logger.debug(f"Is this filename main process? {is_main_process}, app_name={app_name}")
            if is_main_process:
                logger.debug(f"Is this filename main process? {is_main_process}, app_name={app_name}")
                # 防止重复处理同一个进程树
                if not self.is_process_handled(pid):
                    app_data = self._app_map_index.get(app_name.lower())
                    app_id, app_priority = app_data['app_id'], app_data.get('priority', 'low') if app_data else ("", "low")
                    logger.debug(f"launch: app_id={app_id}, app_name={app_name}, comm={comm}, filename={filename}")
                    self.monitored_app_launched[pid] = (app_id, app_name, comm, filename)
                    if app_priority.lower() == "critical":
                        app_utils.adjust_oom_priority(app_id, app_name, app_priority, app_data['cmdline'])
                        app_utils.callback_manager.send_callback_notification({
                            'app_id': app_id,
                            'app_name': app_name,
                            'status': "running",
                            'purpose': "app"
                        }, True)
                    else:
                        # Only intercept (SIGSTOP) when the system is already in
                        # critical pressure state.  If the system is idle the app
                        # is allowed to start freely, avoiding the collateral
                        # "Stopped" visible to the user that the previous
                        # always-SIGSTOP pattern caused.
                        if self._system_critical.is_set():
                            try:
                                os.kill(pid, signal.SIGSTOP)
                            except OSError as e:
                                logger.debug(f"SIGSTOP failed for PID {pid}: {e}")
                            self.handle_monitored_app(pid, comm, filename, app_name, app_id)
                        else:
                            # System is not under critical pressure: let the app run.
                            logger.debug("System not critical, allowing '%s' (PID: %s) to run freely", app_name, pid)
                            app_utils.callback_manager.send_callback_notification({
                                'app_id': app_id,
                                'app_name': app_name,
                                'status': "running",
                                'purpose': "app"
                            }, True)
                    self.mark_process_handled(pid)

        elif type == 1:  # 退出事件
            if pid not in self.monitored_app_launched:
                return

            # 如果已经有待处理的退出事件，取消旧的定时器
            if pid in self.pending_exit_events:
                self.pending_exit_events[pid].cancel()

            app_id, app_name, old_comm, old_filename = self.monitored_app_launched[pid]
            # logger.debug(f"Detected possible exit: PID={pid}, comm={comm}")

            # 延迟 1.5 秒后检查进程是否真正退出
            timer = Timer(1.5, self.handle_exit_event, args=[pid, app_id, app_name, old_comm, old_filename])
            self.pending_exit_events[pid] = timer
            timer.start()


    def is_process_handled(self, pid: int) -> bool:
        """检查该进程是否已经被处理过"""
        # 检查当前进程及其父进程是否已被处理
        try:
            process = psutil.Process(pid)
            for p in [process] + process.parents():
                if p.pid in self.handled_processes:
                    return True
        except psutil.NoSuchProcess:
            pass
        return False

    def mark_process_handled(self, pid: int) -> None:
        """标记进程为已处理"""
        self.handled_processes.add(pid)

    def handle_monitored_app(self, pid: int, comm: str, filename: str, app_name: str, app_id: str) -> None:
        """Handle a low-priority app that was launched while the system is under critical pressure.

        This method is only called from print_event when _system_critical is set,
        meaning SIGSTOP has already been issued.  It re-checks the event in case
        the system recovered between the SIGSTOP and this point; if so, it issues
        SIGCONT and lets the app run.  Otherwise it queues the app for deferred
        resumption.
        """
        logger.debug(f"Detected monitored app '{app_name}' (PID: {pid}, COMM: {comm}, FILE: {filename}, app_id: {app_id})")

        try:
            # Re-check the critical flag: the monitor may have transitioned out
            # of critical between the SIGSTOP in print_event and here.
            if not self._system_critical.is_set():
                logger.debug(f"System recovered before handling {app_name} (PID: {pid}), resuming")
                os.kill(pid, signal.SIGCONT)
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "running",
                    'purpose': "app"
                }, True)
            else:
                logger.info(f"System resources busy, skipping relaunch of {app_name}")
                app_utils.safe_notify("System resources busy", f"已暂停应用{app_name}启动", icon='dialog-warning')
                app_utils.callback_manager.send_callback_notification({
                    'app_id': app_id,
                    'app_name': app_name,
                    'status': "pending",
                    'purpose': "app"
                }, True)
                app_utils.update_app_status(app_id, "pending")
                self.app_pending_queue.put(
                    {"pid": pid, "comm": comm, "filename": filename, "app_name": app_name, "app_id": app_id})

        except Exception as e:
            logger.debug(f"Error handling {app_name} (PID: {pid}): {str(e)}")

    def add_to_monitorlist(self, app_names: Union[str, List[str]]) -> None:
        """添加应用到监控列表（支持批量操作）"""
        if not app_names:
            return

        # 统一转为列表处理
        names = [app_names] if isinstance(app_names, str) else app_names

        # 转换为小写用于比较
        existing_lower = {name.lower() for name in self.monitored_apps}

        added_count = 0
        for name in names:
            if not name or not name.strip():
                logger.debug(f"Skipping empty app name in monitor list")
                continue
            if name.lower() not in existing_lower:
                self.monitored_apps.add(name)
                existing_lower.add(name.lower())  # 更新检查集
                added_count += 1
                logger.debug(f"Added '{name}' to monitoring list")

        if added_count == 0 and names:
            app_str = ', '.join(f"'{name}'" for name in names)
            logger.debug(f"All {len(names)} app(s) [{app_str}] already in monitoring list")
        elif added_count > 0:
            logger.debug(f"Successfully added {added_count}/{len(names)} new app(s)")
            self._rebuild_match_cache()

    def remove_from_monitorlist(self, app_name: str) -> None:
        """从监控列表中移除应用"""
        if app_name in self.monitored_apps:
            self.monitored_apps.remove(app_name)
            logger.debug(f"Removed '{app_name}' from monitoring list")
            self._rebuild_match_cache()
        else:
            logger.debug(f"'{app_name}' not found in monitoring list")

    def clear_monitorlist(self) -> None:
        """清空监控列表"""
        self.monitored_apps.clear()
        logger.debug("Cleared monitoring list")
        self._rebuild_match_cache()

    def get_monitored_apps(self) -> List[str]:
        """获取当前监控的应用列表"""
        return list(self.monitored_apps)

    def check_system_resources(self, cpu_threshold: int = 70, mem_threshold: int = 80) -> bool:
        """检查系统资源使用情况"""
        try:
            # 获取CPU使用率
            cpu_percent = psutil.cpu_percent(interval=1)

            # 获取内存使用率
            mem_percent = psutil.virtual_memory().percent

            logger.debug(f"System status - CPU: {cpu_percent}%, Memory: {mem_percent}%")

            # 检查是否低于阈值
            return cpu_percent < cpu_threshold and mem_percent < mem_threshold

        except Exception as e:
            logger.debug(f"Error checking system resources: {str(e)}")
            # 出现错误时默认允许启动
            return True


if __name__ == "__main__":
    # 初始化BPF
    bpf_monitor = AppIntercept()

    # 添加应用到监控列表
    bpf_monitor.add_to_monitorlist("firefox")
    bpf_monitor.add_to_monitorlist("Calculator")

    # 打开性能缓冲区
    bpf_monitor.bpf["events"].open_perf_buffer(bpf_monitor.print_event)
    logger.debug(f"Monitoring execve() for: {', '.join(bpf_monitor.get_monitored_apps())}")
    logger.debug("Ctrl+C to exit")

    while True:
        try:
            # 同时处理trace打印和事件
            bpf_monitor.bpf.perf_buffer_poll(timeout=100)
        except KeyboardInterrupt:
            logger.debug("\nExiting...")
            break
        except Exception as e:
            logger.debug(f"Error: {e}")
            break
