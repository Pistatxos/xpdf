# -*- mode: python ; coding: utf-8 -*-
# Especificación de PyInstaller para xpdf (Windows y macOS).
#
# Decisiones para no parecer malware a los antivirus:
# - modo carpeta («onedir»), nunca un .exe único que se autoextrae;
# - sin UPX (los ejecutables comprimidos disparan heurísticas);
# - metadatos de versión en el .exe de Windows (packaging/windows/version_info.txt,
#   generado por build.py).
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))
from xpdf import __version__  # noqa: E402

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

a = Analysis(
    [str(ROOT / "src" / "xpdf" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "assets"), "assets")],
    hiddenimports=["PySide6.QtSvg"],
    excludes=[
        "tkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtTest",
        "PySide6.QtNetwork",
        "pymupdf.layout",
    ],
    noarchive=False,
)
# Fuera lo que xpdf no usa y viene arrastrado por dependencias (~100 MB):
# - los modelos de IA de pymupdf.layout (análisis de maquetación);
# - módulos de Qt que cargan algunos plugins (QtPdf por el plugin de imágenes
#   «pdf», QtQuick/QtQml...) y QtNetwork (las descargas usan urllib).
_DROP = ("QtQuick", "QtQml", "QtPdf", "qpdf", "QtNetwork", "QtVirtualKeyboard", "QtOpenGL", "Qt3D")


def _keep(entry) -> bool:
    name = entry[0].replace("\\", "/")
    return "pymupdf/layout" not in name and not any(x in name for x in _DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="xpdf",
    console=False,
    upx=False,
    icon=str(ROOT / "assets" / "icons" / ("app.icns" if IS_MAC else "app.ico")),
    version=str(ROOT / "packaging" / "windows" / "version_info.txt") if IS_WIN else None,
    argv_emulation=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="xpdf", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="xpdf.app",
        icon=str(ROOT / "assets" / "icons" / "app.icns"),
        bundle_identifier="io.github.xpdf",
        version=__version__,
        info_plist={
            "CFBundleName": "xpdf",
            "CFBundleDisplayName": "xpdf",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "LSMinimumSystemVersion": "12.0",
            # «Abrir con xpdf» para PDF (sin convertirse en la app por defecto).
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Documento PDF",
                    "CFBundleTypeRole": "Editor",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
    )
