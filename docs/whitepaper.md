# Intel XPU SmarTune: App-Centric, Pressure-Driven Resource Governance for AI NAS Platforms

*Technical Whitepaper — Intel Corporation, 2026*

---

## Abstract

Intel XPU SmarTune is a platform-level resource governance framework designed for AI Network-Attached Storage (NAS) systems running concurrent, heterogeneous AI workloads across CPU, integrated GPU (iGPU), discrete GPU (dGPU), and NPU. Rather than applying static, host-level quotas, SmarTune implements a **closed-loop, application-centric control system** that continuously senses system pressure (via Linux PSI), intercepts application launch/exit events (via eBPF), and dynamically adjusts per-application resource limits (via cgroups v2) across CPU, memory, disk I/O, and network. This paper describes the system architecture, key technical innovations, implementation details, and presents benchmark methodology with placeholders for measured results.

---

## 1. Introduction and Motivation

AI NAS platforms increasingly serve as consolidated inference and data-pipeline hosts—running multiple workloads simultaneously: vision inference tasks, VLM (Vision-Language Model) services, background indexing, media transcoding, and file-serving. These workloads have fundamentally different resource demands and latency sensitivity. Under concurrent execution:

- **Critical workloads** (e.g., real-time inference) require guaranteed CPU and memory headroom.
- **Best-effort workloads** (e.g., background indexing) can absorb throttling without visible impact.
- **I/O-intensive tasks** can starve other workloads of disk and network bandwidth.

Existing approaches fall short:

| Approach | Limitation |
|---|---|
| Static cgroups | Cannot adapt to dynamic workload mix |
| System-level OOM killer | Blunt; kills by memory watermark, ignores app semantics |
| Manual `nice`/`ionice` | Operator-dependent; no holistic pressure awareness |
| Container orchestrators | Heavyweight; not designed for embedded NAS environments |

SmarTune addresses these gaps with a lightweight, fully integrated, application-aware control loop—deployable as a single systemd service on Ubuntu/Debian with no container runtime dependency.

---

## 2. System Architecture

SmarTune is structured in two independently deployable layers (see Figure 1):

```
┌─────────────────────────────────────────────────┐
│               React Dashboard (UI)               │
│   System Overview | App Resources | History |    │
│   Process Monitor | Balancer | About            │
└──────────────────┬──────────────────────────────┘
                   │  HTTPS REST + SSE  (port 9001)
┌──────────────────▼──────────────────────────────┐
│          SmarTune Backend (smartune.py)          │
│                                                  │
│  ┌─────────────────┐   ┌──────────────────────┐ │
│  │   Monitor Layer  │   │   Balancer Layer      │ │
│  │ (standalone-able)│   │ (depends on monitor)  │ │
│  │                  │   │                      │ │
│  │  PSI reader      │   │  DynamicBalancer      │ │
│  │  CPU/Mem/Disk    │   │  MaxPriorityQueue     │ │
│  │  Network monitor │   │  LimitRegistry        │ │
│  │  GPU (PMU/RAPL)  │   │  AppIntercept (eBPF)  │ │
│  │  NPU (PMT)       │   │  NetworkController    │ │
│  │  System info     │   │  Controllers:         │ │
│  │  History/SQLite  │   │   cpu / mem / io /    │ │
│  │                  │   │   network / governor  │ │
│  └─────────────────┘   └──────────────────────┘ │
└─────────────────────────────────────────────────┘
              ↕ cgroups v2 / eBPF / tc+iptables
┌─────────────────────────────────────────────────┐
│                  Linux Kernel                    │
│   PSI  |  cgroups v2  |  eBPF/BCC  |  tc/HTB   │
└─────────────────────────────────────────────────┘
```

**Figure 1: SmarTune layered architecture.**

The monitor layer can run standalone (telemetry only). The balancer layer adds active resource control and requires the monitor.

---

## 3. Key Technical Innovations

### 3.1 App-Centric Resource Governance via cgroups v2

The fundamental unit of control in SmarTune is the **managed application** (controlled app), not the process or container. Each registered app is assigned:

