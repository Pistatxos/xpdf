# Fuentes incluidas

Fuentes de reserva que usa `xpdf.core.fonts` cuando al editar texto no se puede
reutilizar la fuente incrustada en el PDF ni encontrar la misma en el sistema.

| Archivos | Versión | Licencia |
|---|---|---|
| `LiberationSans-{Regular,Bold,Italic,BoldItalic}.ttf` | 2.1.5 | SIL OFL 1.1 |
| `LiberationSerif-{Regular,Bold,Italic,BoldItalic}.ttf` | 2.1.5 | SIL OFL 1.1 |
| `LiberationMono-{Regular,Bold,Italic,BoldItalic}.ttf` | 2.1.5 | SIL OFL 1.1 |

- Origen: release oficial 2.1.5 de <https://github.com/liberationfonts/liberation-fonts/releases/tag/2.1.5>,
  archivo `liberation-fonts-ttf-2.1.5.tar.gz`
  (<https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz>,
  SHA-256 `7191c669bf38899f73a2094ed00f7b800553364f90e2637010a69c0e268f25d0`). Archivos sin modificar.
- Licencia: [LICENSE-Liberation.txt](LICENSE-Liberation.txt) (copia del `LICENSE` del paquete).
- Son métricamente compatibles con Arial/Helvetica (Sans), Times New Roman (Serif) y
  Courier New (Mono), lo que conserva el ajuste de línea en PDFs generados por Word.
- Cubren latín (incl. extendido), griego y cirílico.

No se incluye una fuente de amplia cobertura (Noto, etc.): `page.insert_htmlbox` de
PyMuPDF ya recurre automáticamente, carácter a carácter, a las fuentes de reserva
integradas en MuPDF (Droid Sans Fallback para CJK, Noto Naskh Arabic, Noto Serif
Devanagari, Noto Emoji, Noto Sans Symbols 2…) para los glifos que falten.
