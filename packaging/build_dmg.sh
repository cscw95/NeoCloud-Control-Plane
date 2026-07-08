#!/usr/bin/env bash
# NeoCloud OS 맥 설치 파일(DMG) 빌드 — 프로젝트 루트에서 실행:
#   bash packaging/build_dmg.sh
# 산출물: dist/NeoCloudOS-<버전>-arm64.dmg
set -euo pipefail
cd "$(dirname "$0")/.."
VER=$(.venv/bin/python -c "from app import __version__; print(__version__)")

.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name "NeoCloud OS" \
  -p "$PWD" \
  --icon "$PWD/packaging/neocloud.icns" \
  --add-data "$PWD/app/static:app/static" \
  --collect-submodules app \
  --hidden-import uvicorn.logging \
  --hidden-import uvicorn.loops.auto \
  --hidden-import uvicorn.protocols.http.auto \
  --hidden-import uvicorn.protocols.websockets.auto \
  --hidden-import uvicorn.lifespan.on \
  --osx-bundle-identifier com.skt.neocloud-os \
  --distpath dist --workpath build --specpath packaging \
  packaging/launcher.py

rm -rf build/dmg-stage && mkdir -p build/dmg-stage
cp -R "dist/NeoCloud OS.app" build/dmg-stage/
ln -s /Applications build/dmg-stage/Applications
cp "packaging/설치 안내.txt" build/dmg-stage/ 2>/dev/null || true
hdiutil create -volname "NeoCloud OS" -srcfolder build/dmg-stage -ov -format UDZO \
  "dist/NeoCloudOS-${VER}-arm64.dmg"
echo "✓ dist/NeoCloudOS-${VER}-arm64.dmg"