- A unique `id` matching its systemd scope/service cgroup path.
- A **priority level**: Critical (100) / High (80) / Medium (50) / Low (20).
- Per-resource limit policies sourced from `config.yaml` and overridable at runtime via REST API.

Resource limits are applied through cgroups v2 kernel subsystems:

| Resource | cgroups v2 Interface | What SmarTune Sets |
|---|---|---|
| CPU | `cpu.max` | Quota + period (e.g., 70% of all CPUs) |
| Memory | `memory.high` | Soft limit causing reclaim pressure |
| Disk I/O | `io.max` | Per-device: `rbps`, `wbps`, `riops`, `wiops` |
| I/O priority | `io.weight` | Relative weight between apps |
| CPU frequency | `cpupower` governor | `powersave` / `performance` by pressure level |

**Priority-based limit policy (default):**

| Priority | CPU quota | Memory.high | Write BW | Read BW |
|---|---|---|---|---|
| Critical | *Unlimited* | *Unlimited* | — | — |
| High | 70% | 30% total | 50 MB/s | 60 MB/s |
| Medium | 50% | 20% total | 40 MB/s | 50 MB/s |
| Low | 40% | 10% total | 20 MB/s | 30 MB/s |
| Undefined | 30% | 10% total | 10 MB/s | 20 MB/s |

> 📌 **Data needed:** Fill in measured cgroup enforcement overhead (CPU cycles, latency introduced by limit application).

### 3.2 Pressure-Driven Closed Control Loop

The core scheduling decision relies on **Linux PSI (Pressure Stall Information)**—a kernel mechanism that quantifies how much time processes are stalled waiting for CPU, memory, or I/O resources.

SmarTune computes a composite weighted pressure score:

```
score = w_cpu × PSI_cpu + w_mem × PSI_mem + w_io × PSI_io
```

Default weights: `cpu=2, memory=7, io=1` (memory-weighted, reflecting AI workload characteristics). The score is clamped to `[0.0, 1.0]` and mapped to four levels:

| Level | Score Range | Balancer Action |
|---|---|---|
| Low | < 0.4 | No action; restore if pending |
| Medium | 0.4–0.6 | No new limits; monitor |
| High | 0.6–0.8 | Pre-fetch top consumers (warm cache) |
| Critical | 0.8–1.0 | Apply limits to top resource consumers |

**Dominant-app correction:** When the already-limited app remains the top consumer but the system is not globally busy (CPU < 90%, RAM < 90%), weights are divided by a configurable `dominant_app_reduce_factor` (default 3.5) to prevent over-throttling.

**Progressive restore:** When pressure drops below the high threshold, limits are lifted in FIFO order: the oldest-limited app is restored first, one at a time, preventing a simultaneous release that could spike pressure again.

> 📌 **Data needed:** PSI score distributions under typical NAS workloads; oscillation rate (limit→restore→limit cycles per hour).

### 3.3 eBPF-Based Real-Time Application Interception

SmarTune uses **eBPF via BCC** to attach tracepoints on `execve` and process-exit kernel events. The BPF C program (`bpf_event.c`) captures:

- `comm` (15-char command name) and `filename` (executable path) of newly launched processes.
- PID and exit events for tracked processes.

Events are streamed to userspace via a BPF perf ring buffer and matched against the configured `bpf_name` list for each managed app. This enables:

1. **Zero-latency detection** of a managed app launch without polling `/proc`.
2. **Multi-process app tracking**: a single logical app (e.g., a VLM service with separate agent and model subprocesses) can register multiple `bpf_name` entries; `stopped` status is only emitted when the last tracked PID exits.
3. **Pre-existing app discovery**: a one-time `/proc` scan at startup registers apps that were already running before the balancer service started.

The cold-start path seeds `monitored_app_launched` and `app_live_pids` dictionaries so BPF exit events fire correctly for pre-existing processes.

> 📌 **Data needed:** eBPF event dispatch latency (launch-event → userspace handler, median and p99); false-positive rate on execve matching.

### 3.4 Priority-Queue-Based Launch Deferral

