#!/usr/bin/env bash
# Constrói o bundle Linux (PyInstaller) + AppImage portátil (DermaMetric-x86_64.AppImage)
# e o pacote tar.gz (DermaMetric-linux-x86_64.tar.gz) a partir da raiz do repositório.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ARCH="$(uname -m)"
BUILD_DIR="$ROOT_DIR/build/linux-appimage"
APPDIR="$BUILD_DIR/DermaMetric.AppDir"
DIST_LINUX="$ROOT_DIR/dist/linux"
APPIMAGE_OUT="$DIST_LINUX/DermaMetric-${ARCH}.AppImage"
TARBALL_OUT="$DIST_LINUX/DermaMetric-linux-${ARCH}.tar.gz"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

echo "==> [1/4] Gerando bundle PyInstaller..."
"$PYTHON_BIN" -m PyInstaller packaging/dermametric.spec --noconfirm --clean

if [[ ! -x "$ROOT_DIR/dist/DermaMetric/DermaMetric" ]]; then
  echo "Erro: binário dist/DermaMetric/DermaMetric não foi gerado." >&2
  exit 1
fi

mkdir -p "$DIST_LINUX"

echo "==> [2/4] Gerando pacote tar.gz universal..."
tar -czf "$TARBALL_OUT" -C "$ROOT_DIR/dist" DermaMetric

echo "==> [3/4] Montando estrutura AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/share/applications"

cp -a "$ROOT_DIR/dist/DermaMetric/." "$APPDIR/usr/bin/"
cp "$ROOT_DIR/frontend/favicon.svg" "$APPDIR/dermametric.svg"
cp "$ROOT_DIR/frontend/favicon.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/dermametric.svg"
cp "$ROOT_DIR/packaging/dermametric.desktop" "$APPDIR/dermametric.desktop"
cp "$ROOT_DIR/packaging/dermametric.desktop" "$APPDIR/usr/share/applications/dermametric.desktop"

cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
exec "${HERE}/usr/bin/DermaMetric" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> [4/4] Empacotando AppImage (${ARCH})..."
APPIMAGETOOL_BIN="${APPIMAGETOOL_BIN:-$(command -v appimagetool || true)}"
if [[ -z "$APPIMAGETOOL_BIN" ]]; then
  TOOLS_DIR="$BUILD_DIR/tools"
  mkdir -p "$TOOLS_DIR"
  APPIMAGETOOL_BIN="$TOOLS_DIR/appimagetool-${ARCH}.AppImage"
  if [[ ! -x "$APPIMAGETOOL_BIN" ]]; then
    URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    echo "Baixando appimagetool de ${URL}..."
    curl -fsSL "$URL" -o "$APPIMAGETOOL_BIN"
    chmod +x "$APPIMAGETOOL_BIN"
  fi
fi

ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 "$APPIMAGETOOL_BIN" --no-appstream "$APPDIR" "$APPIMAGE_OUT"
chmod +x "$APPIMAGE_OUT"

echo "Concluído:"
echo "  - AppImage: $APPIMAGE_OUT"
echo "  - Tarball : $TARBALL_OUT"
