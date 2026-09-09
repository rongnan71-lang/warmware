# 🔥 WarmWare

> **自适应计算负载调度与热力学转化框架**
>
> 一套面向通用计算硬件的自适应负载调度系统，通过精密控制的计算任务分配，实现计算资源向热能的高效、可控、安全转化。

---

## 概述

WarmWare 是一个跨平台的自适应负载调度框架。它在运行时持续感知硬件环境与系统负载，动态计算各计算资源的"保护区"与"可用区"，并将可控的计算负载精确注入可用区间，从而实现**计算能耗向热能的定向转化**。

核心设计哲学：**不与系统争资源，只利用系统的闲置产能。**

> ⚠️ 本框架不以性能测试或基准测评为目标。其唯一存在意义，是在特定气候条件下，为操作者提供一种基于现有硬件的、可持续的热力学补偿方案。

---

## 核心特性

- **自适应资源保护区**：不采用固定比例预留，而是基于实时采样的「当前占用 + 安全余量」动态计算保护区，剩余产能才纳入调度范围。运行时每 2 秒重新感知，根据瓶颈资源实时调节注入强度。
- **硬件能力自动探测**：启动时自动识别 CPU 拓扑、独立显卡 / 集成显卡、显存容量与系统内存。单核设备、低内存设备自动切换至保守调度策略，确保在各类硬件上均能安全运行。
- **多后端计算注入**：自动选择最优计算后端 —— NVIDIA 显卡采用 CUDA 张量运算，AMD / Intel / 摩尔线程采用 Vulkan Compute，均不可用时回退至纯 CPU 多进程注入。
- **真·全核占用（GIL 旁路）**：CPU 注入采用多进程架构，每个逻辑核心对应一个长驻进程，彻底绕开全局解释器锁，实现真正的全核心占用，而非线程级伪并行。
- **自我干扰免疫**：安全守护从系统读数中扣除框架自身贡献的负载（EMA 平滑 + 保守系数），仅对外部负载做出响应，避免自我反馈引起的调度震荡。
- **死区与限速调节**：瓶颈余量在稳定区间内保持当前注入强度不变（死区），杜绝微小波动引起的频繁调节；每次强度变化限速，确保过渡平滑无跳变。
- **跨平台**：Windows / Linux 通用，提供一键打包脚本，可生成单文件可执行程序。

---

## 快速开始

### 源码运行

```bash
pip install -r requirements.txt
python main.py
```

> 首次运行前，如需自定义应用图标，可执行 `python build_icon.py` 生成 `warmware.ico`（纯标准库实现，无需额外依赖）。

### Windows 打包

```bash
# 完整版（含 GPU 计算后端，体积较大）
build_exe.bat

# 精简版（仅 CPU 注入，体积小巧，适合分发）
build_exe_cpu.bat
```

### Linux 打包

```bash
bash build_deb.sh
# 产物：dist/warmware_*.deb
# 安装：sudo apt install ./dist/warmware_*.deb
```

---

## 调度模式

| 模式 | 描述 |
|------|------|
| **自动（推荐）** | 启动时探测硬件与基线负载，自动选择最优调度策略 |
| 仅 CPU / 低功率 | 仅通过 CPU 注入，低功率模式更温和 |
| GPU 优先 | 以 GPU 注入为主，CPU 辅助 |
| 双引擎 / 低功率 | CPU + GPU 协同注入 |
| **激进模式** | 全资源满功率注入；安全守护仍实时压制，确保系统不进入不可用状态 |
| 关闭 | 停止所有注入 |

> 无论何种模式，安全守护均将各资源保护区约束在硬上限以内（CPU ≤ 75%、GPU ≤ 85%、内存 ≤ 92%）。系统最坏情况仅为响应延迟增加，**不会进入不可恢复状态**。

---

## 安全机制

1. **启动基线采样**：点火前采样 5 秒建立系统基线，此期间不进行任何注入。
2. **动态保护区计算**：`保护区 = 当前占用 + 安全余量`，并受硬上限约束。
3. **实时瓶颈感知**：每 2 秒采集 CPU / GPU / 内存 / 系统负载，取最小余量为瓶颈。
4. **自我贡献扣除**：从读数中扣除框架自身制造的负载，仅响应外部负载变化。
5. **死区迟滞**：瓶颈余量在稳定区间内不动作，防止微震荡。
6. **限速调节**：每次强度变化不超过 ±0.15（每 2 秒），平滑过渡。
7. **紧急降载连续确认**：外部负载真的爆满时，先降载；连续 3 轮（约 6 秒）确认后才执行紧急降载，防止单次读数抖动误判。
8. **设备自适应**：单核 / 低内存设备自动大幅加大保护区，自动回退至 CPU-only 模式。

---

## 目录结构

```
WarmWare/
├── main.py                  # 程序入口
├── requirements.txt         # 依赖（核心仅 psutil，其余标准库）
├── build_icon.py            # 纯标准库生成应用图标
├── build_exe.bat            # Windows 完整版打包
├── build_exe_cpu.bat        # Windows 精简版打包
├── build_deb.sh             # Linux deb 打包
├── warmware.ico             # 应用图标（可由 build_icon.py 生成）
├── packaging/
│   └── debian/control       # deb 包元数据
├── warmware/
│   ├── __init__.py
│   ├── detect.py            # 硬件能力自动探测
│   ├── safety.py            # 自适应安全守护（保护区计算 + 实时监控）
│   ├── engines.py           # 负载注入引擎（CPU 多进程 + 可选 GPU 多后端）
│   └── gui.py               # tkinter 控制面板
├── README.md
├── LICENSE
└── .gitignore
```

---

## 依赖

