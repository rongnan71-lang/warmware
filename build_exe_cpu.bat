@echo off
REM ============================================================
REM  WarmWare 暖炉电脑 —— 精简版 CPU-only 打包成小体积 exe
REM  适合：大多数只用 CPU 取暖的用户（无独显或不想带 GPU 库）
REM  产物在 dist\ 目录下：WarmWare_cpu.exe（约 20MB，体积小）
REM  想用 GPU 产热：请用 build_exe.bat（产物含 torch，约 1GB）
REM ============================================================
setlocal

echo [1/3] 安装依赖...
pip install psutil
if errorlevel 1 goto :err

echo [2/3] 安装打包工具...
pip install pyinstaller

echo [3/3] 打包中（排除 torch 等重型库）...
pyinstaller --noconfirm --clean ^
    --name WarmWare_cpu ^
    --onefile ^
    --windowed ^
    --icon warmware.ico ^
    --exclude-module torch ^
    --exclude-module numpy ^
    --exclude-module scipy ^
    --exclude-module pandas ^
    --exclude-module pyarrow ^
    --exclude-module bitsandbytes ^
    --exclude-module cudnn ^
    main.py

if errorlevel 1 goto :err

echo.
echo ============================================================
echo  打包完成！精简版在 dist\WarmWare_cpu.exe
echo  复制到任意 Windows 电脑双击即可玩（仅 CPU 产热）。
echo ============================================================
goto :eof

:err
echo.
echo ❌ 打包失败，请检查上方错误信息。
exit /b 1