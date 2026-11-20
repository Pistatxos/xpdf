"""Permite `python -m xpdf` y sirve de punto de entrada a PyInstaller."""

from xpdf.main import main

raise SystemExit(main())