- **核心依赖**：`psutil`（跨平台系统资源读取）
- **可选依赖**：
  - NVIDIA GPU → `torch`（CUDA 后端）
  - AMD / Intel / 摩尔线程 → 系统 Vulkan 驱动
  - 均无 → 自动使用 CPU 多进程注入

---

## 许可证

MIT License（详见 [LICENSE](LICENSE)）。

---

---

# 🔥 WarmWare

> **Adaptive Compute Load Scheduling & Thermodynamic Conversion Framework**
>
> A cross-platform adaptive load scheduling system that achieves efficient, controllable, and safe conversion of computational resources into thermal energy through precisely calibrated compute task allocation.

---

## Overview

WarmWare is a cross-platform adaptive load scheduling framework. It continuously senses the hardware environment and system load at runtime, dynamically computes the "reserved zone" and "available zone" for each compute resource, and injects controllable compute load precisely into the available interval — thereby achieving **directed conversion of compute energy consumption into thermal energy**.

Core design philosophy: **Never compete with the system for resources; only harness the system's idle capacity.**

> ⚠️ This framework is not intended for performance testing or benchmarking. Its sole purpose is to provide operators with a sustainable, hardware-based thermodynamic compensation solution under specific climatic conditions.

---

## Key Features

- **Adaptive Resource Reservation**: Instead of fixed-percentage reservation, the reserved zone is computed dynamically from real-time sampled "current usage + safety margin." Only the remaining capacity enters the scheduling scope. Re-sensing every 2 seconds at runtime, injection intensity is adjusted in real time based on the bottleneck resource.
- **Automatic Hardware Capability Detection**: At startup, CPU topology, dedicated / integrated GPU, VRAM, and system memory are automatically identified. Single-core and low-memory devices automatically switch to conservative scheduling strategies, ensuring safe operation across all hardware classes.
- **Multi-Backend Compute Injection**: Automatically selects the optimal compute backend — NVIDIA GPUs use CUDA tensor operations, AMD / Intel / Moore Threads use Vulkan Compute, and when neither is available, falls back to pure CPU multi-process injection.
- **True All-Core Occupancy (GIL Bypass)**: CPU injection uses a multi-process architecture with one long-running process per logical core, completely bypassing the Global Interpreter Lock for genuine all-core occupancy — not thread-level pseudo-parallelism.
- **Self-Interference Immunity**: The safety guardian subtracts the framework's own contributed load from system readings (EMA smoothing + conservative coefficient), responding only to external load and avoiding self-feedback scheduling oscillations.
- **Dead Zone & Rate-Limited Adjustment**: Injection intensity remains unchanged when bottleneck headroom falls within the stable interval (dead zone), eliminating frequent adjustments from micro-fluctuations; every intensity change is rate-limited for smooth, jump-free transitions.
- **Cross-Platform**: Works on Windows / Linux, with one-click packaging scripts producing single-file executables.

---

## Quick Start

### Run from Source

```bash
pip install -r requirements.txt
python main.py
```

> Before first run, if a custom application icon is desired, execute `python build_icon.py` to generate `warmware.ico` (pure standard library implementation, no extra dependencies needed).

### Windows Packaging

```bash
# Full version (includes GPU compute backend, larger file size)
build_exe.bat

# Lite version (CPU injection only, compact size, ideal for distribution)
build_exe_cpu.bat
```

### Linux Packaging

```bash
bash build_deb.sh
# Output: dist/warmware_*.deb
# Install: sudo apt install ./dist/warmware_*.deb
```

---

## Scheduling Modes

| Mode | Description |
|------|-------------|
| **Auto (Recommended)** | Probes hardware and baseline load at startup, automatically selects optimal scheduling strategy |
| CPU Only / Low Power | CPU injection only; low power mode is gentler |
| GPU Primary | GPU injection primary, CPU auxiliary |
| Hybrid / Low Power | CPU + GPU coordinated injection |
| **Aggressive** | Full-power injection across all resources; safety guardian still enforces real-time suppression to keep the system operational |
| Off | Stops all injection |

> Regardless of mode, the safety guardian constrains each resource's reserved zone within hard caps (CPU ≤ 75%, GPU ≤ 85%, Memory ≤ 92%). Worst case is increased response latency — **the system will never enter an unrecoverable state**.

---

## Safety Mechanisms

1. **Startup Baseline Sampling**: 5-second baseline sampling before ignition, no injection during this period.
2. **Dynamic Reservation Calculation**: `Reserved = Current Usage + Safety Margin`, bounded by hard caps.
3. **Real-Time Bottleneck Sensing**: CPU / GPU / Memory / system load collected every 2 seconds, minimum headroom taken as bottleneck.
4. **Self-Contribution Deduction**: Framework's own load subtracted from readings, responding only to external load changes.
5. **Dead Zone Hysteresis**: No action when bottleneck headroom falls within stable interval, preventing micro-oscillation.
6. **Rate-Limited Adjustment**: Each intensity change ≤ ±0.15 (per 2 seconds), smooth transitions.
7. **Emergency Derate Confirmation**: When external load truly spikes, derate first; emergency derate to minimum only after 3 consecutive confirmations (~6 seconds), preventing false triggers from single reading jitter.
8. **Device Adaptation**: Single-core / low-memory devices automatically enlarge reservation significantly and fall back to CPU-only mode.

---

## Dependencies

- **Core**: `psutil` (cross-platform system resource reading)
- **Optional**:
  - NVIDIA GPU → `torch` (CUDA backend)
  - AMD / Intel / Moore Threads → system Vulkan drivers
  - Neither → automatic CPU multi-process injection

---

## License

MIT License (see [LICENSE](LICENSE)).