When system pressure reaches **critical** level or disk I/O utilization exceeds the configured threshold, newly detected low-priority app launches are intercepted and suspended via `SIGSTOP`, then inserted into a **max-priority queue**:

```
MaxPriorityQueue: heap ordered by app priority (Critical=100, High=80, ...)
```

On pressure recovery, the queue is drained in descending-priority order: each app is resumed via `SIGCONT`. This mechanism provides:

- **Back-pressure on low-priority work** exactly when the system needs relief.
- **Deterministic ordering**: higher-priority apps always resume before lower-priority ones regardless of arrival order.
- **Manual cancellation** via REST API or dashboard.

The `TopConsumerPrefetcher` background worker pre-fetches the top-resource-consumer list asynchronously while pressure is `high` (before it reaches `critical`), eliminating the multi-second sampling cost from the critical throttle path.

> 📌 **Data needed:** Queue depth distribution under stress tests; time-to-relaunch after pressure drops; comparison of critical-path latency with and without prefetch.

### 3.5 Critical App Keep-Alive

For apps designated as **Critical** priority, SmarTune applies two protections:

1. **OOM score tuning**: sets `oom_score_adj` to a negative value (configurable), reducing the OOM killer's probability of selecting the process under memory pressure.
2. **Continuous process monitoring**: the balancer loop tracks whether the registered PIDs of critical apps remain alive and logs anomalies when processes unexpectedly exit.

> 📌 **Data needed:** OOM event count (baseline vs. SmarTune) under synthetic memory pressure; critical app survival rate.

### 3.6 Four-Class Network Traffic Shaping

Network governance is implemented as an independent controller using a stack of:

- **tc/HTB (Hierarchical Token Bucket)**: egress traffic shaping with per-priority-class rate and ceiling.
- **IFB (Intermediate Functional Block) device**: enables ingress traffic shaping (redirect → IFB → HTB).
- **iptables `mangle` table**: marks packets with per-app `fwmark` values for tc classifier matching.
- **cgroup-based process marking**: ensures all processes of a managed app share the same mark.

Four priority classes are defined:

| Class | Priority | Rate (min) | Ceil (max) | Burst |
|---|---|---|---|---|
| Critical | Highest | 500 Mbit/s | 900 Mbit/s | 64k |
| High | High | 300 Mbit/s | 800 Mbit/s | 32k |
| Low | Low | 100 Mbit/s | 800 Mbit/s | 16k |
| System | System ports | 50 Mbit/s | 100 Mbit/s | 8k |

> Note: Values above are defaults for a 1 Gbit/s interface; configurable via `config.yaml`.

**Pressure-responsive ceiling reduction:**
Network pressure is computed from a moving-average window of NIC tx/rx utilization, mapped to low/medium/high/critical levels. When critical:

1. Low-priority class `ceil` is reduced to 50% of its current value, or clamped to its `min` rate.
2. If still critical after the `recover_cooldown`, high-priority class `ceil` is similarly reduced.
3. Critical class `ceil` is **never** reduced.

Recovery is symmetric: high-priority class is restored first (partially if still under load, fully otherwise), then low-priority.

**System port protection**: Common ports (22/SSH, 53/DNS, 80/HTTP, 443/HTTPS, 123/NTP) are automatically assigned to the system priority class, regardless of which app they belong to.

> 📌 **Data needed:** Per-class achieved bandwidth under saturation; latency comparison for critical vs. low-priority apps during network stress; system port throughput before/after enabling network governance.

### 3.7 Unified XPU Telemetry (CPU / GPU / NPU)

SmarTune provides a unified telemetry plane covering all compute accelerators present on Intel client platforms:

**GPU monitoring (i915/Xe kernel driver, RAPL, fdinfo):**
- Per-card metrics: gt0/gt1 frequencies (current, actual, max), RC6 residency.
- Per-engine utilization: Render (rcs), Compute (ccs), Video Encode (vecs), Video Decode (vcs), Blitter (bcs).
- VRAM usage (used/free/total MB) and GPU + package power (RAPL).
- Throttle reason detection.
- iGPU/dGPU automatic distinction via PCI address and driver type.

