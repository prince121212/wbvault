#!/bin/bash
# 编译 WBVault 菜单栏 App 到项目目录：tools/wbvault/WBVault.app
# 同时把 wbvault 源码打进 Resources/runtime-src，App 启动时会用它校准运行时副本，
# 避免「App 传了新参数、副本还是旧代码」导致的 unrecognized arguments。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
APP="$SRC/WBVault.app"
BIN="$APP/Contents/MacOS/WBVault"
RES="$APP/Contents/Resources"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "缺少 swiftc，请先执行：xcode-select --install" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES"

swiftc -O -swift-version 5 \
  -target arm64-apple-macos13.0 \
  -framework AppKit \
  -framework ApplicationServices \
  -o "$BIN" "$HERE/main.swift" "$HERE/AutoRetry.swift"

cp "$HERE/Info.plist" "$APP/Contents/Info.plist"

# 内置运行时源码 + 指纹（算法与 wbvault_lib/agent.py 的 source_stamp 一致）
mkdir -p "$RES/runtime-src"
cp "$SRC/wbvault.py" "$RES/runtime-src/"
cp -R "$SRC/wbvault_lib" "$RES/runtime-src/"
find "$RES/runtime-src" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
STAMP="$(python3 - "$SRC" <<'PY'
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
digest = hashlib.sha1()
for path in sorted(root.rglob("*.py")):
    if "__pycache__" in path.parts or "tests" in path.parts:
        continue
    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest()[:16])
PY
)"
printf '%s\n' "$STAMP" > "$RES/runtime-src/.stamp"

chmod +x "$BIN"
# 自签名（ad-hoc）+ 固定的「设计要求」。
#
# 不写 -r 的话，codesign 会把设计要求定成 cdhash，也就是钉死这一份二进制；
# 每次重新编译 cdhash 都变，系统就认为「这不是同一个 App」，
# 用户在「隐私与安全性 → 辅助功能」里勾的那一下会失效。
# 显式写成 identifier 之后，授权只认 bundle id，重编译不用再勾一次。
codesign --force --sign - \
  --identifier ai.workbuddy.wbvault.menu \
  -r='designated => identifier "ai.workbuddy.wbvault.menu"' \
  "$APP" >/dev/null 2>&1 || codesign --force --sign - "$APP" >/dev/null 2>&1 || true

echo "已构建：${APP}（运行时源码指纹 ${STAMP}）"
