"""
安全守护 — 自适应保护区
========================
核心思路：
  不是"CPU 保留 15%"，而是"当前用了多少 + 安全余量 = 保护区，
  剩下的才是暖炉能用的"。

启动时采样基准线，运行时每 2 秒重新感知真实系统状态，
根据"此刻还剩多少余量"实时调节产热强度，绝不让系统卡死。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("warmware.safety")


class SafetyGuardian:
    """自适应安全守护。负责计算保护区 + 实时瓶颈感知。"""

    # 默认安全余量（在基准之上额外保留）
    DEFAULT_MARGIN = {
        "cpu": 0.10,            # CPU 额外保留 10%
        "gpu": 0.15,            # GPU 额外保留 15%
        "mem": 0.15,            # 内存额外保留 15%
        "load_per_core": 0.30,  # 每核至少保留 0.3 负载余量
    }

    # 各资源保护区硬上限（防止极端条件下仍压榨过度）
    # v2.2 放宽上限：允许烧到更高负载，更贴近"拿来取暖"的本意。
    # 仍保留最低底线：保护区不会超过上限，因此系统绝不至于完全卡死。
    CPU_RESERVE_CAP = 0.75
    GPU_RESERVE_CAP = 0.85
    MEM_RESERVE_CAP = 0.92

    def __init__(self, caps, margin: Optional[dict] = None):
        from .detect import HardwareCapabilities
        self.caps: HardwareCapabilities = caps
        self.margin = dict(self.DEFAULT_MARGIN)
        if margin:
            self.margin.update(margin)

        # 根据硬件能力自动收紧安全余量
        self._apply_hardware_tuning()

        self.baseline = None
        self.cpu_reserve = 0.0
        self.cpu_available = 1.0
        self.gpu_reserve = 0.0
        self.gpu_available = 1.0
        self.mem_reserve = 0.0
        self.mem_available = 1.0
        self.max_load = 0.0

        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._on_update: Optional[Callable] = None   # 回调(dict)
        self._on_alert: Optional[Callable] = None    # 回调(level, msg)

        self._cpu_reader = self._make_cpu_reader()

        # —— v2.3 自我干扰修复 ——
        # 引擎引用：安全循环需要知道"暖炉自己贡献了多少负载"，
        # 从读数里扣除，避免把自己的高占用误判为系统资源紧张。
        self._engine = None
        # EMA 平滑状态：滤掉 psutil 读数的抖动
        self._ema = None
        # 紧急降载需要连续确认（默认 3 次 ≈ 6 秒），防止单次抖动误判
        self._emer_count = 0
        self.EMER_CONFIRM = 3
        # 绝对保护上限：CPU 总峰值、内存（不管谁占的，真不能超）
        self.ABS_PEAK_CAP = 0.97
        self.ABS_MEM_CAP = 0.95

    # ------------------------------------------------------------------
    # 硬件能力收紧
    # ------------------------------------------------------------------
    def _apply_hardware_tuning(self):
        c = self.caps
        if c.is_low_mem:
            self.margin["mem"] = max(self.margin["mem"], 0.30)
            self.margin["cpu"] = max(self.margin["cpu"], 0.15)
            log.info("低内存设备：加大内存/CPU 保护区")
        if c.is_single_core:
            self.margin["cpu"] = max(self.margin["cpu"], 0.40)
            self.margin["load_per_core"] = max(self.margin["load_per_core"], 0.50)
            log.warning("单核设备：极端保守，CPU 保护区至少 40%")

    # ------------------------------------------------------------------
    # 读数
    # ------------------------------------------------------------------
    def _make_cpu_reader(self):
        """返回一个读取 [0,1] 每核使用率的函数；无 psutil 时退化为 0。"""
        try:
            import psutil
            return lambda: [c / 100.0 for c in psutil.cpu_percent(percpu=True, interval=None)]
        except Exception:
            return lambda: [0.0] * max(1, self.caps.cpu_logical_cores)

    def _read_gpu_usage(self) -> float:
        """GPU 使用率 [0,1]。

        有引擎且 GPU 后端在跑时，用引擎上报的占用（Vulkan/CUDA 都准）；
        否则（无引擎，如启动基准线采样）回退 nvidia-smi 实测。
        """
        # 引擎在跑 GPU → 用引擎上报，避免 Vulkan 后端"盲烧"
        eng = self._engine
        if eng is not None and eng.running and eng.gpu.available:
            return eng.gpu.current_utilization()
        # 无引擎 → 回退 nvidia-smi 实测（NVIDIA）
        if not self.caps.gpu_available:
            return 0.0
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if result.returncode == 0 and result.stdout.strip():
                val = float(result.stdout.strip().splitlines()[0].strip())
                return max(0.0, min(1.0, val / 100.0))
        except Exception:
            pass
        return 0.0

    def _read_mem(self) -> float:
        """内存使用率 [0,1]。"""
        try:
            import psutil
            return psutil.virtual_memory().percent / 100.0
        except Exception:
            return 0.0

    def _read_load_avg(self) -> float:
        """1 分钟系统负载（跨平台尽力）。Windows 用 psutil 换算。"""
        try:
            import psutil
            if os.name == "nt":
                return float(psutil.getloadavg()[0])
            return float(os.getloadavg()[0])
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # 基准线
    # ------------------------------------------------------------------
    def take_baseline(self, seconds: float = 5.0, interval: float = 0.5,
                      on_log: Optional[Callable[[str], None]] = None):
        """
        采样 N 秒建立系统基准线。采样期间不做任何加热。
        返回 (ok, baseline_dict)。
        """
        cpu_samples, gpu_samples, mem_samples, load_samples = [], [], [], []
        log_it = on_log or (lambda m: log.info(m))
        log_it("🔍 WarmWare 正在感知系统环境…")

        n = max(1, int(seconds / interval))
        for _ in range(n):
            cpu_samples.append(self._cpu_reader())
            gpu_samples.append(self._read_gpu_usage())
            mem_samples.append(self._read_mem())
            load_samples.append(self._read_load_avg())
            time.sleep(interval)

        cores = len(cpu_samples[0]) if cpu_samples else max(1, self.caps.cpu_logical_cores)
        self.baseline = {
            "cpu_per_core": [
                sum(core_vals) / len(cpu_samples) for core_vals in zip(*cpu_samples)
            ],
            "gpu_usage": sum(gpu_samples) / len(gpu_samples),
            "mem_usage": sum(mem_samples) / len(mem_samples),
            "load_avg": sum(load_samples) / len(load_samples),
            "cpu_count": self.caps.cpu_logical_cores,
            "sampled_cores": cores,
            "timestamp": time.time(),
        }
        self._calc_reserves()
        return True, self.baseline

    def _calc_reserves(self):
        """基于基准线计算保护区/可用区，写入 self。"""
        b = self.baseline
        m = self.margin

        # CPU：取所有核心中最高的为统一保护区
        peak_core = max(b["cpu_per_core"]) if b["cpu_per_core"] else 0.0
        self.cpu_reserve = min(peak_core + m["cpu"], self.CPU_RESERVE_CAP)
        self.cpu_available = max(0.0, 1.0 - self.cpu_reserve)

        # GPU
        self.gpu_reserve = min(b["gpu_usage"] + m["gpu"], self.GPU_RESERVE_CAP)
        self.gpu_available = max(0.0, 1.0 - self.gpu_reserve)

        # 内存
        self.mem_reserve = min(b["mem_usage"] + m["mem"], self.MEM_RESERVE_CAP)
        self.mem_available = max(0.0, 1.0 - self.mem_reserve)

        # 负载
        self.max_load = b["cpu_count"] * (1.0 - m["load_per_core"])

    def reserve_report(self) -> dict:
        """当前保护区报告，用于 GUI 展示。"""
        return {
            "cpu_reserve": self.cpu_reserve,
            "cpu_available": self.cpu_available,
            "gpu_reserve": self.gpu_reserve,
            "gpu_available": self.gpu_available,
            "mem_reserve": self.mem_reserve,
            "mem_available": self.mem_available,
            "max_load": self.max_load,
        }

    # ------------------------------------------------------------------
    # 实时安全循环
    # ------------------------------------------------------------------
    def set_engine(self, engine):
        """关联引擎引用，让安全循环能扣除暖炉自己制造的负载。"""
        self._engine = engine

    def start_safety_loop(self, on_update=None, on_alert=None):
        """启动实时安全监控循环。on_update(dict) 每轮回调实时余量。"""
        self._on_update = on_update
        self._on_alert = on_alert
        self.running = True
        self._ema = None            # 重启时重置平滑状态
        self._emer_count = 0
        self._thread = threading.Thread(target=self._safety_loop, daemon=True)
        self._thread.start()
        log.info("安全监控循环已启动")

    def stop(self):
        self.running = False

    def _alert(self, level: str, msg: str):
        log.log(logging.WARNING if level == "warn" else logging.INFO, msg)
        if self._on_alert:
            try:
                self._on_alert(level, msg)
            except Exception:
                pass

    # ---- v2.3 自我干扰修复：EMA 平滑 + 自我贡献扣除 + 死区判定 ----

    def _ema_update(self, avg_cpu, peak_cpu, gpu, mem, load):
        """指数滑动平均，alpha=0.3，滤掉读数抖动。"""
        if self._ema is None:
            self._ema = {"avg": avg_cpu, "peak": peak_cpu, "gpu": gpu,
                         "mem": mem, "load": load}
        else:
            a = 0.3
            e = self._ema
            e["avg"] += a * (avg_cpu - e["avg"])
            e["peak"] += a * (peak_cpu - e["peak"])
            e["gpu"] += a * (gpu - e["gpu"])
            e["mem"] += a * (mem - e["mem"])
            e["load"] += a * (load - e["load"])
        return self._ema

    def _self_contribution(self):
        """返回 (cpu_self, gpu_self)：暖炉当前在各资源上制造的负载估算 [0,1]。

        CPU：多进程占空比 → 每核约等于 cpu.factor
        GPU：CUDA 张量占空比 → 约等于 gpu.factor
        乘 0.9 保守扣除（宁可多扣，绝不少扣，免得把外部负载误当自己的）。
        """
        if self._engine is None or not self._engine.running:
            return 0.0, 0.0
        return (self._engine.cpu.factor * 0.9,
                self._engine.gpu.factor * 0.9)

    def _safety_loop(self):
        """每 2 秒感知一次，算出外部瓶颈余量，交给引擎调节强度。

        核心：把暖炉自己制造的负载从读数中扣除，只对外部负载做保护；
        EMA 平滑 + 死区 + 连续确认，杜绝自我震荡和微小波动误判。
        """
        while self.running:
            current_cpu = self._cpu_reader()
            current_gpu = self._read_gpu_usage()
            current_mem = self._read_mem()
            current_load = self._read_load_avg()

            avg_cpu = sum(current_cpu) / len(current_cpu) if current_cpu else 0.0
            peak_core = max(current_cpu) if current_cpu else 0.0
            ema = self._ema_update(avg_cpu, peak_core, current_gpu,
                                   current_mem, current_load)

            cpu_self, gpu_self = self._self_contribution()

            # ---- 外部负载（扣除暖炉自己的贡献）----
            ext_cpu = max(0.0, ema["avg"] - cpu_self)
            ext_gpu = (max(0.0, ema["gpu"] - gpu_self)
                       if self.caps.gpu_available else 0.0)
            ext_mem = ema["mem"]                       # 内存不做扣除
            # 负载：我们的烧载进程约占 cpu_self * cores 个 runnable 槽
            ext_load = max(0.0, ema["load"]
                           - cpu_self * self.caps.cpu_logical_cores * 0.85)

            m = self.margin
            real_cpu_headroom = max(0.0, 1.0 - ext_cpu - m["cpu"])
            real_gpu_headroom = max(0.0, 1.0 - ext_gpu - m["gpu"])
            real_mem_headroom = max(0.0, 1.0 - ext_mem - m["mem"])
            real_load_headroom = max(0.0, self.max_load - ext_load)

            bottleneck = min(
                real_cpu_headroom,
                real_gpu_headroom if self.caps.gpu_available else 1.0,
                real_mem_headroom,
                max(0.0, real_load_headroom / max(1, self.caps.cpu_logical_cores)),
            )

            # ---- 绝对保护（外部真的撑爆了，不管谁烧的）----
            # CPU：外部负载本身就接近爆满才紧急（自己烧满是预期，不算）
            cpu_external_critical = ext_cpu + m["cpu"] > 0.92
            # 总 CPU 峰值超限：即使算不出自我贡献，总峰值 > 0.97 也说明系统在卡死边缘
            # （这兜底能接住"峰值远超平均"的单核满载突发）
            cpu_total_peak_critical = ema["peak"] > self.ABS_PEAK_CAP
            # 内存：超上限就危险（OOM 不分你我）
            mem_critical = ema["mem"] > self.ABS_MEM_CAP
            abs_critical = cpu_external_critical or cpu_total_peak_critical or mem_critical

            # ---- 判定动作（死区 + 连续确认）----
            if abs_critical:
                # 外部真的爆满（CPU 外部超 92% 或 内存超 95%）：
                # 先降载，连续确认 3 次（≈6s）才紧急降载，防抖动误判
                self._emer_count += 1
                if self._emer_count >= self.EMER_CONFIRM:
                    action = "emergency"
                else:
                    action = "ramp_down"
            elif bottleneck < 0.12:
                # 余量偏低：直接慢慢降载（不用确认）
                self._emer_count = 0
                action = "ramp_down"
            elif bottleneck > 0.45:
                # 余量充裕：慢慢升载
                self._emer_count = 0
                action = "ramp_up"
            else:
                # 死区 0.12~0.45：保持现状，杜绝微小波动震荡
                self._emer_count = 0
                action = "hold"

            if self._on_update:
                try:
                    self._on_update({
                        "cpu_headroom": real_cpu_headroom,
                        "gpu_headroom": real_gpu_headroom,
                        "mem_headroom": real_mem_headroom,
                        "load_headroom": real_load_headroom,
                        "bottleneck": bottleneck,
                        "action": action,
                        "current_cpu": peak_core,
                        "current_gpu": current_gpu,
                        "current_mem": current_mem,
                        "current_load": current_load,
                        "external_cpu": ext_cpu,
                        "external_gpu": ext_gpu,
                    })
                except Exception:
                    pass

            time.sleep(2.0)