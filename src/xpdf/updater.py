"""Actualizaciones desde GitHub Releases (sin Qt: testeable).

Flujo: `fetch_latest` consulta la última versión publicada; si es más nueva,
`download` baja el paquete de esta plataforma y `verify` comprueba su SHA-256
contra el `SHA256SUMS.txt` publicado junto a él. Después `apply_update` lanza la
instalación en segundo plano y la app debe cerrarse:

- Windows: el instalador de Inno Setup en modo silencioso; cierra xpdf si sigue
  abierto y lo vuelve a abrir al terminar (`/RELAUNCH=1`).
- macOS: un pequeño script espera a que xpdf se cierre, sustituye la `.app` y
  la vuelve a abrir.

Solo tiene sentido en la versión empaquetada (`sys.frozen`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi

from xpdf import GITHUB_REPO, __version__
from xpdf.core.errors import XpdfError

log = logging.getLogger(__name__)

API = "https://api.github.com/repos/{repo}/releases/latest"
SUMS_NAME = "SHA256SUMS.txt"
USER_AGENT = f"xpdf-updater/{__version__}"


class UpdateError(XpdfError):
    """Fallo al buscar o instalar una actualización (mensaje para el usuario)."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    asset_name: str
    asset_url: str
    asset_size: int
    sums_url: str
    notes: str
    page_url: str


# ------------------------------------------------------------------ versiones
def parse_version(text: str) -> tuple[tuple[int, ...], bool]:
    """«v1.2.3» -> ((1, 2, 3), False); «1.3.0-beta.2» -> ((1, 3, 0), True)."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)(.*)$", text.strip())
    if not m:
        raise ValueError(f"versión no válida: {text!r}")
    nums = tuple(int(x) for x in m.group(1).split("."))
    while len(nums) < 3:
        nums += (0,)
    return nums, bool(m.group(2).strip())


def is_newer(candidate: str, current: str = __version__) -> bool:
    """¿`candidate` es posterior a `current`? Las preliminares cuentan como anteriores."""
    (c, c_pre), (v, v_pre) = parse_version(candidate), parse_version(current)
    if c != v:
        return c > v
    return v_pre and not c_pre


def asset_name(version: str, system: str | None = None) -> str | None:
    """Nombre del paquete de `version` para esta plataforma (None si no hay)."""
    system = system or sys.platform
    v = version.lstrip("v")
    if system == "win32":
        return f"xpdf-{v}-windows-x64-setup.exe"
    if system == "darwin":
        return f"xpdf-{v}-macos-arm64.zip"
    return None


def can_self_update() -> bool:
    """Solo la versión empaquetada se actualiza sola (en desarrollo se usa git)."""
    if not getattr(sys, "frozen", False):
        return False
    if sys.platform == "darwin":
        return platform.machine() == "arm64" and installed_bundle() is not None
    return sys.platform == "win32"


# ------------------------------------------------------------------ red
def _context() -> ssl.SSLContext:
    # Certificados de certifi: la app empaquetada no depende de los del sistema.
    return ssl.create_default_context(cafile=certifi.where())


def _get(url: str, timeout: float = 15) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_context()) as resp:
        return bytes(resp.read())


def fetch_latest(repo: str = GITHUB_REPO, getter: Callable[[str], bytes] = _get) -> Release | None:
    """Última versión publicada con paquete para esta plataforma, o None."""
    if "TU-USUARIO" in repo:
        return None  # repositorio aún sin configurar
    try:
        data = json.loads(getter(API.format(repo=repo)))
    except Exception as exc:
        log.info("No se pudo consultar la última versión: %s", exc)
        raise UpdateError("No se pudo comprobar si hay actualizaciones. ¿Hay conexión a Internet?") from exc
    tag = str(data.get("tag_name", ""))
    try:
        parse_version(tag)
    except ValueError:
        return None
    name = asset_name(tag)
    assets = {a.get("name"): a for a in data.get("assets", [])}
    if name is None or name not in assets or SUMS_NAME not in assets:
        return None
    a = assets[name]
    return Release(
        version=tag.lstrip("v"),
        asset_name=name,
        asset_url=str(a["browser_download_url"]),
        asset_size=int(a.get("size", 0)),
        sums_url=str(assets[SUMS_NAME]["browser_download_url"]),
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or ""),
    )


def _open(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=60, context=_context())


def download(
    rel: Release,
    dest_dir: Path,
    progress: Callable[[int, int], None] | None = None,
    getter: Callable[[str], bytes] = _get,
    opener: Callable[[str], Any] = _open,
) -> Path:
    """Descarga el paquete y verifica su SHA-256. Devuelve la ruta del archivo.

    `getter`/`opener` se pueden sustituir en los tests.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / rel.asset_name
    try:
        sums = getter(rel.sums_url).decode("utf-8", "replace")
        with opener(rel.asset_url) as resp, open(target, "wb") as out:
            total = int(resp.headers.get("Content-Length") or rel.asset_size or 0)
            done = 0
            while chunk := resp.read(256 * 1024):
                out.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except Exception as exc:
        target.unlink(missing_ok=True)
        log.info("Descarga fallida: %s", exc)
        raise UpdateError("No se pudo descargar la actualización. ¿Hay conexión a Internet?") from exc
    if not verify(target, sums, rel.asset_name):
        target.unlink(missing_ok=True)
        raise UpdateError(
            "La descarga no coincide con la huella publicada (SHA-256). "
            "Por seguridad no se instalará."
        )
    return target


