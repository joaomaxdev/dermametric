#!/usr/bin/env bash
# Instalador e desinstalador amigável para Linux (sem exigência de sudo).
# - Modo 1 (padrão): baixa o AppImage oficial da última release do GitHub,
#   instala em ~/.local/bin/dermametric e cria atalhos no Menu e Área de Trabalho.
# - Modo 2 (--from-source / fallback): se a arquitetura não tiver AppImage pré-compilado
#   (ex.: ARM64/Raspberry Pi) ou se --from-source for informado, instala localmente
#   via ambiente virtual Python isolado em ~/.local/share/dermametric/app.
set -euo pipefail

REPO="joaomaxdev/dermametric"
APP_NAME="DermaMetric"
BIN_DIR="${HOME}/.local/bin"
BIN_PATH="${BIN_DIR}/dermametric"
DATA_BASE="${XDG_DATA_HOME:-${HOME}/.local/share}"
APP_HOME="${DATA_BASE}/dermametric/app"
ICON_DIR="${DATA_BASE}/icons/hicolor/scalable/apps"
APPS_DIR="${DATA_BASE}/applications"
DESKTOP_FILE="${APPS_DIR}/dermametric.desktop"

log()  { printf "\033[1;36m[DermaMetric]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[Aviso]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[Erro]\033[0m %s\n" "$*" >&2; }

uninstall_app() {
  log "Removendo binário e atalhos do DermaMetric..."
  rm -f "$BIN_PATH" "$DESKTOP_FILE" "${ICON_DIR}/dermametric.svg"
  rm -rf "$APP_HOME"
  if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    [[ -n "$DESKTOP_DIR" ]] && rm -f "${DESKTOP_DIR}/DermaMetric.desktop"
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  log "Aplicativo removido. Seus prontuários em ${DATA_BASE}/dermametric/data foram preservados por segurança."
  exit 0
}

MODE="auto"
LOCAL_APPIMAGE=""
for arg in "$@"; do
  case "$arg" in
    --uninstall) uninstall_app ;;
    --from-source) MODE="source" ;;
    --appimage=*) MODE="local"; LOCAL_APPIMAGE="${arg#*=}" ;;
    -h|--help)
      cat <<EOF
Uso: install-linux.sh [opções]

Opções:
  (sem argumentos)       Instala automaticamente a versão pronta (AppImage) ou via código-fonte
  --from-source          Força instalação local isolada via Python venv
  --appimage=/caminho    Instala a partir de um arquivo .AppImage já baixado
  --uninstall            Remove o programa e os atalhos (preserva os dados clínicos)
EOF
      exit 0
      ;;
  esac
done

mkdir -p "$BIN_DIR" "$ICON_DIR" "$APPS_DIR"

install_desktop_entries() {
  local icon_src="$1"
  cp "$icon_src" "${ICON_DIR}/dermametric.svg"

  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=DermaMetric
GenericName=Fotogrametria Clínica
Comment=Estação clínica local de fotogrametria, morfometria cutânea e laudos longitudinais
Exec=${BIN_PATH} %U
Icon=dermametric
Terminal=false
Categories=Science;MedicalSoftware;Education;
Keywords=dermatologia;fotogrametria;morfometria;clinica;laudo;
StartupNotify=true
EOF
  chmod +x "$DESKTOP_FILE"

  # Copia atalho também para a Área de Trabalho, se existir.
  if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    if [[ -n "$DESKTOP_DIR" && -d "$DESKTOP_DIR" && "$DESKTOP_DIR" != "$HOME" ]]; then
      cp "$DESKTOP_FILE" "${DESKTOP_DIR}/DermaMetric.desktop"
      chmod +x "${DESKTOP_DIR}/DermaMetric.desktop"
      command -v gio >/dev/null 2>&1 && gio set "${DESKTOP_DIR}/DermaMetric.desktop" metadata::trusted true >/dev/null 2>&1 || true
    fi
  fi

  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t "${DATA_BASE}/icons/hicolor" >/dev/null 2>&1 || true
}

install_from_appimage() {
  local src="$1"
  log "Instalando executável portátil em ${BIN_PATH}..."
  install -m 0755 "$src" "$BIN_PATH"

  TMP_ICON="$(mktemp --suffix=.svg)"
  if curl -fsSL "https://raw.githubusercontent.com/${REPO}/main/frontend/favicon.svg" -o "$TMP_ICON" 2>/dev/null; then
    install_desktop_entries "$TMP_ICON"
  fi
  rm -f "$TMP_ICON"
}

install_from_source() {
  log "Instalando DermaMetric em ambiente isolado (${APP_HOME})..."
  if ! command -v python3 >/dev/null 2>&1; then
    err "Python 3 não encontrado. Instale o pacote python3 da sua distribuição e tente novamente."
    exit 1
  fi

  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT

  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "https://github.com/${REPO}.git" "$TMP_DIR/repo"
  else
    curl -fsSL "https://github.com/${REPO}/archive/refs/heads/main.tar.gz" -o "$TMP_DIR/src.tar.gz"
    tar -xzf "$TMP_DIR/src.tar.gz" -C "$TMP_DIR"
    mv "$TMP_DIR"/dermametric-* "$TMP_DIR/repo"
  fi

  rm -rf "$APP_HOME"
  mkdir -p "$APP_HOME"
  cp -a "$TMP_DIR/repo/." "$APP_HOME/"

  log "Criando ambiente Python e instalando dependências..."
  python3 -m venv "${APP_HOME}/.venv"
  "${APP_HOME}/.venv/bin/pip" install --upgrade pip >/dev/null
  "${APP_HOME}/.venv/bin/pip" install -r "${APP_HOME}/requirements.txt"

  cat > "$BIN_PATH" <<EOF
#!/usr/bin/env bash
exec "${APP_HOME}/.venv/bin/python" "${APP_HOME}/desktop_launcher.py" "\$@"
EOF
  chmod +x "$BIN_PATH"

  install_desktop_entries "${APP_HOME}/frontend/favicon.svg"
}

ARCH="$(uname -m)"

if [[ "$MODE" == "local" ]]; then
  if [[ ! -f "$LOCAL_APPIMAGE" ]]; then
    err "Arquivo não encontrado: $LOCAL_APPIMAGE"
    exit 1
  fi
  install_from_appimage "$LOCAL_APPIMAGE"
elif [[ "$MODE" == "auto" && "$ARCH" == "x86_64" ]]; then
  ASSET_URL="https://github.com/${REPO}/releases/latest/download/DermaMetric-x86_64.AppImage"
  TMP_IMG="$(mktemp --suffix=.AppImage)"
  log "Buscando pacote pronto (AppImage x86_64) na última release..."
  if curl -fsSL "$ASSET_URL" -o "$TMP_IMG" 2>/dev/null && [[ -s "$TMP_IMG" ]]; then
    install_from_appimage "$TMP_IMG"
    rm -f "$TMP_IMG"
  else
    rm -f "$TMP_IMG"
    warn "AppImage pré-compilado ainda não publicado na release. Alternando para instalação automática via Python..."
    install_from_source
  fi
else
  install_from_source
fi

# Garante ~/.local/bin no PATH em shells futuros, se ainda não estiver.
if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
  for rc in "${HOME}/.bashrc" "${HOME}/.zshrc"; do
    if [[ -f "$rc" ]] && ! grep -q '\.local/bin' "$rc"; then
      printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    fi
  done
fi

log "Instalação concluída com sucesso!"
log "Abra pelo menu de aplicativos procurando por '${APP_NAME}' ou execute: ${BIN_PATH}"
