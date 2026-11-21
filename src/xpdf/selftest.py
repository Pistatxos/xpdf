"""Autocomprobación de la app empaquetada (`xpdf --selftest=RUTA`).

La usa la integración continua sobre el ejecutable ya construido: verifica que
están los recursos (iconos, fuentes de respaldo, certificados) y que el núcleo
puede abrir, editar, añadir texto y guardar un PDF. Escribe el resultado en
RUTA (en Windows una app de ventana no tiene consola) y devuelve 0 si todo va bien.
"""

from __future__ import annotations

import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path


def run(report: Path) -> int:
    lines: list[str] = []
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and cond
        lines.append(f"{'OK ' if cond else 'FALLO'} {name}{': ' + detail if detail else ''}")

    try:
        import certifi
        import pymupdf

        from xpdf import __version__
        from xpdf.core.document import Document
        from xpdf.core.editing import NewTextStyle, Span, TextEditing
        from xpdf.core.history import History
        from xpdf.paths import assets_dir

        lines.append(f"xpdf {__version__} · PyMuPDF {pymupdf.__version__}")
        check("iconos", (assets_dir() / "icons" / "app.png").is_file())
        fonts = list((assets_dir() / "fonts").glob("Liberation*.ttf"))
        check("fuentes de respaldo", len(fonts) == 12, f"{len(fonts)} encontradas")
        check("certificados", Path(certifi.where()).is_file())

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "prueba.pdf"
            d = pymupdf.open()
            d.new_page().insert_htmlbox(
                pymupdf.Rect(72, 72, 400, 300),
                "<p>Texto original de prueba para xpdf.</p>",
                css="* {font-family: sans-serif; font-size: 12px}",
            )
            d.save(src)
            doc = Document.open(src)
            ed = TextEditing(doc)
            h = History()
            p = ed.paragraphs(0)[0]
            h.push(ed.edit_command(0, p.id, [Span("Texto editado: ñandú y €.", p.spans[0][1])]))
            h.push(ed.add_command(0, 72, 400, "Añadido", NewTextStyle("serif", 14)))
            out = Path(tmp) / "salida.pdf"
            doc.save_as(out)
            text = str(pymupdf.open(out)[0].get_text())
            check("editar texto", "Texto editado: ñandú y €." in text)
            check("añadir texto", "Añadido" in text)
            check("renderizar", doc.render_page(0, 1.0).width > 0)
            _check_signing(Path(tmp), out, check)
    except Exception:
        ok = False
        lines.append("FALLO excepción:\n" + traceback.format_exc())
    lines.append("RESULTADO: " + ("OK" if ok else "FALLO"))
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


def _check_signing(tmp: Path, pdf: Path, check: Callable[[str, bool, str], None]) -> None:
    """Firma con un certificado de prueba generado al vuelo (comprueba pyHanko)."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    from xpdf.core.signatures import sign_pdf, signature_status
    from xpdf.ui.signatures_ui import help_text

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PRUEBA XPDF")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    p12 = tmp / "prueba.p12"
    p12.write_bytes(pkcs12.serialize_key_and_certificates(
        b"prueba", key, cert, None, serialization.BestAvailableEncryption(b"clave")
    ))
    signed = tmp / "firmado.pdf"
    sign_pdf(pdf.read_bytes(), 0, (72, 600, 300, 680), p12, "clave", signed)
    st = signature_status(signed.read_bytes())
    check("firmar con certificado", len(st) == 1 and st[0].intact, str(st))
    check("ayuda de firmas", "Cómo firmar" in help_text(), "")
