from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from xpdf import updater
from xpdf.updater import UpdateError, asset_name, fetch_latest, is_newer, parse_version, verify


def test_parse_and_compare_versions() -> None:
    assert parse_version("v1.2.3") == ((1, 2, 3), False)
    assert parse_version("0.4") == ((0, 4, 0), False)
    assert parse_version("1.3.0-beta.2") == ((1, 3, 0), True)
    assert is_newer("0.3.1", "0.3.0")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("0.3.0", "0.3.0")
    assert not is_newer("0.2.9", "0.3.0")
    assert not is_newer("0.4.0-beta.1", "0.4.0")
    assert is_newer("0.4.0", "0.4.0-beta.1")
    with pytest.raises(ValueError):
        parse_version("latest")


def test_asset_names() -> None:
    assert asset_name("v0.3.0", "win32") == "xpdf-0.3.0-windows-x64-setup.exe"
    assert asset_name("0.3.0", "darwin") == "xpdf-0.3.0-macos-arm64.zip"
    assert asset_name("0.3.0", "linux") is None


def _release_json(tag: str, names: list[str]) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "html_url": "https://example/release",
            "body": "Novedades",
            "assets": [
                {"name": n, "browser_download_url": f"https://example/{n}", "size": 10} for n in names
            ],
        }
    ).encode()


def test_fetch_latest_picks_platform_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater.sys, "platform", "win32")
    name = "xpdf-0.4.0-windows-x64-setup.exe"
    rel = fetch_latest("u/xpdf", getter=lambda url: _release_json("v0.4.0", [name, "SHA256SUMS.txt"]))
    assert rel is not None and rel.version == "0.4.0" and rel.asset_name == name
    assert rel.sums_url.endswith("SHA256SUMS.txt")
    # sin sumas publicadas o sin paquete para la plataforma: no se ofrece
    assert fetch_latest("u/xpdf", getter=lambda url: _release_json("v0.4.0", [name])) is None
    assert fetch_latest("u/xpdf", getter=lambda url: _release_json("v0.4.0", ["SHA256SUMS.txt"])) is None


def test_unconfigured_repo_and_network_error() -> None:
    assert fetch_latest("TU-USUARIO/xpdf") is None

    def boom(url: str) -> bytes:
        raise OSError("sin red")

    with pytest.raises(UpdateError, match="conexión"):
        fetch_latest("u/xpdf", getter=boom)


class _Resp(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _rel(name: str) -> updater.Release:
    return updater.Release("0.4.0", name, "https://x/" + name, 0, "https://x/SHA256SUMS.txt", "", "")


def test_download_verifies_checksum(tmp_path: Path) -> None:
    payload = b"instalador" * 1000
    name = "xpdf-0.4.0-windows-x64-setup.exe"
    good = f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()
    seen: list[tuple[int, int]] = []
    out = updater.download(
        _rel(name), tmp_path, lambda d, t: seen.append((d, t)),
        getter=lambda url: good, opener=lambda url: _Resp(payload),
    )
    assert out.read_bytes() == payload and seen[-1] == (len(payload), len(payload))

    bad = f"{'0' * 64}  {name}\n".encode()
    with pytest.raises(UpdateError, match="SHA-256"):
        updater.download(_rel(name), tmp_path, getter=lambda url: bad, opener=lambda url: _Resp(payload))
    assert not (tmp_path / name).exists()  # no se deja nada a medias


def test_verify_formats(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    assert verify(f, f"{digest}  a.bin", "a.bin")
    assert verify(f, f"{digest} *a.bin", "a.bin")  # formato binario de sha256sum
    assert not verify(f, f"{digest}  otro.bin", "a.bin")


def test_macos_script_waits_replaces_and_reopens(tmp_path: Path) -> None:
    script = updater._macos_script(tmp_path / "x.zip", Path("/Applications/xpdf.app"), 4242)
    text = script.read_text()
    assert "kill -0 4242" in text and "ditto -x -k" in text
    assert "com.apple.quarantine" in text and "/usr/bin/open" in text


def test_not_self_updating_in_development() -> None:
    assert not updater.can_self_update()


def test_windows_app_control_block_is_reported_clearly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WinError 4551 (Control inteligente de aplicaciones) no debe ser un «error inesperado»."""
    monkeypatch.setattr(updater.sys, "platform", "win32")

    def blocked(*args: object, **kwargs: object) -> None:
        err = OSError("bloqueado")
        err.winerror = 4551  # type: ignore[attr-defined]
        raise err

    monkeypatch.setattr(updater.subprocess, "Popen", blocked)
    with pytest.raises(updater.BlockedBySystemError, match="Control inteligente"):
        updater.apply_update(tmp_path / "setup.exe")

    def other(*args: object, **kwargs: object) -> None:
        raise OSError("otro fallo")

    monkeypatch.setattr(updater.subprocess, "Popen", other)
    with pytest.raises(UpdateError) as info:
        updater.apply_update(tmp_path / "setup.exe")
    assert not isinstance(info.value, updater.BlockedBySystemError)


def test_install_command_uses_configured_repo() -> None:
    assert updater.install_command("u/r") == "irm https://raw.githubusercontent.com/u/r/main/scripts/install.ps1 | iex"
