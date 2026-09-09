@echo off
REM ============================================================
REM  WarmWare 暖炉电脑 —— Windows 一键打包成 exe
REM  用法：双击运行 或 命令行执行 build_exe.bat
REM  产物在 dist\ 目录下：WarmWare.exe（单个可执行文件）
REM ============================================================
setlocal

echo [1/3] 安装依赖...
pip install -r requirements.txt
if errorlevel 1 goto :err

echo [2/3] 安装打包工具...
pip install pyinstaller

echo [3/3] 打包中...
pyinstaller --noconfirm --clean ^
    --name WarmWare ^
    --onefile ^
    --windowed ^
    --icon warmware.ico ^
    main.py

if errorlevel 1 goto :err

echo.
echo ============================================================
echo  打包完成！exe 文件在 dist\WarmWare.exe
echo  复制到任意 Windows 电脑双击即可玩。
echo ============================================================
goto :eof

:err
echo.
echo ❌ 打包失败，请检查上方错误信息。
exit /b 1