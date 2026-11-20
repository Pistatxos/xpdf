# Windows: avisos de seguridad y bloqueos

[← Volver al README](../README.md)

xpdf todavía **no está firmado digitalmente**. Windows trata con desconfianza
los programas sin firma, aunque sean seguros. Según cómo esté configurado tu
equipo puedes encontrarte una de estas dos situaciones.

## «Windows protegió su PC» (SmartScreen)

Aparece si descargas el instalador con el navegador. Pulsa **Más información →
Ejecutar de todas formas**. Con la línea de PowerShell del README no suele
aparecer.

## Bloqueado: error 4551 (Control inteligente de aplicaciones)

Windows 11 incluye el **Control inteligente de aplicaciones** (*Smart App
Control*). Cuando está activado, bloquea cualquier programa sin firma, venga del
navegador, de PowerShell o del actualizador de xpdf. Verás un mensaje como:

> Una directiva de Control de aplicaciones bloqueó este archivo (error 4551)

No se puede autorizar un programa concreto: solo se puede desactivar para todo
el equipo.

**Para desactivarlo:**

1. Menú Inicio → escribe **Seguridad de Windows** y ábrelo.
2. **Control de aplicaciones y del navegador**.
3. **Configuración del control inteligente de aplicaciones** → **Desactivado**.

Después vuelve a pegar la línea de instalación en PowerShell.

> ⚠️ En muchas versiones de Windows 11 **no se puede volver a activar** sin
> reinstalar Windows. El antivirus (Microsoft Defender) sigue funcionando igual.
> Si prefieres no desactivarlo, espera a que xpdf esté firmado.

## Firma digital

Está previsto firmar xpdf con [SignPath Foundation](https://signpath.org), que
firma gratis proyectos de código abierto. Con la firma, Windows dejará instalarlo
y actualizarlo sin estos avisos.

## Comprobar la descarga

Cada versión publica `SHA256SUMS.txt` en
[Releases](https://github.com/Pistatxos/xpdf/releases/latest). Los instaladores
de una línea y el actualizador verifican la huella antes de instalar y se niegan
si no coincide.
