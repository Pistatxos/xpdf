# Limitaciones conocidas y privacidad

[← Volver al README](../README.md)

## Lo que xpdf no edita (a propósito)

xpdf prefiere no dejarte editar algo a estropearlo. Te lo dice al hacer clic en:

- párrafos con **fórmulas matemáticas**;
- fuentes de **símbolos** (Wingdings…) o fuentes **Type3**;
- texto **vertical** y la capa invisible de **OCR** de los escaneados;
- imágenes que forman parte de un **grupo** del PDF (por ejemplo, una página
  importada dentro de otra).

## Otras limitaciones

- Si un párrafo crece, se amplía hacia abajo y puede montarse sobre el siguiente
  (como en Acrobat). xpdf lo avisa y puedes ensanchar la caja con los tiradores.
- Las escrituras que necesitan motor de forma (árabe, hebreo, índicas) usan una
  ruta de composición menos fiel al original.
- La vista previa mientras escribes usa el motor de texto del sistema: los cortes
  de línea pueden variar algo respecto al resultado final.
- Al reemplazar una imagen, la nueva queda por encima del resto de elementos
  (usa **Detrás del texto** si hace falta).
- Al cambiar el orden de capas de una imagen que el PDF recortaba con una
  máscara o un trazado, puede perder ese recorte.
- El texto no rodea a las imágenes (un PDF no tiene flujo de texto): si hace
  falta hueco, estrecha el párrafo con los tiradores.
- En macOS solo hay versión para Apple Silicon (M1 o posterior).
- Firma digital: solo certificados en archivo (`.pfx`/`.p12`); no DNI
  electrónico ni tarjetas. La firma no lleva sello de tiempo de un servidor
  externo (la fecha es la del ordenador) y xpdf solo comprueba que el documento
  no se ha alterado, no si el certificado es de una entidad de confianza.

## Privacidad

xpdf funciona sin conexión (también al firmar y comprobar firmas). La única petición a Internet es la comprobación de
actualizaciones (a GitHub), como mucho una vez al día. Sin telemetría y sin
cuentas.

Los errores se guardan solo en tu equipo:

- Windows: `%LOCALAPPDATA%\xpdf\xpdf.log`
- macOS: `~/Library/Application Support/xpdf/xpdf.log`
