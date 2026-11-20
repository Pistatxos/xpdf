# xpdf

Editor de PDF de escritorio, minimalista, gratuito y de código abierto (AGPL-3.0).
Hecho con Python 3.12, PySide6 y PyMuPDF.

> Estado: **fase 2** — visualizar, editar texto existente, añadir texto,
> deshacer/rehacer, guardar (sin engordar el archivo) y «Reducir tamaño…».
> Páginas e imágenes llegan en las fases 3 y 4.

## Edición de texto

- **Editar texto** (E): pasa el ratón por un párrafo y haz clic. Se edita en su
  sitio con su fuente, tamaño, color y alineación. Ctrl/Cmd+B y +I para
  negrita/cursiva; la barra de formato cambia tamaño, color y alineación.
  Esc cancela; Ctrl+Intro o un clic fuera confirma.
  Arrastra los tiradores azules de los lados para ensanchar o estrechar la caja;
  una línea suelta crece sola en horizontal hasta el margen.
- **Añadir texto** (T): clic en cualquier punto de la página y escribe (los
  tiradores fijan un ancho a partir del cual el texto pasa de línea).
- Fuentes: se escribe con **la misma fuente del PDF** (su propio objeto de
  fuente, sin volver a incrustarla) todo carácter que el documento ya dibuja con
  ella. Solo los caracteres que no existen en esa fuente recortada usan otra: la
  misma instalada en el sistema o la más parecida incluida (Liberation). La
  barra de estado avisa de qué caracteres se han sustituido.

### Limitaciones conocidas

- No se editan (se avisa al hacer clic): párrafos con fórmulas matemáticas,
  fuentes de símbolos (Wingdings…) o Type3, texto vertical y la capa invisible
  de OCR de los escaneados.
- Si un párrafo crece, se amplía hacia abajo y puede montarse sobre el
  siguiente (como en Acrobat); xpdf lo avisa.
- Escrituras que necesitan motor de forma (árabe, hebreo, índicas) usan otro
  motor de composición, menos fiel al original.
- La vista previa del editor usa el motor de texto de Qt: los cortes de línea
  mientras escribes pueden diferir ligeramente del resultado final.
- Al añadir o sustituir con una fuente del sistema se incrusta completa;
  «Archivo → Reducir tamaño… → Sin pérdida» la recorta.

## Ejecutar en desarrollo

Requiere [uv](https://docs.astral.sh/uv/).

```sh
uv sync                  # crea .venv e instala dependencias
uv run xpdf              # abre la aplicación
uv run xpdf archivo.pdf  # abre directamente un PDF
uv run xpdf --debug      # log detallado también en la consola
```

## Tests y comprobación de tipos

```sh
uv run pytest
uv run mypy
```

## Estructura

```
src/xpdf/
├── main.py         arranque, manejo global de errores
├── core/           lógica PDF (PyMuPDF), sin Qt — cubierta por tests
│   ├── text.py     detección de párrafos y edición
│   ├── layout.py   composición propia (partición, justificado, escritura)
│   ├── fonts.py    elección de fuentes y comprobación de glifos
│   ├── pdffonts.py escritura con las fuentes originales del PDF
│   ├── optimize.py «Reducir tamaño…»
│   └── editing.py  fachada para la UI
└── ui/             interfaz PySide6 — nunca importa pymupdf
assets/icons/       iconos SVG (se tiñen según el tema claro/oscuro)
tests/              pytest sobre core/ + corpus de PDFs reales (tests/samples)
```

## Log

Los errores se registran en:

- macOS: `~/Library/Application Support/xpdf/xpdf.log`
- Windows: `%LOCALAPPDATA%\xpdf\xpdf.log`
- Linux: `~/.local/state/xpdf/xpdf.log`

## Licencia

AGPL-3.0-or-later. Ver [LICENSE](LICENSE). PyMuPDF se distribuye también bajo AGPL.
