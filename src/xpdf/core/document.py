"""Modelo de documento: abrir, guardar, consultar y renderizar páginas.

Esta es la única puerta de entrada a PyMuPDF para la UI. Devuelve tipos simples
(dataclasses, bytes) para que la capa Qt no dependa de pymupdf.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from xpdf.core.errors import OpenError, SaveError

log = logging.getLogger(__name__)

#: Opciones de guardado sin pérdida: nunca alteran el aspecto ni el contenido.
#: - garbage=4: elimina objetos huérfanos, compacta xref y deduplica flujos
#:   idénticos (en la práctica no es más lento que garbage=3).
#: - deflate*: comprime con Flate los flujos sin comprimir, incluidas imágenes
#:   y fuentes (sin pérdida; las imágenes JPEG no se tocan).
#: - use_objstms: agrupa los objetos pequeños en object streams (PDF 1.5).
#: No se usa clean=True: reescribe los flujos de contenido de las páginas y el
#: ahorro medido es despreciable (<0,1 %), así que no compensa el riesgo.
SAVE_OPTIONS: dict[str, int | bool] = {
    "garbage": 4,
    "deflate": True,
    "deflate_images": True,
    "deflate_fonts": True,
    "use_objstms": 1,
}


@dataclass(frozen=True, slots=True)
class PageSize:
    """Tamaño de página en puntos PDF (1/72 pulgada), ya con la rotación aplicada."""

    width: float
    height: float


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """Mapa de bits RGB de 8 bits por canal, sin alfa."""

    width: int
    height: int
    stride: int
    samples: bytes


class Document:
    """Documento PDF abierto."""

    def __init__(self, doc: pymupdf.Document, path: Path | None) -> None:
        self._doc = doc
        self._path = path
        self._version = 0

    # ---------------------------------------------------------------- apertura
    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> Document:
        p = Path(path)
        if not p.is_file():
            raise OpenError(f"No se encuentra el archivo «{p.name}».")
        try:
            # Se carga en memoria: no bloquea el archivo y permite sobrescribirlo.
            doc = pymupdf.open("pdf", p.read_bytes())
        except Exception as exc:  # pymupdf lanza tipos variados
            log.exception("Error abriendo %s", p)
            raise OpenError(f"«{p.name}» no es un PDF válido o está dañado.") from exc
        if doc.needs_pass:
            doc.close()
            raise OpenError(
                f"«{p.name}» está protegido con contraseña. "
                "xpdf todavía no admite PDFs cifrados."
            )
        if doc.page_count == 0:
            doc.close()
            raise OpenError(f"«{p.name}» no contiene páginas.")
        log.info("Abierto %s (%d páginas)", p, doc.page_count)
        return cls(doc, p)

    @classmethod
    def new_blank(cls, width: float = 595.0, height: float = 842.0) -> Document:
        """Documento de una página en blanco (A4 por defecto). Útil en tests."""
        doc = pymupdf.open()
        doc.new_page(width=width, height=height)
        return cls(doc, None)

    def close(self) -> None:
        self._doc.close()

    # --------------------------------------------------------------- consultas
    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def page_count(self) -> int:
        return int(self._doc.page_count)

    def page_size(self, index: int) -> PageSize:
        rect = self._page(index).rect  # rect ya refleja la rotación
        return PageSize(float(rect.width), float(rect.height))

    def page_rotation(self, index: int) -> int:
        return int(self._page(index).rotation)

    # -------------------------------------------------------------- renderizado
    def render_page(self, index: int, scale: float) -> RenderedPage:
        """Renderiza la página `index` a `scale` píxeles por punto PDF."""
        if scale <= 0:
            raise ValueError("scale debe ser positivo")
        pix = self._page(index).get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=True
        )
        return RenderedPage(pix.width, pix.height, pix.stride, bytes(pix.samples))

    # ------------------------------------------------------------------ guardado
    def save(self) -> None:
        """Guarda sobre la ruta actual (la UI decide si hay que preguntar antes)."""
        if self._path is None:
            raise SaveError("El documento no tiene ruta; usa «Guardar como».")
        self.save_as(self._path)

    def save_as(self, path: str | os.PathLike[str]) -> None:
        """Guarda en `path` de forma atómica (archivo temporal + reemplazo).

        PyMuPDF no permite escribir encima del archivo que tiene abierto salvo en
        modo incremental, así que se escribe a un temporal en la misma carpeta y
        luego se reemplaza. Si algo falla, el archivo de destino queda intacto.
        """
        target = Path(path)
        try:
            fd, tmp_name = tempfile.mkstemp(prefix=".xpdf-", suffix=".pdf", dir=target.parent)
        except PermissionError as exc:
            raise SaveError(f"No hay permiso para escribir en la carpeta «{target.parent}».") from exc
        except OSError as exc:
            log.exception("Error creando temporal en %s", target.parent)
            raise SaveError(f"No se pudo guardar «{target.name}».") from exc
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            try:
                self._doc.save(tmp, **SAVE_OPTIONS)
            except Exception as exc:
                log.exception("Error escribiendo temporal para %s", target)
                raise SaveError(f"No se pudo guardar «{target.name}».") from exc
            # El temporal ya contiene todo el estado. Se carga en memoria para no
            # mantener bloqueado ningún archivo (Windows) y poder reemplazar.
            data = tmp.read_bytes()
            self._doc.close()
            self._doc = pymupdf.open("pdf", data)
            self.touch()
            try:
                os.replace(tmp, target)
            except PermissionError as exc:
                raise SaveError(
                    f"No hay permiso para escribir «{target.name}». "
                    "¿Está abierto en otro programa?"
                ) from exc
            except OSError as exc:
                log.exception("Error reemplazando %s", target)
                raise SaveError(f"No se pudo guardar «{target.name}».") from exc
        finally:
            tmp.unlink(missing_ok=True)
        self._path = target
        log.info("Guardado %s", target)

    # ------------------------------------------------------------ instantáneas
    @property
    def version(self) -> int:
        """Aumenta con cada cambio de contenido (para invalidar cachés)."""
        return self._version

    def touch(self) -> None:
        self._version += 1

    def snapshot(self) -> bytes:
        """Estado completo del documento, para deshacer.

        Sin compactar ni recomprimir: es rápido y ocupa más o menos lo que el
        archivo. (El journalling de MuPDF no sirve: deja de funcionar tras guardar.)
        """
        return bytes(self._doc.tobytes(garbage=0, deflate=False))

    def restore(self, data: bytes) -> None:
        """Vuelve a un estado tomado con `snapshot`."""
        new = pymupdf.open("pdf", data)
        self._doc.close()
        self._doc = new
        self.touch()

    # ----------------------------------------------------------------- interno
    def _page(self, index: int) -> pymupdf.Page:
        if not 0 <= index < self.page_count:
            raise IndexError(f"Página {index} fuera de rango (0..{self.page_count - 1})")
        return self._doc[index]

    @property
    def raw(self) -> pymupdf.Document:
        """Acceso al documento PyMuPDF. Solo para otros módulos de core."""
        return self._doc
