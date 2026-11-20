# Funciones y atajos

[← Volver al README](../README.md)

## Editar texto existente

Elige **Editar texto** (menú *Edición* o tecla `E`), haz clic en un párrafo y
escribe. xpdf detecta párrafos, alineación (izquierda, centro, derecha,
justificado), sangría e interlineado, y vuelve a componer el texto en el mismo
ancho.

- Negrita, cursiva, tamaño y color por tramos (barra de formato).
- Arrastra los **tiradores azules** de los lados para ensanchar o estrechar la
  caja. Una línea suelta crece sola en horizontal hasta el margen.
- `Esc` cancela; `Ctrl+Intro` o un clic fuera confirma.

### La misma letra que el original

xpdf escribe con *el propio objeto de fuente del PDF* y con sus métricas exactas
(anchos, espaciado y kerning medidos en el documento): las palabras que no
cambias quedan donde estaban y el archivo no crece.

Los PDFs solo guardan las letras que usan. Si escribes una que el documento no
contenía (por ejemplo una «ñ»), solo esa letra sale de otra fuente parecida
(la misma instalada en el sistema, o Liberation, incluida con xpdf), y la barra
de estado te dice cuáles.

## Añadir texto

Elige **Añadir texto** (`T`), haz clic en la página y escribe. En la barra de
formato eliges tipo de letra (sans, serif, mono), tamaño, color, negrita y
cursiva. Los tiradores fijan un ancho a partir del cual el texto pasa de línea.

## Páginas

- Selecciona una o varias miniaturas (`Ctrl/⌘` o `Mayús` + clic).
- **Reordenar:** arrastra las miniaturas; una línea indica dónde caerán.
- **Girar** y **borrar:** iconos de la barra, menú *Páginas* o clic derecho.
- Se conservan formularios, marcadores y enlaces internos.

## Abrir, guardar y reducir tamaño

- Arrastra un PDF a la ventana para abrirlo.
- **Guardar** nunca sobrescribe el original sin preguntar la primera vez, y no
  engorda el archivo.
- **Archivo → Reducir tamaño…** guarda una copia más ligera: sin pérdida,
  equilibrado (imágenes a 150 ppp) o máxima reducción (96 ppp).
- **Deshacer y rehacer** funcionan con todo, incluso después de guardar.

## Atajos

| Acción | Atajo |
|---|---|
| Abrir · Guardar · Guardar como | Ctrl/⌘ + O · S · Mayús+S |
| Deshacer · Rehacer | Ctrl/⌘ + Z · Mayús+Z |
| Seleccionar · Editar texto · Añadir texto | V · E · T |
| Negrita · Cursiva (editando) | Ctrl/⌘ + B · I |
| Confirmar · Cancelar edición | Ctrl+Intro (o clic fuera) · Esc |
| Girar página izquierda · derecha | Ctrl/⌘ + L · R |
| Borrar páginas | Supr (en las miniaturas) |
| Zoom · Ajustar al ancho · Tamaño real | Ctrl/⌘ + / − · Ctrl/⌘ + 0 · Ctrl/⌘ + 1 |