**NPU monitoring (Intel PMT telemetry):**
- Utilization (%), power (W), temperature (°C), operating frequency (MHz).
- NoC bandwidth (MiB/s), memory utilization.
- Per-process NPU memory tracking via `/proc/[pid]/fdinfo` (`intel_vpu` driver, `drm-resident-memory`).
- Supported platforms: MTL, ARL, ARL-H, ARL-S, LNL, PTL (GUIDs: `0x130670b2`, `0x1306a0b3`, `0x1306a0b2`, `0x1306a0b4`, `0x3072005`, `0x3086000`).

**CPU/memory monitoring:**
- Per-core usage, P-core/E-core topology, frequency governor state.
- Memory pressure via PSI + physical utilization.

> 📌 **Data needed:** Telemetry collection overhead (CPU% consumed by SmarTune itself); measurement accuracy comparison vs. vendor tools (Intel GPU Top, NPU SMI).

### 3.8 Historical Snapshot Storage and Retrieval

All dynamic metrics are periodically written to a **SQLite database** (Peewee ORM, WAL mode) as time-series snapshots. The `MonitorSnapshot` table stores full JSON payloads tagged with `snapshot_type` (static/dynamic), `source`, and UNIX timestamps.

The REST API (`/monitor/history`) supports:
- Time-range filtering (`start_time`, `end_time` as UNIX timestamps).
- Projection via `sections=cpu,gpu` to return only requested subsystems.
- Configurable retention period with automatic purge of old records.

The dashboard **History** tab renders these as time-series charts for system pressure, CPU/GPU/NPU utilization, and disk/network I/O over selectable time windows.

### 3.9 REST + SSE API and React Dashboard

SmarTune exposes a fully documented REST API (see `docs/API_ENDPOINTS.md`) over HTTPS (self-signed TLS auto-generated at startup). All endpoints are protected by a token-based authentication scheme (`X-Auth-Token` header; HMAC-SHA256 comparison).

Real-time app status changes are streamed via **Server-Sent Events (SSE)** (`/app/events`), enabling the dashboard to reflect balancer decisions (limit applied, app queued, resource restored, app stopped) without polling.

The **React dashboard** (Vite + React 18 + TypeScript + Ant Design v5) provides six tabs:

| Tab | Content |
|---|---|
| System Overview | Live CPU, memory, disk, network, iGPU, dGPU, NPU panels |
| App Resources | Per-controlled-app CPU%, memory MB, disk I/O, GPU% |
| Process Resources | Per-process PID, CPU avg%, RSS, I/O rate (like `top`) |
| Balancer | App management: priority, limits, restore, keep-alive, delete; pending queue view |
| History | Time-series charts of system pressure and resource utilization |
| About | Static hardware/software inventory (CPU topology, GPU config, NPU device, OS/BIOS/drivers) |

A `/smartune/capabilities` endpoint allows the dashboard to auto-adapt to monitor-only deployments (no balancer features).

---

## 4. Implementation Details

### 4.1 Technology Stack

| Component | Technology |
|---|---|
| Backend runtime | Python 3.12, Flask (Blueprints) |
| Database | SQLite via Peewee ORM (WAL mode) |
| eBPF runtime | BCC (BPF Compiler Collection) |
| cgroups | cgroups v2 via libcgroup + direct sysfs writes |
| Network control | iproute2 (`tc`), iptables (mangle table), IFB |
| CPU governor | `cpupower` |
| GPU monitoring | Intel i915/Xe `fdinfo`, RAPL via `perf_event`, PMU |
| NPU monitoring | Intel PMT (Platform Monitoring Technology) |
| Dashboard | React 18, TypeScript, Vite 7, Ant Design v5 |
| TLS | Auto-generated self-signed certificate at startup |
| Authentication | HMAC-SHA256 token; persisted to `key/api_token` (mode 0600) |

### 4.2 Deployment

SmarTune runs as a **single systemd service** (`smartune.service`) on Ubuntu/Debian, requiring only:

- `bcc`, `cpupower`, `psutil`, `peewee`, `flask`, optionally `libcgroup`.
- Verified platforms: **MTL, PTL, WildCat Lake** with Ubuntu or Debian.
- No container runtime, no Kubernetes, no additional message brokers.

