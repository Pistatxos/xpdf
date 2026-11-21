# Cómo firmar documentos

xpdf tiene dos tipos de firma:

- **Firma con certificado digital**: la que tiene validez legal. Usa tu certificado (el de la FNMT, el que te da la gestoría, etc.) y deja en el documento un sello con tu nombre y la fecha. Si alguien modifica el PDF después, se nota.
- **Firma dibujada (rúbrica)**: tu firma manuscrita, como una imagen. Sirve para documentos que solo hay que «firmar a mano»; no tiene la validez de un certificado.

## 1. Preparar tus firmas (solo la primera vez)

Abre **Firma → Gestionar firmas…**

**Añadir tu certificado digital**

1. Pulsa **Añadir certificado…** y elige el archivo `.pfx` o `.p12` de tu certificado.
2. Escribe la contraseña del certificado. Solo se usa para comprobar que es correcto: **xpdf no guarda la contraseña**, la pedirá cada vez que firmes.
3. Ponle un nombre para reconocerlo (p. ej. «Mario» o «Mireia»).

Puedes añadir varios certificados: si en el ordenador firman varias personas, cada una elige el suyo al firmar.

> xpdf recuerda dónde está el archivo del certificado, no lo copia. Si lo mueves o lo borras, tendrás que volver a añadirlo.

**Crear tu firma dibujada**

- **Dibujar…**: firma con el ratón, el panel táctil o un lápiz.
- **Importar imagen…**: una foto o escaneo de tu firma sobre papel blanco. xpdf quita el fondo y la recorta.

## 2. Firmar con certificado

1. Abre el PDF.
2. Pulsa el botón **Firmar** de la barra de herramientas (o el menú **Firma**) y elige tu certificado.
3. Arrastra sobre la página para dibujar el recuadro donde irá la firma (o haz un clic para un recuadro de tamaño estándar).
4. Escribe la contraseña del certificado. Si quieres, elige también tu **rúbrica** para que aparezca junto al sello, y añade el motivo o el lugar.
5. Pulsa **Firmar…** y elige dónde guardar. Por defecto se guarda como «*nombre* (firmado).pdf»: **el original no se modifica**.

Al terminar, xpdf abre el documento firmado.

## 3. Añadir la firma dibujada

1. Pulsa **Firmar** y elige tu firma en «Añadir firma dibujada».
2. Haz clic donde quieras ponerla, ajústala con las esquinas y pulsa **Intro**.
3. Guarda el documento.

## Documentos ya firmados

- Cuando abres un PDF firmado, xpdf lo indica abajo, en la barra de estado. En **Firma → Firmas del documento…** ves quién lo firmó y si se ha modificado después.
- **Puedes añadir tu firma** a un documento que ya firmó otra persona: las firmas anteriores siguen siendo válidas.
- **Modificar** un documento firmado (editar texto, imágenes o páginas) invalida sus firmas. xpdf te avisa antes de hacerlo.

## Preguntas frecuentes

**¿Dónde está mi certificado?** Si lo instalaste en Windows y no tienes el archivo, puedes exportarlo: busca «Administrar certificados de usuario» → Personal → Certificados → clic derecho sobre el tuyo → Todas las tareas → Exportar → «Exportar la clave privada» → formato `.pfx`. Pon una contraseña y guárdalo en un lugar seguro.

**¿.pfx o .p12?** Son el mismo formato con distinta extensión. xpdf admite los dos.

**¿Y el DNI electrónico o tarjetas?** No se admiten: hace falta el certificado en archivo.

**¿Cómo compruebo que la firma es válida?** xpdf comprueba que el documento no se ha alterado. Para comprobar además que el certificado es de una entidad de confianza, abre el PDF con Adobe Acrobat Reader o usa el servicio [VALIDe](https://valide.redsara.es) del Gobierno.

**La contraseña es incorrecta.** Es la que pusiste al exportar o descargar el certificado (no la del ordenador). Si no la recuerdas, tendrás que volver a exportarlo o solicitarlo.
