#!/bin/bash
# 编译 WBVault 菜单栏 App 到项目目录：tools/wbvault/WBVault.app
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(cd "$HERE/.." && pwd)/WBVault.app"
BIN="$APP/Contents/MacOS/WBVault"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "缺少 swiftc，请先执行：xcode-select --install" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -framework AppKit \
  -o "$BIN" "$HERE/WBVault.swift"

cp "$HERE/Info.plist" "$APP/Contents/Info.plist"
chmod +x "$BIN"
codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "已构建：$APP"