Two startup modes are supported:
- `-a` (default): balancer + monitor on port 9001.
- `-m`: monitor-only (standalone telemetry).

### 4.3 Configuration

All tunable parameters are centralized in `config/config.yaml`:

- Pressure thresholds and PSI weights.
- Per-priority CPU/memory/disk limit ratios.
- Network interface, bandwidth, per-class rate/ceil/burst.
- Managed app list (name, id, commandline, bpf_name, process_names).
- Blacklist of system processes excluded from monitoring and the wizard.
- Cooldown intervals, disk utilization thresholds, idle check intervals.

Runtime changes (priority, per-app limit overrides, passive control toggle, weights) are accepted via REST API with optimistic concurrency and written back to `config.yaml` atomically.

---

## 5. Evaluation

### 5.1 Test Methodology

All benchmarks are conducted on **[TARGET PLATFORM: MTL / PTL / WildCat Lake]** with **[OS: Ubuntu XX.XX, Kernel X.X.X]** using a representative AI NAS workload mix:

**Workload scenarios:**

| Scenario | Description |
|---|---|
| W1 – CPU-heavy | Parallel CPU inference (e.g., OpenVINO CPU plugin) + background indexing |
| W2 – Memory-heavy | Large-batch VLM inference (memory-resident model) + concurrent file serving |
| W3 – Disk-heavy | Sequential media transcoding + concurrent NAS reads |
| W4 – Network-heavy | High-throughput data transfer + concurrent inference with remote data |
| W5 – Mixed XPU | GPU inference + NPU inference + CPU background tasks concurrently |

**Comparison baseline:** Same workload mix without SmarTune (no cgroup limits, no PSI-based governance, stock kernel scheduler).

### 5.2 Resource Governance Effectiveness

> 📌 **Data needed:**

| Metric | Baseline | SmarTune | Improvement |
|---|---|---|---|
| Critical-app p95 inference latency (W1, ms) | `___` | `___` | `___%` |
| Critical-app p99 inference latency (W5, ms) | `___` | `___` | `___%` |
| Critical-app throughput variance (CV%) | `___` | `___` | `___%` |
| Time for pressure to return below "high" after spike (s) | `___` | `___` | `___%` |
| Best-effort app throughput penalty (acceptable regression, %) | N/A | `≤ ___%` | — |

### 5.3 OOM and Keep-Alive

> 📌 **Data needed:**

| Metric | Baseline | SmarTune |
|---|---|---|
| OOM-kill events (30-min memory stress test) | `___` | `___` |
| Critical app survival rate (%) | `___` | `___` |
| Time to first OOM event (s) | `___` | `___` |

### 5.4 Priority Queue Efficiency

> 📌 **Data needed:**

| Metric | Value |
|---|---|
| Max queue depth observed during stress | `___` |
| Median time-to-relaunch after pressure drops (s) | `___` |
| Priority ordering correctness (%) | `___` |
| Prefetch hit rate (top-consumer list warm on critical entry, %) | `___` |

### 5.5 Network Traffic Shaping

> 📌 **Data needed:** Run with iperf3 or similar; measure per-class actual bandwidth under NIC saturation.

| Class | Configured Min | Configured Max | Measured Achieved (saturated NIC) |
|---|---|---|---|
| Critical | 500 Mbit/s | 900 Mbit/s | `___` Mbit/s |
| High | 300 Mbit/s | 800 Mbit/s | `___` Mbit/s |
| Low | 100 Mbit/s | 800 Mbit/s | `___` Mbit/s |
| System | 50 Mbit/s | 100 Mbit/s | `___` Mbit/s |

Additional metrics needed:
- SSH (port 22) latency during network saturation: baseline vs. SmarTune.
- Network pressure level oscillation rate (limit/recover cycles per minute).

### 5.6 System Overhead

> 📌 **Data needed:**

