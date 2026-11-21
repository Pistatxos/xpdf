from __future__ import annotations

import datetime as dt
from pathlib import Path

import pymupdf
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from xpdf.core import signatures
from xpdf.core.signatures import (
    SignatureError,
    SignatureStore,
    is_signed,
    read_certificate,
    sign_pdf,
    signature_status,
)


def make_p12(path: Path, cn: str, password: str) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            cn.encode(), key, cert, None, serialization.BestAvailableEncryption(password.encode())
        )
    )
    return path


@pytest.fixture
def certs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        make_p12(tmp_path / "mario.p12", "MARIO PRUEBA", "secreto"),
        make_p12(tmp_path / "mireia.pfx", "MIREIA PRUEBA", "otra ñ"),
    )


@pytest.fixture
def pdf() -> bytes:
    d = pymupdf.open()
    d.new_page().insert_text((72, 100), "Contrato de prueba", fontsize=20)
    return bytes(d.tobytes())


def test_read_certificate_and_wrong_password(certs: tuple[Path, Path]) -> None:
    info = read_certificate(certs[1], "otra ñ")
    assert info.subject == "MIREIA PRUEBA" and info.not_after > dt.datetime.now(dt.timezone.utc)
    with pytest.raises(SignatureError, match="Contraseña incorrecta"):
        read_certificate(certs[0], "mala")
    with pytest.raises(SignatureError, match="No se encuentra"):
        read_certificate(certs[0].with_name("no.p12"), "x")


def test_store_keeps_several_and_never_the_password(certs: tuple[Path, Path], tmp_path: Path) -> None:
    folder = tmp_path / "firmas"
    store = SignatureStore.load(folder)
    a = store.add_certificate(certs[0], "secreto")
    store.add_certificate(certs[1], "otra ñ", label="Mireia")
    store.add_drawn(b"\x89PNG fake", "Mi rúbrica")
    again = SignatureStore.load(folder)
    assert [c.label for c in again.certificates] == ["MARIO PRUEBA", "Mireia"]
    assert [d.label for d in again.drawn] == ["Mi rúbrica"]
    index = (folder / "firmas.json").read_text(encoding="utf-8")
    assert "secreto" not in index and "otra" not in index
    again.remove_certificate(a.id)
    assert [c.label for c in SignatureStore.load(folder).certificates] == ["Mireia"]


def test_sign_verify_and_second_signature(certs: tuple[Path, Path], pdf: bytes, tmp_path: Path) -> None:
    out1 = tmp_path / "firmado.pdf"
    who = sign_pdf(pdf, 0, (72, 600, 300, 680), certs[0], "secreto", out1)
    assert who == "MARIO PRUEBA"
    st = signature_status(out1.read_bytes())
    assert [(s.signer, s.intact) for s in st] == [("MARIO PRUEBA", True)]
    assert is_signed(pymupdf.open(out1))
    # el recuadro está donde se dibujó (coordenadas de pantalla)
    w = next(pymupdf.open(out1)[0].widgets())
    assert tuple(w.rect) == pytest.approx((72, 600, 300, 680), abs=0.5)
    # segunda firma sobre el archivo firmado: las dos siguen íntegras
    out2 = tmp_path / "firmado2.pdf"
    sign_pdf(out1.read_bytes(), 0, (320, 600, 540, 680), certs[1], "otra ñ", out2)
    st2 = signature_status(out2.read_bytes())
    assert [(s.signer, s.intact) for s in st2] == [("MARIO PRUEBA", True), ("MIREIA PRUEBA", True)]


def test_modification_after_signing_is_detected(certs: tuple[Path, Path], pdf: bytes, tmp_path: Path) -> None:
    out = tmp_path / "firmado.pdf"
    sign_pdf(pdf, 0, (72, 600, 300, 680), certs[0], "secreto", out)
    d = pymupdf.open(out)
    d[0].insert_text((72, 300), "Añadido después", fontsize=12)
    tampered = d.tobytes()
    assert [s.intact for s in signature_status(tampered)] == [False]


def test_sign_with_drawn_signature(certs: tuple[Path, Path], pdf: bytes, tmp_path: Path) -> None:
    img = tmp_path / "rubrica.png"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 40), True)
    pix.clear_with(0)
    pix.set_rect(pymupdf.IRect(10, 15, 110, 25), (20, 40, 120, 255))
    pix.save(str(img))
    out = tmp_path / "firmado.pdf"
    sign_pdf(pdf, 0, (72, 600, 330, 680), certs[0], "secreto", out, signature_image=img)
    assert [s.intact for s in signature_status(out.read_bytes())] == [True]
    # la rúbrica azul se ve dentro del recuadro
    page = pymupdf.open(out)[0]
    px = page.get_pixmap(dpi=72, clip=pymupdf.Rect(72, 600, 200, 680))
    s = px.samples
    assert any(s[i + 2] > 100 and s[i] < 60 for i in range(0, len(s), px.n))


@pytest.mark.parametrize("rotation", [90, 270])
def test_box_on_rotated_page(certs: tuple[Path, Path], tmp_path: Path, rotation: int) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.set_rotation(rotation)
    out = tmp_path / "girada.pdf"
    sign_pdf(bytes(d.tobytes()), 0, (100, 100, 300, 160), certs[0], "secreto", out)
    page = pymupdf.open(out)[0]
    w = next(page.widgets())
    visual = pymupdf.Rect(w.rect) * page.rotation_matrix
    visual.normalize()
    assert tuple(visual) == pytest.approx((100, 100, 300, 160), abs=0.5)


def test_clean_signature_image(tmp_path: Path) -> None:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 800, 300), False)
    pix.set_rect(pix.irect, (248, 246, 240))  # papel
    pix.set_rect(pymupdf.IRect(200, 140, 600, 160), (20, 30, 120))  # trazo
    src = tmp_path / "firma.jpg"
    pix.save(src)
    out = pymupdf.Pixmap(signatures.clean_signature_image(src))
    assert out.alpha
    assert out.width < 420 and out.height < 40  # recortada a la tinta
    assert out.pixel(0, 0)[3] == 0  # fondo transparente
    assert out.pixel(out.width // 2, out.height // 2)[3] == 255


def test_clean_signature_image_blank(tmp_path: Path) -> None:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 50), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    src = tmp_path / "blanco.png"
    pix.save(src)
    with pytest.raises(signatures.SignatureError, match="ninguna firma"):
        signatures.clean_signature_image(src)


def test_sign_edited_document(certs: tuple[Path, Path], tmp_path: Path) -> None:
    """Un documento editado se firma desde su estado compactado (to_bytes)."""
    from xpdf.core.document import Document

    doc = Document.new_blank()
    doc.raw[0].insert_text((72, 72), "Editado")
    out = tmp_path / "firmado.pdf"
    signatures.sign_pdf(doc.to_bytes(), 0, (72, 600, 300, 680), certs[0], "secreto", out)
    st = signatures.signature_status(out.read_bytes())
    assert [s.intact for s in st] == [True]
    assert "Editado" in pymupdf.open(out)[0].get_text()
