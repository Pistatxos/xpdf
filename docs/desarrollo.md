# Desarrollo, compilar y publicar

[← Volver al README](../README.md)

## Ejecutar en desarrollo

Requiere [uv](https://docs.astral.sh/uv/).

```sh
uv sync                  # entorno e instalación de dependencias
uv run xpdf [archivo]    # ejecutar
uv run pytest            # tests (incluye un corpus de PDFs reales)
uv run mypy              # tipos (modo estricto)
```

## Estructura

```
src/xpdf/
├── core/            lógica PDF (PyMuPDF), sin Qt — cubierta por tests
│   ├── text.py      detección de párrafos y edición
│   ├── layout.py    composición propia: partición, justificado, escritura
│   ├── pdffonts.py  escritura con las fuentes originales del PDF
│   ├── fonts.py     elección de fuentes y comprobación de glifos
│   ├── pages.py     borrar, girar y reordenar páginas
│   ├── images.py    añadir, mover, reemplazar y borrar imágenes
│   ├── contentstream.py  localiza el dibujo exacto de cada imagen
│   ├── optimize.py  «Reducir tamaño…»
│   ├── signatures.py  firmas: certificados (pyHanko) y rúbricas
│   └── editing.py   fachada para la interfaz
├── ui/              PySide6 — nunca llama a PyMuPDF directamente
└── updater.py       actualizaciones desde GitHub Releases
tests/samples/       corpus de PDFs reales (Word, LaTeX, formularios, OCR…)
packaging/           PyInstaller (xpdf.spec) e instalador de Windows (Inno Setup)
scripts/             instaladores de una línea (install.ps1, install.sh)
tools/               icono de la app, configurar el repositorio y publicar versiones
```

La interfaz y el núcleo están separados: todo lo que toca el PDF vive en `core/`
y se prueba sin interfaz gráfica.

## Compilar el ejecutable

```sh
uv run python build.py
```

Genera en `dist/` el paquete del sistema actual (PyInstaller en modo carpeta,
sin UPX) y lo comprueba con `--selftest`:

- Windows: `xpdf-<versión>-windows-x64-setup.exe` (necesita
  [Inno Setup 6](https://jrsoftware.org/isinfo.php)).
- macOS: `xpdf-<versión>-macos-arm64.zip`.

No se puede compilar para Windows desde macOS ni al revés: para eso está la
integración continua.

## Publicar una versión

```sh
uv run python tools/release.py 0.3.4      # sube la versión, pasa tests, commit y etiqueta
git push && git push origin v0.3.4        # GitHub Actions compila y publica
```

El workflow `release.yml` compila en Windows y macOS, ejecuta la autocomprobación
sobre cada ejecutable y publica en GitHub Releases junto con `SHA256SUMS.txt`.
Las copias instaladas lo detectan al arrancar.

El CI (`ci.yml`) pasa los tests en Linux, macOS y Windows en cada subida; si un
test falla en GitHub, aparece como anotación pública de la ejecución.

## Icono de la aplicación

`uv run python tools/make_icon.py` regenera `assets/icons/app.*` con la
tipografía Inter Black (en `tools/fonts/`, solo para generarlo).

## Licencias y créditos

- xpdf: [AGPL-3.0-or-later](../LICENSE).
- [PyMuPDF](https://pymupdf.readthedocs.io) (AGPL) y
  [PySide6](https://doc.qt.io/qtforpython-6/) (LGPL).
- Firma digital PAdES con [pyHanko](https://github.com/MatthiasValvekens/pyHanko) (MIT).
- Fuentes de respaldo [Liberation](https://github.com/liberationfonts/liberation-fonts)
  (SIL OFL 1.1), incluidas en `assets/fonts/`.
- Iconos [Lucide](https://lucide.dev) (ISC), en `assets/icons/`.
- Tipografía del logo [Inter](https://rsms.me/inter/) (SIL OFL 1.1), solo en
  `tools/fonts/`.
- PDFs de prueba: origen y licencia en
  [`tests/samples/README.md`](../tests/samples/README.md).