| Metric | Idle | Monitored (all sections) |
|---|---|---|
| SmarTune CPU usage (%) | `___` | `___` |
| SmarTune RSS memory (MB) | `___` | `___` |
| eBPF event processing latency, median (µs) | `___` | `___` |
| cgroups limit application latency, median (ms) | `___` | `___` |

---

## 6. Related Work

| System | Comparison |
|---|---|
| Linux cgroups (manual) | No automated pressure detection; requires operator to set static limits |
| systemd resource properties | Per-service, static; no cross-app priority or PSI feedback |
| Kubernetes / cAdvisor | Container-centric; heavyweight; not designed for single-host NAS |
| Intel oneAPI profiling | Telemetry only; no governance or control plane |
| NVIDIA MPS / MIG | GPU-specific; no CPU/memory/disk integration |

SmarTune's distinguishing properties: (1) application-granular closed-loop control across all resource domains; (2) eBPF-based launch interception without kernel patches; (3) single-service deployment on a stock Linux kernel with cgroups v2.

---

## 7. Conclusion

Intel XPU SmarTune introduces a practical, lightweight, closed-loop resource governance framework for AI NAS platforms. Its key contributions are:

1. **App-centric governance** unifying CPU, memory, disk I/O, and network under a single priority-aware control plane.
2. **PSI-driven pressure loop** with dominant-app correction and progressive restore, avoiding the oscillation trap of simple threshold controllers.
3. **eBPF-based zero-latency app interception** enabling proactive, launch-time governance decisions.
4. **Priority-queue launch deferral** ensuring higher-priority workloads are never delayed by lower-priority launches during contention.
5. **Integrated XPU telemetry** (CPU, iGPU, dGPU, NPU) via Intel PMU, RAPL, PMT, and `fdinfo` on a unified REST/SSE API.

The result is a NAS platform that maintains critical workload QoS under multi-tenant AI load while making efficient use of all available compute resources.

---

## Appendix A: Key Configuration Reference

| Parameter | Default | Description |
|---|---|---|
| `weights.cpu` | 2 | PSI CPU pressure weight |
| `weights.memory` | 7 | PSI memory pressure weight |
| `weights.io` | 1 | PSI I/O pressure weight |
| `dominant_app_reduce_factor` | 3.5 | Weight divisor when limited app is still dominant |
| `cpu_busy_threshold` | 90 | CPU % above which system is considered "busy" |
| `memory_busy_threshold` | 90 | Memory % above which system is considered "busy" |
| `disk_utilization_threshold` | 95 | Disk utilization % triggering launch deferral |
| `disk_iowait_threshold` | 10 | iowait % triggering I/O stress detection |
| `disk_io_throughput_threshold_kb` | 102400 | KB/s (100 MB/s) aggregate I/O threshold |
| `cooldown_time` | 15 s | Min seconds between successive notifications |
| `monitor_idle_check_interval` | 10 s | Pressure check interval when balancer is idle |
| `limit_reap_interval` | 2 s | How often to check if a limited app has exited |
| `enable_network_control` | true | Toggle network tc/HTB governance |
| `passive_resource_control.enabled` | true | Toggle auto-throttle on pressure (manual limits unaffected) |

## Appendix B: Supported Platforms

| Platform | Code Name | GPU | NPU PMT GUID |
|---|---|---|---|
| Intel Core Ultra (Series 1) | Meteor Lake (MTL) | iGPU (Xe-LP) | `0x130670b2` |
| Intel Core Ultra (Series 2) | Arrow Lake (ARL) | iGPU (Xe-LPG) | `0x1306a0b3` / `0x1306a0b2` / `0x1306a0b4` |
| Intel Core Ultra (Series 2) | Lunar Lake (LNL) | iGPU (Xe-LPG+) | `0x3072005` |
| Intel Core Ultra (Series 3) | Panther Lake (PTL) | iGPU | `0x3086000` |
| Intel Core Ultra (Series 3) | WildCat Lake | iGPU | TBD |

---

*For API reference, see [docs/API_ENDPOINTS.md](API_ENDPOINTS.md).*  
*For deployment instructions, see [README.md](../README.md).*  
*License: Apache 2.0 — Copyright (c) 2026 Intel Corporation*
