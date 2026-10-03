#!/bin/sh
# Build "CLI Proxy.app" and install it in /Applications (or the folder given as $1).
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
dest="${1:-/Applications}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
app="$work/CLI Proxy.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$work/icon.iconset"

swiftc -O -target arm64-apple-macos14 -o "$app/Contents/MacOS/CLIProxy" "$here/main.swift" -framework AppKit -framework WebKit

swift "$here/icon.swift" "$work/icon.png"
for px in 16 32 128 256 512; do
  sips -z $px $px "$work/icon.png" --out "$work/icon.iconset/icon_${px}x${px}.png" >/dev/null
  sips -z $((px * 2)) $((px * 2)) "$work/icon.png" --out "$work/icon.iconset/icon_${px}x${px}@2x.png" >/dev/null
done
iconutil -c icns "$work/icon.iconset" -o "$app/Contents/Resources/AppIcon.icns"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>CLI Proxy</string>
  <key>CFBundleDisplayName</key><string>CLI Proxy</string>
  <key>CFBundleIdentifier</key><string>io.tstout.cliproxy-dashboard</string>
  <key>CFBundleExecutable</key><string>CLIProxy</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

codesign --force --sign - "$app"
rm -rf "$dest/CLI Proxy.app"
cp -R "$app" "$dest/"
printf 'Installed %s/CLI Proxy.app\n' "$dest"
