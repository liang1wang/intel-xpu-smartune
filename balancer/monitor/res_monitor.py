# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import os
import re
# [SECURITY REVIEW]: All subprocess calls in this module use list-based arguments 
# with shell=False (default). No untrusted shell execution or string 
# concatenation is performed. All inputs are internally validated.
import subprocess # nosec
import time
from collections import defaultdict
from time import sleep

import psutil
from config.config import b_config
from utils.app_utils import fetch_all_apps, get_cgroup_path_by_pid, get_pids_in_cgroup
from utils.logger import logger

from monitor import PSIMonitor


class ResourceMonitor:
    def __init__(self):
        """初始化资源监视器"""
        self.config = b_config
        self.cpu_cores = os.cpu_count() or 16
        self.prev_io = psutil.disk_io_counters(perdisk=True)
        self.prev_time = time.time()
        # 桌面应用信息
        try:
            self.desktop_apps = {app["app_id"]: app for app in fetch_all_apps()}
            logger.info(f"Loaded {len(self.desktop_apps)} desktop applications")
        except Exception as e:
            logger.warning(f"Could not load desktop apps: {str(e)}")
            self.desktop_apps = {}

    def _get_top_processes(self, n=1, samples=3, interval=1.0, mode='default'):
        """获取资源占用最高的应用（基于 cgroup 聚合）
        :param n: 返回前 n 个进程
        :param samples: 对top process进行采样的次数
        :param interval: 采样间隔时间（秒）
        :param mode: 采样模式，默认default - 按 CPU/内存综合评分; 'io' - 仅按 IO 读写量排序
        """
        # Step 1: 采样获取候选进程（考虑权重和进程组）
        psi_data = PSIMonitor().get_current_pressure()
        dynamic_weights = self._adjust_weights_by_pressure(psi_data)

        candidate_procs = self._get_candidate_processes(
            num=max(n * 3, 9),  # 候选进程数，确保有足够候选以覆盖至少 n 个不同cgroup
            samples=samples,
            interval=interval,
            dynamic_weights=dynamic_weights
        )

        # logger.debug(f"Candidate processes for cgroup aggregation: {candidate_procs}")
        # Step 2: 收集所有不重复的cgroup_path
        cgroup_paths = set()
        for proc in candidate_procs:
            cgroup_path = get_cgroup_path_by_pid(proc['pid'])
            if cgroup_path:
                cgroup_paths.add(cgroup_path)

        # Step 3: 按 cgroup 聚合进程
        cgroup_data = defaultdict(lambda: {
            'cpu_total': 0,  # 所有进程 CPU 使用率总和（%）
            'mem_percent_total': 0,  # 所有进程内存占用百分比总和（%）
            'mem_rss_total': 0,  # 所有进程 RSS 内存总和（字节）
            'io_read_total': 0,    # 所有进程 IO 读取字节 delta 总和
            'io_write_total': 0,   # 所有进程 IO 写入字节 delta 总和
            'io_read_count_total': 0,   # 所有进程 IO 读取次数 delta 总和 (for IOPS)
            'io_write_count_total': 0,  # 所有进程 IO 写入次数 delta 总和 (for IOPS)
            'count': 0,
            'pids': set(),
            'names': set(),
            'cmdlines': set(),
            # Dominant process: the single process contributing the most to the mode's metric.
            # In default mode this is the process with the highest CPU%; in io mode it is the
            # process with the highest IO delta.  We track this so the UI shows "stress" instead
            # of an unrelated process like "vte-2.91" that happens to share the same cgroup.
            'dominant_name': '',
            'dominant_cmdline': '',
            'dominant_metric': 0.0,  # highest individual contribution seen so far
        })

        # 缓存每个 cgroup 的 pid 列表和 Process 对象
        cgroup_pids = {}
        pid_process_map = {}
        # IO rate is computed as a delta between two snapshots taken io_sample_interval apart.
        # Using cumulative io_counters directly would give total-lifetime-bytes / elapsed which
        # produces huge, incorrect values (e.g. hundreds of MB/s for an idle Firefox).
        io_sample_interval = 0.5  # seconds between the two IO counter snapshots
        # Each entry: (read_bytes, write_bytes, read_count, write_count) at t0
        pid_io_start: dict[int, tuple[int, int, int, int]] = {}

        # 第一次遍历：初始化 CPU 计时器 + 记录初始 IO 计数（t0）
        for cgroup_path in cgroup_paths:
            pids_in_cgroup = get_pids_in_cgroup(cgroup_path)
            cgroup_pids[cgroup_path] = pids_in_cgroup
            for pid in pids_in_cgroup:
                try:
                    p = psutil.Process(pid)
                    if mode == 'default':
                        p.cpu_percent(interval=None)  # 仅default模式需要初始化CPU计时器
                    try:
                        io = p.io_counters()
                        pid_io_start[pid] = (io.read_bytes, io.write_bytes,
                                             io.read_count, io.write_count)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                        pid_io_start[pid] = (0, 0, 0, 0)
                    pid_process_map[pid] = p
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        # Sleep covers both CPU and IO measurement intervals
        t0 = time.time()
        time.sleep(io_sample_interval)
        elapsed = time.time() - t0

        # 第二次遍历：读取最终计数，计算 delta
        for cgroup_path, pids_in_cgroup in cgroup_pids.items():
            for pid in pids_in_cgroup:
                if pid not in pid_process_map:
                    continue
                p = pid_process_map[pid]
                try:
                    with p.oneshot():
                        cpu_percent = p.cpu_percent(interval=None) if mode == 'default' else 0
                        mem_percent = p.memory_percent() if mode == 'default' else 0
                        mem_info = p.memory_info()
                        try:
                            io_end = p.io_counters()
                            io_start = pid_io_start.get(pid, (0, 0, 0, 0))
                            io_read_delta = max(0, io_end.read_bytes - io_start[0])
                            io_write_delta = max(0, io_end.write_bytes - io_start[1])
                            io_read_count_delta = max(0, io_end.read_count - io_start[2])
                            io_write_count_delta = max(0, io_end.write_count - io_start[3])
                        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                            io_read_delta = io_write_delta = 0
                            io_read_count_delta = io_write_count_delta = 0
                        try:
                            proc_name = p.name()
                            proc_cmdline = ' '.join(p.cmdline()) or proc_name
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            proc_name = ''
                            proc_cmdline = ''

                    cgroup_data[cgroup_path]['cpu_total'] += cpu_percent
                    cgroup_data[cgroup_path]['mem_percent_total'] += mem_percent
                    cgroup_data[cgroup_path]['mem_rss_total'] += mem_info.rss
                    cgroup_data[cgroup_path]['io_read_total'] += io_read_delta
                    cgroup_data[cgroup_path]['io_write_total'] += io_write_delta
                    cgroup_data[cgroup_path]['io_read_count_total'] += io_read_count_delta
                    cgroup_data[cgroup_path]['io_write_count_total'] += io_write_count_delta
                    cgroup_data[cgroup_path]['count'] += 1
                    cgroup_data[cgroup_path]['pids'].add(pid)
                    if proc_name:
                        cgroup_data[cgroup_path]['names'].add(proc_name)
                        cgroup_data[cgroup_path]['cmdlines'].add(proc_cmdline)

                    # Track the single process contributing most to this cgroup's metric so
                    # the UI shows a meaningful name (e.g. "stress") rather than whichever
                    # process name Python's set iteration happens to return first (e.g. "vte").
                    metric = cpu_percent if mode == 'default' else (io_read_delta + io_write_delta)
                    if metric > cgroup_data[cgroup_path]['dominant_metric'] and proc_name:
                        cgroup_data[cgroup_path]['dominant_metric'] = metric
                        cgroup_data[cgroup_path]['dominant_name'] = proc_name
                        cgroup_data[cgroup_path]['dominant_cmdline'] = proc_cmdline
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        # Step 4: 根据模式计算评分
        processes = []
        for cgroup_path, data in cgroup_data.items():
            if data['count'] > 0:
                # IO rates: delta bytes / elapsed seconds → MB/s
                io_read_rate_mb = data['io_read_total'] / elapsed / (1024 ** 2)
                io_write_rate_mb = data['io_write_total'] / elapsed / (1024 ** 2)
                # IO operations per second (IOPS)
                io_read_iops = data['io_read_count_total'] / elapsed
                io_write_iops = data['io_write_count_total'] / elapsed
                if mode == 'io':
                    # IO模式：按实时读写速率之和排序
                    score = io_read_rate_mb + io_write_rate_mb
                else:
                    # Default模式：CPU+内存
                    cpu_total_normalized = data['cpu_total'] / self.cpu_cores
                    score = (
                            dynamic_weights['cpu'] * min(cpu_total_normalized, 100) +
                            dynamic_weights['memory'] * min(data['mem_percent_total'], 100)
                    )

                # dominant_name is the process with the highest individual contribution;
                # fall back to the alphabetically-first name from the set if no dominant
                # was recorded, to ensure consistent output across calls.
                dominant_name = data['dominant_name'] or min(data['names'], default='unknown')
                dominant_cmdline = data['dominant_cmdline'] or min(data['cmdlines'], default='')

                processes.append({
                    'pids': list(data['pids']),
                    'cgroup': cgroup_path,
                    'score': round(score, 2),
                    'cpu_avg': round(data['cpu_total'] / self.cpu_cores, 1) if mode == 'default' else 0,
                    'mem_avg': round(data['mem_percent_total'], 1) if mode == 'default' else 0,
                    'mem_rss': round(data['mem_rss_total'] / (1024 ** 3), 2),
                    'io_read_rate': round(io_read_rate_mb, 4),
                    'io_write_rate': round(io_write_rate_mb, 4),
                    'io_read_iops': round(io_read_iops, 1),
                    'io_write_iops': round(io_write_iops, 1),
                    'names': list(data['names']),
                    'cmdlines': list(data['cmdlines']),
                    'dominant_name': dominant_name,
                    'dominant_cmdline': dominant_cmdline,
                })

        # logger.debug(f"Aggregated processes by cgroup: {processes}")
        # Step 5: 返回评分最高的进程信息列表
        return sorted(processes, key=lambda x: x['score'], reverse=True)[:n]

    def _get_candidate_processes(self, num, samples, interval, dynamic_weights):
        """采样获取候选进程（计算score筛选top进程，但只返回pid和name）

        使用 per-cgroup 多样性筛选：对每个采样周期内的进程按评分排序后，限制同一
        cgroup 贡献的候选数（max_per_cgroup），防止高并发应用（如 stress -c 8）的
        多个同 cgroup 工作进程占满所有候选名额，从而保证候选集覆盖足够多的不同 cgroup。
        """
        candidates = []
        seen_pids = set()  # 用于记录已经处理过的PID
        max_per_cgroup = 2  # 每个 cgroup 最多贡献的候选进程数

        for _ in range(samples):
            current_sample = []
            for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent',
                                             'create_time']):
                try:
                    info = proc.info
                    pid = info['pid']

                    # 跳过已处理过的PID或黑名单进程
                    if (pid in seen_pids or
                            any(b in info.get('name', '') for b in self.config.blacklist) or
                            time.time() - info['create_time'] < 2):
                        continue

                    seen_pids.add(pid)  # 标记为已处理

                    # 计算带权重的实时评分（但不需要返回这些数据）
                    score = (
                            dynamic_weights['cpu'] * min(info['cpu_percent'], 100) +
                            dynamic_weights['memory'] * min(info['memory_percent'], 100)
                    )

                    current_sample.append({
                        'pid': pid,
                        'name': info['name'],
                        'score': score  # 仅用于排序
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Select top-num candidates with per-cgroup diversity cap.
            # Sort by score desc then greedily pick processes, capping each cgroup at
            # max_per_cgroup slots.  This prevents one heavy multi-process app (e.g.
            # `stress -c 8`) from monopolising all candidate slots with workers that all
            # share the same cgroup, which would leave _get_top_processes with only one
            # unique cgroup and return just a single result instead of the requested n.
            current_sample_sorted = sorted(current_sample, key=lambda x: -x['score'])
            selected: list = []
            cgroup_count: dict = {}
            for proc in current_sample_sorted:
                cgroup = get_cgroup_path_by_pid(proc['pid']) or str(proc['pid'])
                cnt = cgroup_count.get(cgroup, 0)
                if cnt < max_per_cgroup:
                    selected.append(proc)
                    cgroup_count[cgroup] = cnt + 1
                    if len(selected) >= num:
                        break
            candidates.extend(selected)
            if _ != samples - 1:
                time.sleep(interval)

        return [{'pid': p['pid'], 'name': p['name']} for p in candidates]

    def _adjust_weights_by_pressure(self, psi_data):
        """根据PSI压力动态调整权重"""
        base_weights = self.config.weights_top
        return {
            'cpu': base_weights['cpu'] * (1 + psi_data.get('cpu', 0)),
            'memory': base_weights['memory'] * (1 + psi_data.get('memory', 0)),
            'io': base_weights['io']  # 保留但不再用于进程评分
        }

    def _find_systemd_unit(self, pid):
        """通过systemd-cgls查找进程所属的scope, service"""
        try:
            result = subprocess.run(
                ['systemd-cgls', '--no-page'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            # 查找包含指定PID的行及其父unit
            lines = result.stdout.split('\n')
            for i, line in enumerate(lines):
                if f'─{pid} ' in line or f'─{pid}\n' in line:
                    # 向上查找最近的unit(scope或service)
                    for j in range(i, -1, -1):
                        line_content = lines[j]
                        if '.scope' in line_content or '.service' in line_content:
                            # 匹配类似 "├─session-c20.scope" 或 "├─fileManage.service"
                            unit_match = re.search(r"─(.*?\.(?:scope|service))", line_content)
                            if unit_match:
                                return unit_match.group(1)

                            # 如果没有匹配到，尝试更宽松的匹配
                            unit_match = re.search(r"\b([\w-]+\.(?:scope|service))\b", line_content)
                            if unit_match:
                                return unit_match.group(1)
        except Exception as e:
            logger.warning(f"Failed to find systemd unit: {str(e)}")
        return None

    def _extract_readable_app_name(self, scope_name: str) -> str:
        """Convert a systemd scope/service/slice name to a human-readable app name.

        Examples:
          snap.firefox.firefox-0e025d0b.scope  -> Firefox
          gnome-remote-desktop.service         -> Gnome Remote Desktop
          org.gnome.Shell@wayland.service      -> Gnome Shell
          session-c20.scope                    -> Session C20
          app-org.gnome.Terminal.slice         -> Gnome Terminal
        """
        name = scope_name
        # Remove trailing .scope / .service / .slice
        name = re.sub(r'\.(scope|service|slice)$', '', name)
        # Strip leading app- prefix (used in app.slice children, e.g. app-org.gnome.Terminal)
        name = re.sub(r'^app-', '', name)
        # Snap apps: snap.<appname>.<appname>-<uuid> -> appname
        snap_match = re.match(r'^snap\.([^.]+)', name)
        if snap_match:
            return snap_match.group(1).replace('-', ' ').title()
        # Strip @instance suffix (e.g. @wayland, @x11)
        name = re.sub(r'@[^@]+$', '', name)
        # Reverse-domain notation (org.gnome.Shell): take last 2 meaningful parts
        if '.' in name:
            parts = [p for p in name.split('.') if p]
            name = ' '.join(parts[-2:]) if len(parts) >= 2 else parts[-1]
        return name.replace('-', ' ').replace('_', ' ').title()

    def try_match_app(self, process_info):
        """尝试匹配桌面应用或systemd scope"""
        cgroup = process_info.get('cgroup')

        # 1. Try to match a registered desktop app by process name or exe path
        if self.desktop_apps:
            exe = process_info.get('exe', '')
            # Support both aggregated format (names: set/list) and single-process format (name: str)
            names = process_info.get('names')
            if names is None:
                proc_name = process_info.get('name', '')
                names = [proc_name] if proc_name else []
            elif isinstance(names, set):
                names = list(names)

            for app_id, app in self.desktop_apps.items():
                try:
                    app_cmd = app.get("cmdline", "")
                    if exe and app_cmd and exe in app_cmd:
                        logger.debug(f"try_match_app matched desktop app by cmdline: {app_id}")
                        return {
                            'type': 'desktop',
                            'id': app_id,
                            'name': app["display_name"]
                        }

                    app_name_lower = app.get("name", "").lower()
                    for proc_name in names:
                        if app_name_lower and proc_name and app_name_lower in proc_name.lower():
                            logger.debug(f"try_match_app matched desktop app by name: {app_id}")
                            return {
                                'type': 'desktop',
                                'id': app_id,
                                'name': app["display_name"]
                            }
                except Exception as e:
                    logger.warning(f"Catch error: {e}")
                    continue

        # 2. Fallback: extract a readable name from the cgroup path
        if cgroup:
            path_parts = cgroup.rstrip('/').split('/')
            scope_name = path_parts[-1]

            # vte-spawn-*.scope is the scope created by GNOME Terminal (via VTE) for child
            # processes.  The scope itself has no useful name; look one level up to the parent
            # slice (e.g. app-org.gnome.Terminal.slice) to get the terminal app name, then
            # append the dominant process name so the user sees "Gnome Terminal - stress" rather
            # than "Vte Spawn <uuid>".
            if re.match(r'^vte-spawn-', scope_name):
                parent_slice = path_parts[-2] if len(path_parts) >= 2 else ''
                terminal_name = self._extract_readable_app_name(parent_slice) if parent_slice else 'Terminal'
                dominant_name = process_info.get('dominant_name', '')
                display_name = f"{terminal_name} - {dominant_name}" if dominant_name else terminal_name
                logger.debug(f"vte-spawn scope: parent={parent_slice!r} dominant={dominant_name!r} -> {display_name!r}")
                return {
                    'type': 'cgroup',
                    'id': scope_name,
                    'name': display_name,
                }

            logger.debug(f"Extracted scope name from cgroup: {scope_name}")
            return {
                'type': 'cgroup',
                'id': scope_name,
                'name': self._extract_readable_app_name(scope_name)
            }

        # 3. Try systemd-cgls lookup (for processes without cgroup info)
        pids = process_info.get('pids')
        if pids is None:
            pid = process_info.get('pid')
            pids = [pid] if pid else []
        elif isinstance(pids, set):
            pids = list(pids)

        if pids:
            unit = self._find_systemd_unit(pids[0])
            if unit:
                logger.debug(f"try_match_app Matched systemd unit: {unit}")
                return {
                    'type': 'systemd',
                    'id': unit,
                    'name': self._extract_readable_app_name(unit)
                }

        return None

    def get_top_resource_consumers(self):
        """获取资源占用最高的1个进程及其应用信息"""
        results = []
        reach_threshold = True
        processes = self._get_top_processes(n=1)
        logger.debug(f"Top processes: {processes}")

        # Return empty list if top process doesn't meet minimum resource thresholds
        # Since the minimum thresholds is 30% for uncontrolled apps, larger for controlled apps,
        # we use 25% here to avoid false negatives.
        if processes and (processes[0]['cpu_avg'] < 25  # if CPU usage < 25% per core
                          and processes[0]['mem_rss'] < psutil.virtual_memory().total * 0.25):  # 25% of total memory
            logger.info(f"Top process - {next(iter(processes[0]['names']), 'unknown')} corresponding "
                        f"app does not meet minimum resource thresholds")
            reach_threshold = False

        for process in processes:
            process_name = next(iter(process['names']), "unknown")
            process_cmdline = next(iter(process['cmdlines']), "unknown")

            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': next(iter(process['pids']), None),  # Use the first PID from 'pids'
                    'name': process_name,
                    'cmdline': process_cmdline,
                    'score': round(process['score'], 3),
                    'cpu_avg': process['cpu_avg'],
                    'mem_rss': process['mem_rss'],
                    'io_read_rate': process['io_read_rate']
                },
                'app': app_info
            })

        return results, reach_threshold

    def get_top_disk_io_consumers(self):
        """获取disk io占用最高的1个进程及其应用信息"""
        results = []
        processes = self._get_top_processes(n=1, mode="io")
        logger.debug(f"Top processes: {processes}")

        for process in processes:
            process_name = next(iter(process['names']), "unknown")
            process_cmdline = next(iter(process['cmdlines']), "unknown")

            app_info = self.try_match_app(process)
            results.append({
                'process': {
                    'pid': next(iter(process['pids']), None),  # Use the first PID from 'pids'
                    'name': process_name,
                    'cmdline': process_cmdline,
                    'score': round(process['score'], 3),
                    'io_read_rate': process['io_read_rate'],
                    'io_write_rate': process['io_write_rate']
                },
                'app': app_info
            })

        return results

    def get_app_resource_stats(self, n=10):
        """获取App Resources页面所需的各应用CPU/内存使用数据（不含阈值过滤）

        与 get_top_resource_consumers 不同，此方法：
          - 返回前 n 个应用（默认10个）而非仅返回top-1
          - 不检查系统压力阈值，始终返回当前资源使用情况
          - 适用于Dashboard "App Resources" 页面的数据展示
        """
        results = []
        processes = self._get_top_processes(n=n)
        logger.debug(f"App resource stats processes: {processes}")

        for process in processes:
            process_name = process.get('dominant_name') or next(iter(process['names']), 'unknown')
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), 'unknown')

            app_match = self.try_match_app(process)
            app_id = app_match['id'] if app_match else process_name
            app_name = app_match['name'] if app_match else process_name

            results.append({
                'app_id': app_id,
                'app_name': app_name,
                'pid': next(iter(process['pids']), None),
                'process_name': process_name,
                'cmdline': process_cmdline,
                'cpu_usage': round(process['cpu_avg'] / 100, 4),       # normalize to 0-1
                'memory_mb': round(process['mem_rss'] * 1024, 1),      # GB -> MB
                'io_read_rate': process['io_read_rate'],                # MB/s
                'io_write_rate': process['io_write_rate'],              # MB/s
                'score': round(process['score'], 3),
            })

        return results

    def get_app_disk_io_stats(self, n=10):
        """获取App Resources页面所需的各应用Disk I/O使用数据（不含阈值过滤）

        与 get_top_disk_io_consumers 不同，此方法：
          - 返回前 n 个应用（默认10个）而非仅返回top-1
          - 适用于Dashboard "App Resources" 页面的磁盘I/O数据展示
        """
        results = []
        processes = self._get_top_processes(n=n, mode="io")
        logger.debug(f"App disk I/O stats processes: {processes}")

        for process in processes:
            process_name = process.get('dominant_name') or next(iter(process['names']), 'unknown')
            process_cmdline = process.get('dominant_cmdline') or next(iter(process['cmdlines']), 'unknown')

            app_match = self.try_match_app(process)
            # Use resolved app name as display name; fall back to dominant process name
            app_name = app_match['name'] if app_match else process_name

            results.append({
                'pid': next(iter(process['pids']), None),
                'name': process_name,
                'app_name': app_name,
                'cmdline': process_cmdline,
                'io_read_rate': process['io_read_rate'],                # MB/s
                'io_write_rate': process['io_write_rate'],              # MB/s
                'io_read_iops': process['io_read_iops'],                # ops/s
                'io_write_iops': process['io_write_iops'],              # ops/s
                'score': round(process['score'], 3),
            })

        return results

    def get_total_memory(self):
        """获取系统物理内存总大小（单位：MB）"""
        mem = psutil.virtual_memory()
        total_memory_mb = round(mem.total / (1024 ** 2), 2)  # 转换为MB并保留2位小数
        return total_memory_mb

    def disk_utilization(self, device='nvme0n1', interval=1):
        """获取单个磁盘的利用率（%）"""
        cnt1 = psutil.disk_io_counters(perdisk=True).get(device)
        time1 = time.time()
        time.sleep(interval)
        cnt2 = psutil.disk_io_counters(perdisk=True).get(device)
        time2 = time.time()
        if not cnt1 or not cnt2:
            return 0.0
        delta_time = (time2 - time1) * 1000  # 毫秒
        busy_time = (cnt2.read_time - cnt1.read_time) + (cnt2.write_time - cnt1.write_time)
        return min(100.0, 100 * busy_time / delta_time)

    def get_physical_disks(self):
        """获取所有物理磁盘设备名"""
        cmd = ["lsblk", "-d", "-o", "NAME,TYPE", "-n"]
        try:
            output = subprocess.check_output(cmd, text=True).strip()

            disks = []
            for line in output.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "disk":
                    disks.append(parts[0])
            
            return disks
            
        except subprocess.CalledProcessError:
            return []

    def get_resource_usage(self) -> dict:
        """获取系统整体资源使用率和剩余容量"""
        # CPU：核心数、使用率（%）
        cpu_count = psutil.cpu_count(logical=True)
        cpu_usage = psutil.cpu_percent(interval=0.5)  # 0.5秒采样
        cpu_available = 100 - cpu_usage  # 剩余CPU百分比

        # 内存：总容量（GB）、使用率（%）、剩余容量占比
        mem = psutil.virtual_memory()
        mem_total_gb = round(mem.total / (1024 **3), 2)
        mem_usage = mem.percent
        mem_available_ratio = round(mem.available / mem.total, 2)  # 剩余内存占比

        return {
            'cpu': {
                'count': cpu_count,
                'usage': cpu_usage,
                'available': cpu_available,
                'is_busy': cpu_usage > self.config.cpu_busy_threshold  # 整体使用率多少算busy
            },
            'memory': {
                'total_gb': mem_total_gb,
                'usage': mem_usage,  # %
                'available_ratio': mem_available_ratio,
                'is_busy': mem_usage > self.config.memory_busy_threshold
            }
        }

    def get_disk_io_usage(self) -> dict:
        # 获取所有物理磁盘的利用率
        disks = self.get_physical_disks()
        disk_utils = {}
        for disk in disks:
            disk_utils[disk] = {
                'utilization': round(self.disk_utilization(disk), 2),
                'is_busy': False
            }

        # 判断磁盘是否繁忙（基于利用率）
        for disk in disk_utils:
            disk_utils[disk]['is_busy'] = disk_utils[disk]['utilization'] > self.config.disk_utilization_threshold

        return {'disk_io': disk_utils}

    def get_disk_io_speed(self) -> dict:
        """获取所有物理磁盘的读写速度（KB/s），基于 prev_time/prev_io 模式"""
        disks = self.get_physical_disks()
        curr_io = psutil.disk_io_counters(perdisk=True)
        curr_time = time.time()
        time_elapsed = curr_time - self.prev_time

        result = {}
        for disk in disks:
            # 初始化 prev_io 如果不存在
            if not hasattr(self, 'prev_io') or disk not in self.prev_io:
                self.prev_io = curr_io
                self.prev_time = curr_time
                result[disk] = {'read_kb_per_sec': 0.0, 'write_kb_per_sec': 0.0}
                continue

            # 计算读写速度
            if time_elapsed > 0:
                read_kb = (curr_io[disk].read_bytes - self.prev_io[disk].read_bytes) / 1024
                write_kb = (curr_io[disk].write_bytes - self.prev_io[disk].write_bytes) / 1024
                read_kb_per_sec = read_kb / time_elapsed
                write_kb_per_sec = write_kb / time_elapsed
            else:
                read_kb_per_sec = write_kb_per_sec = 0.0

            result[disk] = {
                'read_kb_per_sec': round(read_kb_per_sec, 2),
                'write_kb_per_sec': round(write_kb_per_sec, 2)
            }

        # 更新状态
        self.prev_io = curr_io
        self.prev_time = curr_time

        # logger.debug(f"Disk IO speeds: {result}")
        return {'disk_io': result}

    def get_disk_stats(self) -> dict:
        """
        所有磁盘IO利用率和读写速度的统计结果
        :return:
        {
            "disk_io": {
                "nvme0n1": {
                    "utilization": 45.2,
                    "is_busy": True,
                    "read_kb_per_sec": 1024 kB/s,
                    "write_kb_per_sec": 512 kB/s,
                },
                ...
            }
        }
        """
        disks = self.get_physical_disks()
        curr_io = psutil.disk_io_counters(perdisk=True)
        curr_time = time.time()

        prev_io = self.prev_io if isinstance(self.prev_io, dict) else {}
        time_elapsed = curr_time - self.prev_time

        merged_result = {}
        for disk in disks:
            curr = curr_io.get(disk)
            prev = prev_io.get(disk)
            if not curr or not prev or time_elapsed <= 0:
                merged_result[disk] = {
                    'utilization': 0.0,
                    'is_busy': False,
                    'read_kb_per_sec': 0.0,
                    'write_kb_per_sec': 0.0,
                    'read_iops': 0.0,
                    'write_iops': 0.0,
                }
                continue

            read_kb = (curr.read_bytes - prev.read_bytes) / 1024
            write_kb = (curr.write_bytes - prev.write_bytes) / 1024
            read_kb_per_sec = max(0.0, read_kb / time_elapsed)
            write_kb_per_sec = max(0.0, write_kb / time_elapsed)
            read_iops = max(0.0, (curr.read_count - prev.read_count) / time_elapsed)
            write_iops = max(0.0, (curr.write_count - prev.write_count) / time_elapsed)

            # Prefer device busy_time/io_time if available; fallback to read+write time.
            prev_busy = getattr(prev, 'busy_time', None)
            curr_busy = getattr(curr, 'busy_time', None)
            if prev_busy is None or curr_busy is None:
                prev_busy = getattr(prev, 'io_time', None)
                curr_busy = getattr(curr, 'io_time', None)

            if prev_busy is not None and curr_busy is not None:
                busy_delta_ms = curr_busy - prev_busy
            else:
                busy_delta_ms = (curr.read_time - prev.read_time) + (curr.write_time - prev.write_time)

            utilization = min(100.0, max(0.0, 100.0 * busy_delta_ms / (time_elapsed * 1000.0)))

            merged_result[disk] = {
                'utilization': round(utilization, 2),
                'is_busy': utilization > self.config.disk_utilization_threshold,
                'read_kb_per_sec': round(read_kb_per_sec, 2),
                'write_kb_per_sec': round(write_kb_per_sec, 2),
                'read_iops': round(read_iops, 2),
                'write_iops': round(write_iops, 2),
            }

        self.prev_io = curr_io
        self.prev_time = curr_time
        return {'disk_io': merged_result}

    def is_disk_io_stressed(self, device: str = None, threshold: float = None) -> dict:
        """
        判断磁盘 I/O 是否紧张
        :param device: 指定磁盘（如 'nvme0n1'），默认检查所有磁盘
        :param threshold: 自定义是否紧张阈值，否则用config中的配置

        :return:
            {
                "is_stressed": bool,               # 整体是否紧张
                "stressed_disks": list[str],       # 紧张的磁盘列表
                "iowait": float,                   # CPU 的 I/O 等待时间（%）
                "details": {disk: {utilization, read_kb_per_sec, write_kb_per_sec}}
            }
        """
        disk_stats = self.get_disk_stats()["disk_io"]

        # CPU iowait
        iowait = psutil.cpu_times_percent().iowait

        busy_threshold = threshold or self.config.disk_utilization_threshold
        speed_threshold = 100 * 1024  # 100 MB/s = 102400 KB/s（可根据需求调整）

        stressed_disks = []
        details = {}
        for disk, stats in disk_stats.items():
            # 如果指定了 device，只检查该设备
            if device and disk != device:
                continue

            is_busy = (
                    stats["utilization"] > busy_threshold and  # 利用率高
                    (stats["read_kb_per_sec"] + stats["write_kb_per_sec"]) > speed_threshold  # 吞吐量高
            )
            details[disk] = {**stats, "is_busy": is_busy}
            if is_busy:
                stressed_disks.append(disk)

        is_stressed = bool(stressed_disks) and iowait > 10  # 10%

        return {
            "is_stressed": is_stressed,
            "stressed_disks": stressed_disks,
            "iowait": iowait,
            "details": details,
        }

def main():
    """调试用主函数"""
    logger.info("==== Starting Resource Monitor ====")

    monitor = ResourceMonitor()

    try:
        while True:
            results = monitor.get_top_resource_consumers()
            for i, result in enumerate(results, 1):
                logger.debug(f"\n=== Top Resource Consumer #{i} ===")
                logger.debug(f"Process: {result['process']['name']} (PID: {result['process']['pid']})")
                logger.debug(f"CPU: {result['process']['cpu']}% | Memory: {result['process']['memory_mb']}MB")
                logger.debug(f"Score: {result['process']['score']:.2f}")
                logger.debug(f"Cmd: {result['process']['cmdline'][:100]}...")

                if result['app']:
                    logger.debug(f"\nMatched to: {result['app']['name']} ({result['app']['type']})")
                    logger.debug(f"ID: {result['app']['id']}")
                else:
                    logger.debug("\nNo matching application found")

            sleep(5)

    except KeyboardInterrupt:
        logger.info("\nMonitoring stopped by user")
    except Exception as e:
        logger.error(f"Error: {str(e)}")


if __name__ == "__main__":
    main()
