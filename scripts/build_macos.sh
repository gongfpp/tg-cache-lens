#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
DIST_DIR="${DIST_DIR:-build}"
WORK_DIR="${WORK_DIR:-.local/pyinstaller}"
uv sync --frozen --extra gui
uv run --extra gui python scripts/make_icon.py
uv run --extra gui pyinstaller --workpath "$WORK_DIR" --distpath "$DIST_DIR" --noconfirm --clean --onedir --name tg-cache-lens --exclude-module PySide6 scripts/entry.py
uv run --extra gui pyinstaller --workpath "$WORK_DIR" --distpath "$DIST_DIR" --noconfirm --clean --windowed --onedir --name 'Cache Lens' --osx-bundle-identifier io.github.gongfpp.tg-cache-lens --icon assets/icon.icns --collect-data tg_cache_lens scripts/gui_entry.py
for target in "$DIST_DIR/tg-cache-lens" "$DIST_DIR/Cache Lens.app/Contents/Resources"; do
  cp LICENSE README.md "$target/"
  cp -R docs "$target/"
  mkdir -p "$target/assets"
  cp assets/icon.svg "$target/assets/"
  for image in assets/*.png; do [ ! -f "$image" ] || cp "$image" "$target/assets/"; done
  uv run --extra gui python scripts/collect_licenses.py "$target/LICENSES"
done
cat > "$DIST_DIR/tg-cache-lens/Launch.command" <<'LAUNCH'
#!/bin/zsh
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
./tg-cache-lens "$@"
LAUNCH
chmod +x "$DIST_DIR/tg-cache-lens/Launch.command"
# The CLI and independent window are sibling downloads in the release folder.
cat > "$DIST_DIR/tg-cache-lens/GUI.command" <<'LAUNCH'
#!/bin/zsh
/usr/bin/open "${0:A:h}/../Cache Lens.app"
LAUNCH
chmod +x "$DIST_DIR/tg-cache-lens/GUI.command"
codesign --force --deep --sign - "$DIST_DIR/Cache Lens.app"
