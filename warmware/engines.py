"""
产热引擎
========
CPU 引擎：用多进程绕开 GIL，每个逻辑核一个长驻进程跑浮点运算，持续压满 CPU。
GPU 引擎：多后端覆盖 —— NVIDIA（CUDA/torch）、AMD/Intel/摩尔线程（Vulkan compute）。
           哪个能用就用哪个，缺库/缺驱动自动回退，绝不报错打扰用户。

关键：每个引擎都受一个"目标负载因子 factor∈[0,1]"控制，
安全守护实时算出 factor 并调高/调低，引擎只负责听话地执行。
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("warmware.engines")


# 多进程烧载的 worker 函数必须位于模块顶层（可被 pickle）


def _cpu_burn_worker(factor_proxy, idx):
    """长驻子进程：内部按共享 factor 做烧/歇占空比，实现持续高负载。

    用 Manager 的共享 factor（-1 表示停止），避免反复创建进程的 spawn 开销。
    每个进程独立烧浮点，绕开 GIL，能真正压满对应核心。
    """
    x = 1.0000001
    try:
        while True:
            f = factor_proxy[idx]
            if f < 0:  # 停止信号
                break
            if f <= 0.02:
                time.sleep(0.05)
                continue
            # 烧一段再歇一段，构成占空比；烧的比例越高，负载越满
            end = time.monotonic() + 0.1 * f
            while time.monotonic() < end:
                x = (x * x) % 1.0000003
                x = math.sqrt(x)
                x = (x * 1.0000001) % 1.0000003
            time.sleep(0.1 * (1.0 - f))
    except KeyboardInterrupt:
        pass
    except Exception:
        pass


class CpuHeater:
    """
    多进程 CPU 烧载引擎（绕开 GIL）。
    每个逻辑核一个长驻进程，进程内做烧/歇占空比，能持续压满 CPU。
    """

    def __init__(self, cores: int):
        self.cores = max(1, cores)
        self.running = False
        self.factor = 0.0
        self._procs: list[subprocess.Popen] = [] if os.name == "nt" else None
        # Windows 用 spawn（无 fork），Linux/macOS 用 fork
        self._ctx = None
        self._factor_proxy = None
        self._manager = None
        self._error = ""

    def start(self):
        if self.running:
            return
        import multiprocessing as mp
        started: list = []
        try:
            # Linux/macOS 用 fork 最省开销且无 spawn 的导入问题
            if os.name == "nt":
                self._ctx = mp.get_context("spawn")
            else:
                self._ctx = mp.get_context("fork")
            self._manager = self._ctx.Manager()
            self._factor_proxy = self._manager.list([self.factor] * self.cores)
            self._procs = [
                self._ctx.Process(target=_cpu_burn_worker,
                                  args=(self._factor_proxy, i), daemon=True)
                for i in range(self.cores)
            ]
            for p in self._procs:
                p.start()
                started.append(p)     # 记录已成功启动的进程，失败可回滚
            self.running = True
            # 后台线程每 0.3 秒把 self.factor 同步给所有 worker
            threading.Thread(target=self._sync_loop, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            self._error = f"多进程烧载启动失败：{e}"
            log.warning(self._error)
            self.running = False
            # 回滚：清掉已启动的进程与 Manager，避免泄漏
            for p in started:
                try:
                    p.terminate()
                except Exception:
                    pass
            self._procs = []
            if self._manager is not None:
                try:
                    self._manager.shutdown()
                except Exception:
                    pass
                self._manager = None
                self._factor_proxy = None

    def _sync_loop(self):
        while self.running:
            proxy = self._factor_proxy
            if proxy is not None:
                try:
                    for i in range(self.cores):
                        proxy[i] = self.factor
                except Exception:
                    # stop() 已 shutdown Manager，静默退出
                    break
            time.sleep(0.2)

    def stop(self):
        self.running = False
        try:
            if self._factor_proxy is not None:
                for i in range(self.cores):
                    self._factor_proxy[i] = -1.0
            if self._procs:
                for p in self._procs:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                self._procs = []
            if self._manager is not None:
                try:
                    self._manager.shutdown()
                except Exception:
                    pass
                self._manager = None
        except Exception:
            pass

    def set_factor(self, f: float):
        self.factor = max(0.0, min(1.0, f))


class GpuHeater:
    """
    GPU 产热引擎（多后端）。
    覆盖：NVIDIA（CUDA/torch）、AMD/Intel/摩尔线程（Vulkan compute）、
    以及无可用 GPU 时的回退。
    原则：哪个能用就用哪个，绝不因缺库/缺驱动而报错打扰用户。
    """

    def __init__(self, caps):
        self.caps = caps
        self.running = False
        self.factor = 0.0
        self.available = False
        self.backend = ""          # "cuda" | "vulkan" | ""
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._error = ""
        self._vulkan_proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    def _detect_backend(self) -> str:
        """按优先级探测可用 GPU 后端。"""
        # 1) NVIDIA → CUDA（torch）
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        # 2) 其他厂商 → Vulkan（AMD/Intel/摩尔线程）
        if shutil.which("vulkaninfo") or os.path.isdir("/usr/share/vulkan"):
            return "vulkan"
        return ""

    def try_init(self) -> bool:
        """尝试初始化某可用后端。失败返回 False 并记录原因。"""
        if not self.caps.gpu_available:
            self._error = "未检测到可用 GPU"
            return False
        self.backend = self._detect_backend()
        if not self.backend:
            self._error = "未找到可用的 GPU 计算后端（CUDA/Vulkan），仅 CPU 产热"
            log.info(self._error)
            return False
        self.available = True
        log.info("GPU 引擎就绪：%s 后端", self.backend)
        return True

    def start(self):
        if not self.available or self.running:
            return
        self.running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._burn, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._stop_event.set()
        if self._vulkan_proc:
            try:
                self._vulkan_proc.terminate()
            except Exception:
                pass
            self._vulkan_proc = None

    def set_factor(self, f: float):
        self.factor = max(0.0, min(1.0, f))

    def current_utilization(self) -> float:
        """返回 GPU 当前占用估算 [0,1]，供安全循环做负载保护与自我扣除。

        - cuda：优先 nvidia-smi 实测；失败回退为 factor（自我上报）
        - vulkan：返回 factor——Vulkan 在烧就约占 factor（无跨厂商遥测接口，
          由引擎自己上报，避免控制器"盲烧"）
        - 未运行 / 无后端：0
        """
        if not self.running or not self.available:
            return 0.0
        if self.backend == "cuda":
            try:
                import subprocess
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                )
                if r.returncode == 0 and r.stdout.strip():
                    return max(0.0, min(1.0, float(r.stdout.strip()) / 100.0))
            except Exception:
                pass
            return self.factor   # 实测失败，回退自我上报
        if self.backend == "vulkan":
            return self.factor
        return 0.0

    # ------------------------------------------------------------------
    def _burn(self):
        if self.backend == "cuda":
            self._burn_cuda()
        elif self.backend == "vulkan":
            self._burn_vulkan()
        else:
            self.running = False

    def _burn_cuda(self):
        try:
            import torch
            device = "cuda"
            mem_gb = self.caps.gpu_vram_gb or 6.0
            # 保守估算：留出至少一半显存余量，避免 OOM 崩溃
            size = max(256, min(int(math.sqrt(mem_gb * 0.45 * 1e9 / 4)), 16384))
            a = torch.randn(size, size, device=device)
            b = torch.randn(size, size, device=device)
            # 复用一块缓冲区，用 in-place 运算压低显存峰值（防小显存卡 OOM）
            out = torch.randn(size, size, device=device)
            while not self._stop_event.is_set():
                f = self.factor
                if f <= 0.001:
                    time.sleep(0.1)
                    continue
                end = time.monotonic() + 0.2 * f
                while time.monotonic() < end:
                    try:
                        torch.matmul(a, b, out=out)
                        a.mul_(0.99999).add_(out, alpha=1e-9)
                        torch.cuda.synchronize()
                    except RuntimeError as e:
                        # OOM：缩小尺寸后重试一次，避免 GPU 产热直接失效
                        if "out of memory" in str(e).lower():
                            size = max(128, size // 2)
                            a = torch.randn(size, size, device=device)
                            b = torch.randn(size, size, device=device)
                            out = torch.randn(size, size, device=device)
                            torch.cuda.empty_cache()
                            self._error = f"CUDA 显存不足，已缩至 {size}²"
                            log.warning(self._error)
                        else:
                            raise
                time.sleep(0.2 * (1.0 - f))
        except Exception as e:  # noqa: BLE001
            self._error = f"CUDA 烧载循环中断：{e}"
            log.warning(self._error)
            self.available = False
            self.running = False

    def _burn_vulkan(self):
        """
        Vulkan 计算后端：跑一段反复的 compute shader 来压榨 GPU。
        优先用系统自带的 vulkan 计算示例（vkcube 之类）；否则用一小段
        内嵌 C 程序（若有编译器）编译出计算 runner；都没有则回退到 CPU。
        """
        try:
            import shutil
            runner = self._find_vulkan_runner()
            if not runner:
                self._error = "Vulkan 可用但无计算 runner，回退到 CPU 产热"
                log.warning(self._error)
                self.available = False
                self.running = False
                return
            while not self._stop_event.is_set():
                f = self.factor
                if f <= 0.001:
                    time.sleep(0.1)
                    continue
                end = time.monotonic() + 0.3 * f
                # 循环启动 runner（每次压一小段再放一放，形成占空比）
                while time.monotonic() < end:
                    if self._vulkan_proc is None or self._vulkan_proc.poll() is not None:
                        self._vulkan_proc = subprocess.Popen(
                            runner, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
                        )
                    time.sleep(0.03)
                if self._vulkan_proc and self._vulkan_proc.poll() is None:
                    self._vulkan_proc.terminate()
                    self._vulkan_proc = None
                time.sleep(0.3 * (1.0 - f))
        except Exception as e:  # noqa: BLE001
            self._error = f"Vulkan 烧载循环中断：{e}"
            log.warning(self._error)
            # 清理还存活的 runner 进程，避免泄漏
            if self._vulkan_proc is not None:
                try:
                    if self._vulkan_proc.poll() is None:
                        self._vulkan_proc.terminate()
                except Exception:
                    pass
                self._vulkan_proc = None
            self.available = False
            self.running = False

    def _find_vulkan_runner(self) -> Optional[list]:
        """找一个能持续压 GPU 的可执行文件（优先长驻型，避免一闪即逝的工具）。

        覆盖 AMD/Intel/摩尔线程的常见情况：
          - vkcube / vulkan demoscene（长驻绘制，持续占用 GPU）
          - 程序自带的 runner（打包时随 exe 放置，Windows/Linux 通用）
        都找不到则回退 CPU，不报错。
        """
        # 1) 程序自带的可执行 runner（打包时放在 exe 同目录）
        base = os.path.dirname(getattr(sys, "_MEIPASS", os.path.abspath(".")))
        for name in ("warmware_vk.exe", "warmware_vk", "vk_heat.exe"):
            cand = os.path.join(base, name)
            if os.path.isfile(cand):
                return [cand]
        # 2) 系统自带的持续型 GPU 演示（vkcube 等长驻）
        for cand in ("vkcube", "vkcube-xcb", "vkcube-wayland"):
            p = shutil.which(cand)
            if p:
                return [p]
        # 3) 退而求其次：vulkaninfo（一次性，效果差，仅作最后兜底）
        p = shutil.which("vulkaninfo")
        if p:
            return [p]
        return None


class HybridEngine:
    """
    混合引擎：把 CPU/GPU 产热与安全守护绑在一起。
    推荐模式、启动、因子调节都走这里，GUI 只跟它打交道。
    """

    MODES = {
        "cpu_only": "仅 CPU",
        "cpu_only_low": "仅 CPU（低功率）",
        "gpu_primary": "GPU 为主",
        "hybrid": "双引擎",
        "hybrid_low": "双引擎（低功率）",
        "aggressive": "激进模式（烧得猛）",
        "off": "关闭",
    }

    def __init__(self, caps, guardian):
        self.caps = caps
        self.guardian = guardian
        self.cpu = CpuHeater(caps.cpu_logical_cores)
        self.gpu = GpuHeater(caps)
        self.mode = "off"
        self.last_recommend = "off"
        self.running = False

    def detect_recommend(self) -> str:
        """根据硬件与基准线推荐最佳模式。"""
        caps, g = self.caps, self.guardian
        if not caps.ok:
            return "off"

        cpu_avail = g.cpu_available
        gpu_avail = g.gpu_available
        base = g.baseline

        # 无 GPU → 只能 CPU
        if not caps.gpu_available:
            busy = bool(base) and base["load_avg"] > caps.cpu_logical_cores * 0.7
            return "cpu_only_low" if busy else "cpu_only"

        # 有 GPU 但 GPU 已很忙 → 只用 CPU
        if gpu_avail < 0.20:
            return "cpu_only"

        # GPU 有余量但 CPU 很忙 → 主要用 GPU
        if cpu_avail < 0.30 and gpu_avail > 0.40:
            return "gpu_primary"

        # 都有余量 → 双引擎
        if cpu_avail > 0.40 and gpu_avail > 0.30:
            return "hybrid"

        return "hybrid_low"

    def apply_mode(self, mode: str):
        """应用某模式（设置各引擎的初始 factor）。"""
        self.mode = mode
        self.cpu.set_factor(0.0)
        self.gpu.set_factor(0.0)
        if mode in ("cpu_only", "cpu_only_low"):
            self.cpu.set_factor(0.7 if mode == "cpu_only" else 0.4)
        elif mode == "gpu_primary":
            self.cpu.set_factor(0.2)
            self.gpu.set_factor(0.8)
        elif mode in ("hybrid", "hybrid_low"):
            self.cpu.set_factor(0.8 if mode == "hybrid" else 0.5)
            self.gpu.set_factor(0.6 if mode == "hybrid" else 0.3)
        elif mode == "aggressive":
            # 激进模式：CPU 和 GPU 都拉满（仍受安全守护实时压住）
            self.cpu.set_factor(0.98)
            self.gpu.set_factor(0.95)

    def start(self, mode: str):
        """启动指定模式。返回 (ok, msg)。"""
        if not self.caps.ok:
            return False, "此设备连 CPU 产热都不安全，暖炉无法启动。"
        # 尝试初始化 GPU（可用才开 GPU 线程）
        if self.gpu.try_init():
            self.gpu.start()
        else:
            self.gpu.stop()
        self.cpu.start()
        self.apply_mode(mode)
        self.running = True
        return True, f"暖炉已启动：{self.MODES.get(mode, mode)}"

    def stop(self):
        self.cpu.stop()
        self.gpu.stop()
        self.mode = "off"
        self.running = False

    def adjust_from_safety(self, bottleneck: float, action: str = "hold"):
        """安全守护每轮回调这里，根据 action 限速调节 factor。

        v2.3：改为 action 驱动 + 死区 + 速率受限（每次最多 ±0.15），
        彻底消除自我震荡。
        """
        if not self.running:
            return
        m = self.mode
        # 模式对应的满火力基准
        if m == "aggressive":
            base_cpu, base_gpu = 1.0, 0.98
        else:
            base_cpu = {"cpu_only": 0.9, "cpu_only_low": 0.5,
                        "gpu_primary": 0.2, "hybrid": 0.9, "hybrid_low": 0.6}.get(m, 0.5)
            base_gpu = {"gpu_primary": 0.9, "hybrid": 0.7, "hybrid_low": 0.4}.get(m, 0.0)
        floor_cpu = 0.15 if m == "aggressive" else 0.05
        floor_gpu = 0.20 if m == "aggressive" else 0.0

        step = 0.15  # 每 2 秒最多变化 0.15，平滑无跳变
        if action == "emergency":
            # 紧急：直接到最低（保留一点热量避免完全熄灭）
            self.cpu.set_factor(floor_cpu)
            self.gpu.set_factor(floor_gpu)
            self.guardian._alert("warn", "⚠️ 系统资源紧张，紧急降载")
        elif action == "ramp_up":
            self.cpu.set_factor(min(base_cpu, self.cpu.factor + step))
            self.gpu.set_factor(min(base_gpu, self.gpu.factor + step))
        elif action == "ramp_down":
            self.cpu.set_factor(max(floor_cpu, self.cpu.factor - step))
            self.gpu.set_factor(max(floor_gpu, self.gpu.factor - step))
        # action == "hold" 不动

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "mode_label": self.MODES.get(self.mode, self.mode),
            "running": self.running,
            "cpu_factor": self.cpu.factor,
            "gpu_factor": self.gpu.factor,
            "gpu_available": self.gpu.available,
            "gpu_error": self.gpu._error,
            "recommend": self.MODES.get(self.last_recommend, self.last_recommend),
        }