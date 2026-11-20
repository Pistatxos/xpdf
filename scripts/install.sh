#!/bin/sh
# Instalador de xpdf para macOS (Apple Silicon). Uso:
#
#   curl -fsSL https://raw.githubusercontent.com/TU-USUARIO/xpdf/main/scripts/install.sh | sh
#
# Descarga la última versión desde GitHub Releases, comprueba su SHA-256 contra
# SHA256SUMS.txt y copia xpdf.app a /Applications (o a ~/Applications si no hay
# permiso). Volver a ejecutarlo reinstala o actualiza.
# Opcional: XPDF_VERSION=0.3.0 para una versión concreta.
set -eu

REPO="${XPDF_REPO:-TU-USUARIO/xpdf}"

fail() { printf 'xpdf: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "este instalador es para macOS. En Windows usa install.ps1."
[ "$(uname -m)" = "arm64" ] || fail "por ahora solo hay versión para Mac con Apple Silicon (M1 o posterior)."

if [ -n "${XPDF_VERSION:-}" ]; then
    TAG="v${XPDF_VERSION#v}"
else
    printf 'Buscando la última versión de xpdf...\n'
    TAG=$(curl -fsSL -H "User-Agent: xpdf-installer" "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$TAG" ] || fail "no se pudo consultar la última versión."
fi
VERSION="${TAG#v}"
NAME="xpdf-$VERSION-macos-arm64.zip"
BASE="${XPDF_DOWNLOAD_BASE:-https://github.com/$REPO/releases/download/$TAG}"  # override solo para pruebas

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

printf 'Descargando xpdf %s...\n' "$VERSION"
curl -fsSL -o "$TMP/$NAME" "$BASE/$NAME" || fail "no se pudo descargar $NAME."
curl -fsSL -o "$TMP/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt" || fail "no se pudo descargar SHA256SUMS.txt."

EXPECTED=$(awk -v n="$NAME" '{ f=$NF; sub(/^\*/, "", f); if (f == n) { print tolower($1); exit } }' "$TMP/SHA256SUMS.txt")
[ -n "$EXPECTED" ] || fail "SHA256SUMS.txt no incluye $NAME."
ACTUAL=$(shasum -a 256 "$TMP/$NAME" | awk '{ print tolower($1) }')
[ "$EXPECTED" = "$ACTUAL" ] || fail "la descarga no coincide con la huella publicada (SHA-256). No se instala nada."
printf 'Huella SHA-256 verificada.\n'

DEST="${XPDF_INSTALL_DIR:-/Applications}"  # override solo para pruebas
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }

/usr/bin/ditto -x -k "$TMP/$NAME" "$TMP/app"
[ -d "$TMP/app/xpdf.app" ] || fail "el paquete no contiene xpdf.app."
/usr/bin/xattr -dr com.apple.quarantine "$TMP/app/xpdf.app" 2>/dev/null || true

[ -n "${XPDF_NO_OPEN:-}" ] || osascript -e 'tell application "xpdf" to quit' >/dev/null 2>&1 || true
rm -rf "$DEST/xpdf.app"
mv "$TMP/app/xpdf.app" "$DEST/xpdf.app"

printf 'xpdf %s instalado en %s/xpdf.app\n' "$VERSION" "$DEST"
[ -n "${XPDF_NO_OPEN:-}" ] || open "$DEST/xpdf.app"