def verify(path: Path, sums_text: str, name: str) -> bool:
    """Comprueba `path` contra la línea de `name` en un SHA256SUMS.txt."""
    expected = None
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            expected = parts[0].lower()
            break
    if expected is None:
        return False
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest() == expected


# ---------------------------------------------------------------- instalación
def installed_bundle() -> Path | None:
    """macOS: ruta de xpdf.app si se ejecuta empaquetada."""
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def apply_update(package: Path) -> None:
    """Lanza la instalación en segundo plano. Después, la app debe cerrarse."""
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(  # noqa: S603 - instalador verificado por SHA-256
            [str(package), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"],
            creationflags=flags,
            close_fds=True,
        )
        return
    if sys.platform == "darwin":
        bundle = installed_bundle()
        if bundle is None:
            raise UpdateError("No se encuentra la aplicación instalada.")
        script = _macos_script(package, bundle, os.getpid())
        subprocess.Popen(["/bin/sh", str(script)], start_new_session=True, close_fds=True)  # noqa: S603
        return
    raise UpdateError("La actualización automática no está disponible en este sistema.")


def _macos_script(package: Path, bundle: Path, pid: int) -> Path:
    """Script que espera a que xpdf termine, sustituye la .app y la reabre."""
    q = shlex.quote
    work = Path(tempfile.mkdtemp(prefix="xpdf-update-"))
    script = work / "update.sh"
    script.write_text(
        f"""#!/bin/sh
# Actualización de xpdf: generado por la propia app.
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
set -e
/usr/bin/ditto -x -k {q(str(package))} {q(str(work / "new"))}
NEW="$(/usr/bin/find {q(str(work / "new"))} -maxdepth 1 -name '*.app' | head -n 1)"
[ -n "$NEW" ] || exit 1
/usr/bin/xattr -dr com.apple.quarantine "$NEW" 2>/dev/null || true
OLD={q(str(bundle))}
mv "$OLD" "$OLD.old-$$"
if mv "$NEW" "$OLD"; then rm -rf "$OLD.old-$$"; else mv "$OLD.old-$$" "$OLD"; fi
/usr/bin/open "$OLD"
rm -rf {q(str(work))}
""",
        encoding="utf-8",
    )
    return script


def download_dir() -> Path:
    return Path(tempfile.gettempdir()) / "xpdf-updates"
