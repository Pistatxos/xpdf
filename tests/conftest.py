from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """PDF de 3 páginas: A4, A4 apaisado y Carta rotada 90°."""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 72), "Página uno — áéíóú ñ", fontsize=14)
    p = doc.new_page(width=842, height=595)
    p.insert_text((72, 72), "Página dos", fontsize=14)
    p = doc.new_page(width=612, height=792)
    p.insert_text((72, 72), "Página tres", fontsize=14)
    p.set_rotation(90)
    path = tmp_path / "sample.pdf"
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# Real/synthetic PDF corpus (tests/samples/, see tests/samples/README.md)
# ---------------------------------------------------------------------------

import json  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

SAMPLES_DIR = Path(__file__).parent / "samples"


@dataclass(frozen=True)
class SampleInfo:
    """One corpus file plus the facts recorded in samples/manifest.json."""

    file: str
    category: str  # word | latex | scan-ocr | form | non-latin | background | rotated | synthetic-word-like
    origin: str  # "real" | "synthetic"
    tags: frozenset[str]
    source_url: str
    license: str
    min_words: tuple[int, ...]  # per page; 0 = page may have no text
    meta: dict[str, object] = field(default_factory=dict, compare=False, hash=False)

    @property
    def path(self) -> Path:
        return SAMPLES_DIR / self.file

    @property
    def page_count(self) -> int:
        return len(self.min_words)

    @property
    def id(self) -> str:
        return self.file.removesuffix(".pdf")

    def has(self, *tags: str) -> bool:
        return all(t in self.tags or t == self.category for t in tags)


def load_samples() -> list[SampleInfo]:
    data = json.loads((SAMPLES_DIR / "manifest.json").read_text("utf-8"))
    return [
        SampleInfo(
            file=e["file"],
            category=e["category"],
            origin=e["origin"],
            tags=frozenset(e["tags"]),
            source_url=e["source_url"],
            license=e["license"],
            min_words=tuple(e["min_words"]),
            meta=e,
        )
        for e in data["samples"]
    ]


SAMPLES: list[SampleInfo] = load_samples()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "samples(*tags, origin=None): restrict the `sample` parameter to corpus files "
        "having all the given tags/category (and origin 'real'/'synthetic').",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Any test taking a `sample` argument runs once per corpus file.

    Filter with ``@pytest.mark.samples("word", "justified")`` or
    ``@pytest.mark.samples(origin="real")``.
    """
    if "sample" not in metafunc.fixturenames:
        return
    chosen = SAMPLES
    for mark in metafunc.definition.iter_markers("samples"):
        origin = mark.kwargs.get("origin")
        chosen = [s for s in chosen if s.has(*mark.args) and (origin is None or s.origin == origin)]
    metafunc.parametrize("sample", chosen, ids=[s.id for s in chosen])


@pytest.fixture(scope="session")
def samples() -> list[SampleInfo]:
    """All corpus entries (path + category/tags/expectations)."""
    return list(SAMPLES)


@pytest.fixture
def sample_copy(tmp_path: Path) -> Callable[[SampleInfo], Path]:
    """Copy a corpus file to tmp_path so a test can modify/save it freely."""

    def _copy(info: SampleInfo) -> Path:
        dst = tmp_path / info.file
        dst.write_bytes(info.path.read_bytes())
        return dst

    return _copy


@pytest.fixture
def visual_dump(request: pytest.FixtureRequest) -> Iterator[Callable[..., Path | None]]:
    """Callable(before, after, region=None, suffix="") that writes a
    before|after|diff PNG to $XPDF_VISUAL_OUT (no-op when unset)."""
    from tests.visual import dump_if_requested

    def _dump(before: object, after: object, region: object = None, suffix: str = "") -> Path | None:
        name = request.node.nodeid.split("::", 1)[-1] + (f"_{suffix}" if suffix else "")
        return dump_if_requested(name, before, after, region)  # type: ignore[arg-type]

    yield _dump


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """En GitHub Actions, cada fallo se publica como anotación (visible sin
    autenticarse en un repositorio público, a diferencia de los registros)."""
    import os
    import sys

    if not os.environ.get("GITHUB_ACTIONS") or not report.failed:
        return
    text = report.longreprtext[-3000:]
    msg = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    title = report.nodeid.replace(",", "%2C").replace("::", " · ")
    sys.__stdout__.write(f"::error title={title}::{msg}\n")
    sys.__stdout__.flush()
