"""Rutas de la aplicación: recursos incluidos y carpeta de datos del usuario."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "xpdf"


def assets_dir() -> Path:
    """Carpeta `assets/`, tanto en desarrollo como empaquetado con PyInstaller."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle is not None:
        return Path(bundle) / "assets"
    # src/xpdf/paths.py -> raíz del repo
    return Path(__file__).resolve().parents[2] / "assets"


def user_data_dir() -> Path:
    """Carpeta de datos por usuario según el SO. Se crea si no existe."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_file() -> Path:
    return user_data_dir() / "xpdf.log"
