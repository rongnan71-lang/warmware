"""
硬件能力自动探测
================
不是所有电脑都有独显，不是所有 CPU 都支持睿频，不是所有设备都能安全产热。
启动时自动探测硬件，并据此决定产热策略与安全余量。

跨平台：Windows / Linux
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field


@dataclass
class HardwareCapabilities:
    """探测到的硬件能力快照。"""

    cpu_cores: int = 0
    cpu_logical_cores: int = 0
    cpu_name: str = ""
    gpu_available: bool = False
    gpu_backend: str = ""          # "cuda" | "opengl" | ""
    gpu_dedicated: bool = False    # 是否独显
    gpu_name: str = ""
    gpu_vram_gb: float = 0.0
    total_mem_gb: float = 0.0
    is_low_mem: bool = False       # 内存 < 2GB
    is_single_core: bool = False   # 逻辑核 <= 1
    os_name: str = ""
    ok: bool = False               # 是否至少能靠 CPU 产热

    def summary(self) -> str:
        """给用户看的友好摘要。"""
        lines = [
            f"系统: {self.os_name}",
            f"CPU: {self.cpu_name} ({self.cpu_cores}核/{self.cpu_logical_cores}线程)",
            f"内存: {self.total_mem_gb:.1f} GB",
        ]
        if self.gpu_available:
            dedi = "独显" if self.gpu_dedicated else "核显"
            lines.append(
                f"GPU: {self.gpu_name} ({dedi}/{self.gpu_backend}) "
                f"{self.gpu_vram_gb:.1f} GB"
            )
        else:
            lines.append("GPU: 未检测到可用加速设备 → 仅 CPU 产热")
        return "\n".join(lines)


def _run(cmd, timeout=6):
    """安全执行外部命令，返回 (returncode, stdout)。失败返回 (None, "")。"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        return result.returncode, result.stdout
    except Exception:
        return None, ""


def _detect_cpu() -> tuple[int, int, str]:
    """返回 (物理核数, 逻辑核数, 型号)。尽力而为，取不到就退回逻辑核。"""
    try:
        import psutil  # 懒加载，psutil 缺失也能跑（退化为 os 探测）
        cores = psutil.cpu_count(logical=False) or 0
        logical = psutil.cpu_count(logical=True) or 0
        # 型号：跨平台无统一接口，尽力从 /proc 读（Linux）
        name = ""
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
        return cores, logical, name
    except Exception:
        logical = os.cpu_count() or 1
        return 0, logical, ""


def _detect_nvidia_gpu(caps: HardwareCapabilities) -> bool:
    """通过 nvidia-smi 探测 NVIDIA 独显（Windows 与 Linux 通用）。"""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False
    rc, out = _run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if rc != 0 or not out.strip():
        return False
    try:
        name, mem = (p.strip() for p in out.strip().splitlines()[0].rsplit(",", 1))
    except Exception:
        return False
    caps.gpu_available = True
    caps.gpu_backend = "cuda"
    caps.gpu_dedicated = True
    caps.gpu_name = name
    try:
        # memory.total 形如 "8192 MiB"
        caps.gpu_vram_gb = float(mem.split()[0]) / 1024.0
    except Exception:
        caps.gpu_vram_gb = 0.0
    return True


def _detect_integrated_gpu(caps: HardwareCapabilities) -> bool:
    """Linux 下通过 DRM 子系统探测核显；Windows 靠 psutil/ACPI 尽力。"""
    if os.name == "nt":
        # Windows 通用显卡名：用 wmic（老系统）或 powershell
        try:
            rc, out = _run(["wmic", "path", "win32_VideoController", "get", "name"])
            if rc == 0:
                for line in out.splitlines():
                    line = line.strip()
                    if line and "Name" not in line:
                        caps.gpu_available = True
                        caps.gpu_backend = "opengl"
                        caps.gpu_dedicated = False
                        caps.gpu_name = line
                        return True
        except Exception:
            pass
        return False

    # Linux：/sys/class/drm
    drm_path = "/sys/class/drm/"
    if os.path.isdir(drm_path):
        try:
            for card in os.listdir(drm_path):
                if card.startswith("card") and "-" not in card:
                    vendor_path = os.path.join(drm_path, card, "device/vendor")
                    if os.path.exists(vendor_path):
                        caps.gpu_available = True
                        caps.gpu_backend = "opengl"
                        caps.gpu_dedicated = False
                        caps.gpu_name = f"GPU {card}"
                        return True
        except Exception:
            pass
    return False


def detect_hardware() -> HardwareCapabilities:
    """执行完整硬件探测，返回能力快照。"""
    caps = HardwareCapabilities()
    caps.os_name = f"{platform.system()} {platform.release()}"

    # CPU
    caps.cpu_cores, caps.cpu_logical_cores, caps.cpu_name = _detect_cpu()
    if not caps.cpu_name:
        caps.cpu_name = platform.processor() or "未知"

    # GPU：优先 NVIDIA 独显，其次核显
    _detect_nvidia_gpu(caps)
    if not caps.gpu_available:
        _detect_integrated_gpu(caps)

    # 内存
    try:
        import psutil
        mem = psutil.virtual_memory()
        caps.total_mem_gb = mem.total / (1024 ** 3)
    except Exception:
        # 无 psutil 时尽力用平台接口
        if os.name == "nt":
            rc, out = _run(["wmic", "computersystem", "get", "TotalPhysicalMemory"])
            for line in out.splitlines():
                if line.strip().isdigit():
                    caps.total_mem_gb = int(line.strip()) / (1024 ** 3)
        elif os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("memtotal"):
                        caps.total_mem_gb = int(line.split()[1]) / 1024 / 1024
                        break

    caps.is_low_mem = 0 < caps.total_mem_gb < 2.0
    caps.is_single_core = caps.cpu_logical_cores <= 1
    # CPU 总是可用的（至少有 1 个逻辑核就能产热）
    caps.ok = caps.cpu_logical_cores >= 1

    # 单核但没探测到 GPU 的设备，其实几乎没法安全产热，标记不 ok
    if caps.is_single_core and not caps.gpu_available:
        caps.ok = False

    return caps


def to_json(caps: HardwareCapabilities) -> str:
    """序列化为 JSON，便于 GUI 打包传递。"""
    return json.dumps(caps.__dict__, ensure_ascii=False)