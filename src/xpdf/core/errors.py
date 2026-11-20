"""Errores de dominio con mensajes pensados para mostrarse al usuario."""


class XpdfError(Exception):
    """Error base de xpdf. `str(err)` es un mensaje apto para el usuario."""


class OpenError(XpdfError):
    """No se pudo abrir el documento."""


class SaveError(XpdfError):
    """No se pudo guardar el documento."""
