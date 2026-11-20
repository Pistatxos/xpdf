# Limitaciones conocidas y privacidad

[← Volver al README](../README.md)

## Lo que xpdf no edita (a propósito)

xpdf prefiere no dejarte editar algo a estropearlo. Te lo dice al hacer clic en:

- párrafos con **fórmulas matemáticas**;
- fuentes de **símbolos** (Wingdings…) o fuentes **Type3**;
- texto **vertical** y la capa invisible de **OCR** de los escaneados.

## Otras limitaciones

- Si un párrafo crece, se amplía hacia abajo y puede montarse sobre el siguiente
  (como en Acrobat). xpdf lo avisa y puedes ensanchar la caja con los tiradores.
- Las escrituras que necesitan motor de forma (árabe, hebreo, índicas) usan una
  ruta de composición menos fiel al original.
- La vista previa mientras escribes usa el motor de texto del sistema: los cortes
  de línea pueden variar algo respecto al resultado final.
- En macOS solo hay versión para Apple Silicon (M1 o posterior).

## Privacidad

xpdf funciona sin conexión. La única petición a Internet es la comprobación de
actualizaciones (a GitHub), como mucho una vez al día. Sin telemetría y sin
cuentas.

Los errores se guardan solo en tu equipo:

- Windows: `%LOCALAPPDATA%\xpdf\xpdf.log`
- macOS: `~/Library/Application Support/xpdf/xpdf.log`
