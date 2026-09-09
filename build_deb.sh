#!/usr/bin/env bash
# ============================================================
#  WarmWare 暖炉电脑 —— Linux 打包成 .deb 安装包
#  用法：bash build_deb.sh
#  产物在 dist/ 目录下：warmware_*.deb
#  安装：sudo apt install ./warmware_*.deb
#  依赖：debhelper, dpkg-dev, python3, python3-pip, python3-tk
# ============================================================
set -euo pipefail

APP_NAME="warmware"
VERSION="2.3.0"
DIST="dist"
PKG="pkg"
BINDIR="$PKG/usr/bin"
ICONDIR="$PKG/usr/share/icons/hicolor/256x256/apps"
APPDIR="$PKG/usr/share/applications"
DEBINFO="$PKG/DEBIAN"

echo "[1/4] 准备目录..."
rm -rf "$PKG" "$DIST"
mkdir -p "$BINDIR" "$ICONDIR" "$APPDIR" "$DEBINFO"

echo "[2/4] 安装依赖..."
pip3 install -r requirements.txt

echo "[3/4] 用 PyInstaller 打包成单个可执行文件..."
pip3 install pyinstaller
pyinstaller --noconfirm --clean \
    --name "$APP_NAME" \
    --onefile \
    --windowed \
    --icon warmware.ico \
    main.py

cp "$DIST/$APP_NAME" "$BINDIR/$APP_NAME"
cp warmware.ico "$ICONDIR/warmware.png" 2>/dev/null || true

cat > "$APPDIR/warmware.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WarmWare 暖炉电脑
Comment=冬天拿电脑取暖
Exec=$APP_NAME
Icon=warmware
Terminal=false
Categories=Utility;
EOF

echo "[4/4] 生成 control 并构建 deb..."
cat > "$DEBINFO/control" <<EOF
Package: $APP_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3, python3-tk
Maintainer: WarmWare <noreply@example.com>
Description: 冬天电脑取暖器
 把电脑当小太阳用，自适应安全地让 CPU/GPU 产热取暖。
Homepage: https://example.com/warmware
EOF

dpkg-deb --build --root-owner-group "$PKG" "$DIST/${APP_NAME}_${VERSION}_all.deb" >/dev/null

echo
echo "============================================================"
echo " 打包完成！安装包在 dist/${APP_NAME}_${VERSION}_all.deb"
echo " 安装：sudo apt install ./dist/${APP_NAME}_${VERSION}_all.deb"
echo "============================================================"