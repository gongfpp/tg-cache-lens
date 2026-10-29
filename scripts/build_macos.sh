#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv run pyinstaller --noconfirm --clean --onedir --name tg-cache-lens --collect-all textual scripts/entry.py
cp LICENSE README.md dist/tg-cache-lens/
mkdir -p dist/tg-cache-lens/assets
cp assets/*.svg assets/*.png dist/tg-cache-lens/assets/
uv run python scripts/collect_licenses.py
cat > dist/tg-cache-lens/Launch.command <<'LAUNCH'
#!/bin/zsh
cd -- "${0:A:h}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
./tg-cache-lens "$@"
LAUNCH
chmod +x dist/tg-cache-lens/Launch.command
