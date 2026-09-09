#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WarmWare — 冬天电脑取暖器
=========================
把电脑当"小太阳"用：自适应安全地让 CPU/GPU 产热。
绝不把系统搞死；自动识别硬件能力；Windows / Linux 通用。

用法：
    python main.py
    （或打包成 exe 后直接双击运行）

打包 exe（Windows，需先 pip install pyinstaller）：
    build_exe.bat
"""

import logging
import sys

from warmware import APP_NAME, __version__


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("warmware")

    try:
        from warmware.detect import detect_hardware
        from warmware.safety import SafetyGuardian
        from warmware.engines import HybridEngine
        from warmware.gui import WarmWareGUI
    except Exception as e:  # noqa: BLE001
        print(f"❌ 模块加载失败：{e}")
        return 1

    print(f"🔥 {APP_NAME} v{__version__} 启动中…")
    print()

    # 1. 探测硬件
    print("[1/3] 探测硬件能力…")
    caps = detect_hardware()
    print(caps.summary())
    print()

    if not caps.ok:
        print("❌ 此设备无法安全产热，暖炉不启动。")
        return 0

    # 2. 安全守护（先只建对象，基准线在用户点火时才采样）
    guardian = SafetyGuardian(caps)
    engine = HybridEngine(caps, guardian)

    # 3. 启动 GUI（图形界面，不阻塞在此）
    log.info("启动 GUI…")
    app = WarmWareGUI(engine, guardian, caps, baseline=None)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        guardian.stop()
    return 0


if __name__ == "__main__":
    # 多进程 + 打包成 exe 时必须在主模块里调用 freeze_support()
    # 否则 Windows 下子进程会重复导入并执行主脚本
    from multiprocessing import freeze_support
    freeze_support()
    sys.exit(main())