# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import queue as _queue
import re
import subprocess
import time
import psutil
import threading
from getpass import getuser
from pwd import getpwnam
from datetime import datetime

from utils.logger import logger
from db.DatabaseModel import AIAppPriority
from typing import List, Dict, Any
from config.config import b_config
from gi.repository import Gio

_original_oom_scores: dict[str, str] = {}

class ClientCallbackManager:
    """管理客户端回调的全局状态和操作"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            # Initialize SSE state once inside __new__ to avoid races
            instance._sse_queues: List[_queue.Queue] = []
            instance._sse_lock = threading.Lock()
            cls._instance = instance
        return cls._instance

    def add_sse_client(self, q: _queue.Queue) -> None:
        """Register an SSE client queue."""
        with self._sse_lock:
            self._sse_queues.append(q)

    def remove_sse_client(self, q: _queue.Queue) -> None:
        """Unregister an SSE client queue."""
        with self._sse_lock:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

    def send_callback_notification(self, data: Dict[str, Any], store=False) -> bool:
        """发送回调通知（线程安全）"""
        if store:
            try:
                result = AIAppPriority.update_record(
                    id=data['app_id'],
                    status=data['status'],
                    up_time=datetime.now()
                )
                if not result:
                    print(f"Warning: Failed to update database record for {data['app_id']}")
            except Exception as db_error:
                print(f"Database update error: {db_error}")

        with self._sse_lock:
            for q in list(self._sse_queues):
                try:
                    q.put_nowait(data)
                except Exception:
                    pass

        return True


# 单例实例
callback_manager = ClientCallbackManager()


def get_cgroup_path_by_pid(pid):
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 3:
                    # cgroup v2: 0::<path>
                    return parts[2]
    except Exception:
        pass
    return None


def get_controlled_apps_config(apps_dict=None):
    if apps_dict is None:
        apps_dict = {}
    # 配置文件 controlled_apps，补充数据库没有的项
    if hasattr(b_config, 'testing_network_app') and b_config.testing_network_app:
        for app in b_config.testing_network_app:
            app_name = app.get("app_name")
            app_id = app.get("app_cgroup")
            priority = app.get("priority")
            try:
                for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                    if app_name and app_name.lower() in proc.name().lower():
                        cg_path = get_cgroup_path_by_pid(proc.pid)
                        if cg_path and app_id in cg_path:
                            if app_id not in apps_dict:
                                apps_dict[app_id] = {
                                    "app_name": app_name,
                                    "app_id": app_id,
                                    "priority": priority,
                                    "pid": proc.pid,
                                    "cgroup_path": cg_path,
                                }
                            break
            except Exception as e:
                logger.error(f"Error processing app {app_name}: {str(e)}", exc_info=True)
                continue


def get_app_priority(app_id: str = "", app_name: str = "") -> str:
    """Get the priority of an application."""
    try:
        # 构建 OR 查询条件
        query = AIAppPriority.query()
        conditions = []
        if app_id:
            conditions.append(AIAppPriority.app_id == app_id)
        if app_name:
            conditions.append(AIAppPriority.name == app_name)

        if not conditions:
            return "low"

        query = query.where(conditions[0])
        record = query.first()

        if record:
            return record.priority or "low"
        else:
            return "low"

    except Exception as e:
        logger.error("Failed to get app priority from db: %s", str(e))
        return "low"


def get_priority_value(priority_str: str = "") -> int:
    """
    :param priority_str: e.g. critical
    :return: 100
    """
    priority = priority_str.lower()
    print(f"Getting priority for: {priority}, is: {b_config.app_priority}")
    if priority not in b_config.app_priority:
        raise ValueError(f"Invalid priority: {priority_str}")
    return b_config.app_priority[priority]


def get_controlled_apps_net():
    """ Get the list of all controlled apps with their network-related info (cgroup path, pid, etc.) """
    apps_dict = {}
    # 1. 先查数据库 controlled_apps，优先使用数据库
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        for app in controlled_apps:
            app_name = getattr(app, "name", None)
            app_id = getattr(app, "app_id", None)
            priority = getattr(app, "priority", None)
            cmdline = getattr(app, "cmdline", None)
            pid = None
            cgroup_path = None
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                if app_name and app_name.lower() in proc.name().lower():
                    cg_path = get_cgroup_path_by_pid(proc.pid)
                    if cg_path and app_id in cg_path:
                        apps_dict[app_id] = {
                            "app_name": app_name,
                            "app_id": app_id,
                            "priority": priority,
                            "pid": proc.pid,
                            "cgroup_path": cg_path,
                        }
                        break
    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)

    get_controlled_apps_config(apps_dict)
    # 3. 返回合并后的列表
    return list(apps_dict.values()) if apps_dict else None


def get_controlled_apps(priority: str = None):
    """ Get the list of all controlled apps with basic info (without dynamic data like pid/cgroup) """
    try:
        controlled_apps = AIAppPriority.query().filter(AIAppPriority.controlled == True)
        if priority is not None:
            controlled_apps = controlled_apps.filter(AIAppPriority.priority == priority)
        return [{
            "app_name": app.name,
            "app_id": app.app_id,
            "controlled": app.controlled,
            "priority": app.priority,
            "cmdline": app.cmdline,
        } for app in controlled_apps] if controlled_apps else None

    except Exception as e:
        logger.error(f"Database query failed: {str(e)}", exc_info=True)
        return None


def get_app_control_info(app_id: str = None, app_name: str = None):
    """ 获取应用的管控状态和管控数据 """
    controlled_apps = get_controlled_apps() or []
    controlled_map = {app['app_id']: app for app in controlled_apps if app.get('app_id')}
    name_map = {app['app_name'].lower(): app for app in controlled_apps if app.get('app_name')}

    is_controlled = app_id in controlled_map or app_name in name_map
    controlled_data = None
    if is_controlled:
        controlled_data = controlled_map.get(app_id) or name_map.get(app_name)

    return is_controlled, controlled_data


def get_app_processes(app_name):
    """通过pgrep获取应用的所有运行中PID
    :return:
        list[int]: 如[1234, 5678]
    """
    try:
        result = subprocess.run(
            ['pgrep', '-f', app_name.lower()],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            return [int(pid) for pid in result.stdout.splitlines() if pid.strip()]
    except Exception as e:
        logger.warning(f"pgrep failed for {app_name}: {str(e)}")
    return []


def check_pids_disk_io_usage(running_pids: List[int], threshold_mb: float = 100.0) -> tuple[bool, str]:
    """
        批量检查多PID磁盘IO是否超过阈值，仅返回是否繁忙和异常信息
    :param running_pids: 某个app对应的PIDs
    :param threshold_mb: 磁盘IO阈值，单位MB/s
    :return:
        tuple(bool, str): (是否繁忙? 异常信息)
    """
    try:
        sample_times, sample_interval = 3, 0.2

        iotop_cmd = ["sudo", "iotop", "-b", "-o", "-k", "-n", str(sample_times), "-d", str(sample_interval)]
        for pid in running_pids:
            iotop_cmd.extend(["-p", str(pid)])

        result = subprocess.run(
            iotop_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        # 命令执行异常处理
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            if "no such file or directory" in error_msg.lower():
                raise Exception("未安装iotop，请先安装")
            elif "permission denied" in error_msg.lower():
                raise Exception("缺少sudo权限")
            else:
                raise Exception(f"iotop执行失败：{error_msg}")

        # 解析输出
        io_pattern = re.compile(r"(?P<pid>\d+)\s+.+?(?P<read_kb>\d+\.\d+)\s+K/s\s+(?P<write_kb>\d+\.\d+)\s+K/s")
        pid_io_data = {pid: {"read": [], "write": []} for pid in running_pids}

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line or "PID" in line or "DISK READ" in line:
                continue
            match = io_pattern.search(line)
            if match:
                pid = int(match.group("pid"))
                if pid in pid_io_data:
                    pid_io_data[pid]["read"].append(float(match.group("read_kb")))
                    pid_io_data[pid]["write"].append(float(match.group("write_kb")))

        # 计算总IO速率
        total_io_mb = 0.0
        for io_data in pid_io_data.values():
            avg_read = sum(io_data["read"]) / len(io_data["read"]) if io_data["read"] else 0.0
            avg_write = sum(io_data["write"]) / len(io_data["write"]) if io_data["write"] else 0.0
            total_io_mb += (avg_read + avg_write) / 1024.0

        logger.debug(f"Total Disk IO for PIDs {running_pids}: {total_io_mb:.2f} MB/s (Threshold: {threshold_mb} MB/s)")
        # 返回结果：无异常msg为空字符串
        return total_io_mb > threshold_mb, ""
    except Exception as e:
        logger.error(f"Disk IO check failed: {str(e)}", exc_info=True)
        return False, str(e)


def get_pids_in_cgroup(cgroup_path):
    """获取指定cgroup下的所有进程PID"""
    try:
        result = subprocess.run(
            ["systemd-cgls", "--no-page", cgroup_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout

        if not output:
            logger.debug(f"No output from systemd-cgls for cgroup {cgroup_path}")
            return []

        pids = re.findall(r"[├└]─(\d+)\s+.+", output)
        filtered_pids = []
        for pid in map(int, pids):
            try:
                cmdline = psutil.Process(pid).cmdline()
                if cmdline and cmdline[0] == "bash":
                    continue  # Skip processes with cmdline "bash"
                filtered_pids.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return filtered_pids

    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout while getting PIDs for cgroup {cgroup_path}")
        return []
    except Exception as e:
        logger.error(f"Error getting PIDs for cgroup {cgroup_path}: {str(e)}")
        return []


def _get_executable_name(app_name, app_cmdline):
    if not app_cmdline:
        return app_name.lower()

    # 1. Handle Snap apps (e.g., "/snap/bin/firefox %u")
    if "/snap/bin/" in app_cmdline:
        for part in app_cmdline.split():
            if "/snap/bin/" in part:
                return os.path.basename(part)  # "firefox"

    # 2. Handle Flatpak apps (e.g., "flatpak run --command=missioncenter ...")
    if "flatpak run" in app_cmdline:
        match = re.search(r"--command=([^\s]+)", app_cmdline)
        if match:
            return match.group(1).lower()  # "missioncenter"
        last_part = app_cmdline.split()[-1]
        if "." in last_part:
            return last_part.split(".")[-1].lower()

    # 3. Generic cases (e.g., "/usr/bin/foo")
    for part in app_cmdline.split():
        # Skip flags, env vars, and placeholders
        if part.startswith(("-", "%", "env")):
            continue

        if "/" in part:
            return os.path.basename(part)
        # If no path (e.g., "firefox"), use as-is
        return part.lower()

    return app_name.lower()


def adjust_oom_priority(
    app_id: str,
    app_name: str,
    priority: str,
    app_cmdline: str,
    restore: bool = False,
) -> None:
    """
    调整或恢复应用的 OOM 优先级（oom_score_adj）, 主要目的是保活一些特殊的critical的应用
    :param app_id:
    :param app_name:
    :param priority: 仅当为 "critical" 时生效
    :param app_cmdline: 用于 pgrep 匹配
    :param restore: 若为 True，则恢复原始值；否则根据 priority 设置
    :return:
    """
    if not restore and priority.lower() != "critical":
        return  # 非 critical 应用且不强制恢复时跳过

    target_value = 0
    try:
        exe_name = _get_executable_name(app_name, app_cmdline)
        logger.debug(f"Target executable: {exe_name}")

        pgrep_result = subprocess.run(
            ["pgrep", "-f", exe_name],
            capture_output=True,
            text=True,
        )
        if pgrep_result.returncode != 0:
            logger.debug(f"App {app_name} is not running and no OOM adjustment needed.")
            return

        pids = [pid for pid in pgrep_result.stdout.strip().split("\n") if pid]
        for pid in pids:
            oom_file = f"/proc/{pid}/oom_score_adj"

            if restore:
                if pid not in _original_oom_scores:
                    logger.warning(f"No original OOM score recorded for PID {pid}. Skipping.")
                    continue
                target_value = _original_oom_scores.pop(pid)
                action = "Restoring"
            else:
                # 记录app的默认值
                if pid not in _original_oom_scores:
                    with open(oom_file, "r") as f:
                        _original_oom_scores[pid] = f.read().strip()
                target_value = "-1000"
                action = "Setting"

            # 修改 oom_score_adj
            logger.debug(f"{action} OOM priority for PID {pid} to {target_value}")
            base_cmd = ["tee", oom_file]
            cmd = ["sudo", *base_cmd] if getattr(b_config, "vendor", "") == "generic" else base_cmd
            subprocess.run(
                cmd,
                input=target_value,
                text=True,
                check=True,
            )

        _update_app_oom_score_adj(app_id, int(target_value))
        logger.info(f"OOM priority updated for {app_name} (PID(s): {', '.join(pids)})")

    except Exception as e:
        logger.error(f"Failed to adjust OOM priority for {app_name}: {e}")


def _update_app_oom_score_adj(app_id: str, score: int) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id,
            oom_score=score
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"oom_score_adj updated - ID: {app_id}, New score: {score}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def update_app_status(app_id: str, status: str) -> bool:
    try:
        result = AIAppPriority.update_record(
            id=app_id,
            status=status
        )
        if not result:
            logger.warning(f"No record updated for app_id: {app_id}")
            return False

        logger.info(f"Status updated - ID: {app_id}, New status: {status}")
        return True

    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False


def get_app_resource_usage(app_id: str, app_name: str) -> dict:
    """Query the actual CPU, memory, and IO usage of a specific application via cgroup."""
    try:
        base_cgroup = "/sys/fs/cgroup"
        if hasattr(b_config, 'cgroup_mount') and b_config.cgroup_mount:
            base_cgroup = b_config.cgroup_mount

        # Find a representative PID to locate the cgroup.
        # Try app_name first; if that yields nothing, fall back to app_id (e.g. "benchmark.py")
        # so that processes whose argv[0] was renamed (e.g. via perl $0=) are still found.
        pids = get_app_processes(app_name)
        if not pids and app_id:
            pids = get_app_processes(os.path.basename(app_id))
        if not pids:
            print(f"No processes found for app {app_name} (ID: {app_id})")
            return {}

        # Locate the cgroup from the first PID
        cgroup_path = get_cgroup_path_by_pid(pids[0])
        if not cgroup_path:
            print(f"No cgroup found for PID {pids[0]} of app {app_name}")
            return {}

        cgroup_dir = os.path.join(base_cgroup, cgroup_path.lstrip('/'))
        num_cpus = os.cpu_count() or 1

        # --- Instantaneous memory from cgroup memory.current ---
        try:
            with open(os.path.join(cgroup_dir, "memory.current"), 'r') as f:
                cgroup_mem_bytes = int(f.read().strip())
        except (FileNotFoundError, IOError, ValueError):
            cgroup_mem_bytes = 0

        # --- Helpers to sample cumulative cgroup counters ---
        def read_cpu_usage_usec():
            try:
                with open(os.path.join(cgroup_dir, "cpu.stat"), 'r') as f:
                    for line in f:
                        if line.startswith('usage_usec'):
                            return int(line.split()[1])
            except (FileNotFoundError, IOError, ValueError):
                pass
            return 0

        def read_io_bytes():
            rbytes, wbytes = 0, 0
            try:
                with open(os.path.join(cgroup_dir, "io.stat"), 'r') as f:
                    for line in f:
                        parts = dict(p.split('=') for p in line.split() if '=' in p)
                        rbytes += int(parts.get('rbytes', 0))
                        wbytes += int(parts.get('wbytes', 0))
            except (FileNotFoundError, IOError, ValueError):
                pass
            return rbytes, wbytes

        # Sample CPU and IO over a short window so we get accurate rates
        t1 = time.monotonic()
        cpu_usec1 = read_cpu_usage_usec()
        io_rbytes1, io_wbytes1 = read_io_bytes()
        time.sleep(0.5)
        t2 = time.monotonic()
        cpu_usec2 = read_cpu_usage_usec()
        io_rbytes2, io_wbytes2 = read_io_bytes()

        elapsed = t2 - t1
        elapsed_usec = elapsed * 1_000_000

        cpu_percent = (
            round(max(0.0, cpu_usec2 - cpu_usec1) / (elapsed_usec * num_cpus) * 100, 1)
            if elapsed_usec > 0 else 0.0
        )
        io_read_mb_s = round(max(0.0, (io_rbytes2 - io_rbytes1) / elapsed / (1024 ** 2)), 2) if elapsed > 0 else 0.0
        io_write_mb_s = round(max(0.0, (io_wbytes2 - io_wbytes1) / elapsed / (1024 ** 2)), 2) if elapsed > 0 else 0.0
        mem_current_mb = round(cgroup_mem_bytes / (1024 ** 2), 2)

        all_pids = get_pids_in_cgroup(cgroup_path)
        logger.debug(
            f"Resource usage for {app_name} (ID: {app_id}): CPU={cpu_percent:.1f}%, "
            f"Memory_current={mem_current_mb:.2f}MB, IO Read={io_read_mb_s:.2f}MB/s, IO Write={io_write_mb_s:.2f}MB/s"
        )
        return {
            'pids': list(all_pids),
            'name': app_name,
            'cgroup_path': cgroup_path,
            'cpu_percent': cpu_percent,
            'mem_current': mem_current_mb,
            'io_read_mb': io_read_mb_s,
            'io_write_mb': io_write_mb_s,
        }
    except Exception as e:
        print(f"Error getting resource usage for {app_name} (ID: {app_id}): {e}")
        return {}


def safe_notify(title, message, icon="dialog-information"):
    try:
        # 方法1：优先尝试原生notify-send
        user = os.getenv("SUDO_USER") or getuser()

        user_uid = getpwnam(user).pw_uid

        # 构建正确的DBus地址
        dbus_address = f'unix:path=/run/user/{user_uid}/bus'

        # 使用sudo -u切换用户身份执行
        subprocess.run([
            'sudo', '-u', user,
            f'DBUS_SESSION_BUS_ADDRESS={dbus_address}',
            'DISPLAY=:0',
            'notify-send',
            f'--icon={icon}',
            title,
            message
        ], check=True)

    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            # 方法2：使用zenity作为后备方案
            subprocess.run(
                ["zenity", "--info", "--text", f"{title}\n{message}", "--title", "系统通知"],
                check=True
            )
        except:
            print(f"\a⚠️ {title}: {message}")


def get_dbus_address():
    """动态获取当前用户的DBus地址"""
    uid = os.getuid()

    # 方法1：检查标准路径
    standard_path = f"/run/user/{uid}/bus"
    if os.path.exists(standard_path):
        return f"unix:path={standard_path}"

    # 方法2：从进程环境获取
    try:
        import psutil
        for proc in psutil.process_iter(['environ']):
            try:
                env = proc.environ()
                if 'DBUS_SESSION_BUS_ADDRESS' in env:
                    return env['DBUS_SESSION_BUS_ADDRESS']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass

    # 方法3：通过loginctl获取
    try:
        cmd = ["loginctl", "show-user", str(uid), "--property=Display"]
        display = subprocess.check_output(cmd).decode().strip()
        if display:
            return f"unix:path=/run/user/{uid}/bus"
    except:
        pass

    return None


def fetch_all_apps():
    app_list = []
    if hasattr(b_config, 'all_apps'):
        apps = b_config.all_apps
        for app in apps:
            app_data = {
                "name": app["name"],       # legacy key used by other callers
                "app_name": app["name"],   # normalized key expected by the React dashboard
                "app_id": app["id"],
                "cmdline": app["commandline"],
                "display_name": app["name"]
            }
            app_list.append(app_data)
    else:
        apps = Gio.AppInfo.get_all()
        for app in apps:
            app_data = {
                "name": app.get_name(),    # legacy key used by other callers
                "app_name": app.get_name(),  # normalized key expected by the React dashboard
                "app_id": app.get_id(),  # org.gnome.Calculator.desktop
                "cmdline": app.get_commandline() or "",  # gnome-calculator
                "display_name": app.get_display_name()
            }
            app_list.append(app_data)
    return app_list
