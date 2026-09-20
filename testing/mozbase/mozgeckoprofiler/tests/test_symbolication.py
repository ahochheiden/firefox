#!/usr/bin/env python

import zipfile
from pathlib import Path

import mozunit
import pytest
from mozgeckoprofiler.symbolication import get_extracted_symbols


@pytest.fixture(autouse=True)
def clear_symbols_cache():
    get_extracted_symbols.cache_clear()
    yield
    get_extracted_symbols.cache_clear()


def make_symbol_zip(path):
    with zipfile.ZipFile(path, "w") as zipf:
        zipf.writestr("xul.so/ABCDEF/xul.so.sym", "MODULE Linux x86_64 ABCDEF xul.so\n")
    return path


def test_prefers_extracted_symbols_dir(monkeypatch, tmp_path):
    fetches = tmp_path / "fetches"
    (fetches / "symbols").mkdir(parents=True)
    monkeypatch.setenv("MOZ_AUTOMATION", "1")
    monkeypatch.setenv("MOZ_FETCHES_DIR", str(fetches))
    monkeypatch.delenv("MOZ_SYMBOLS_URL", raising=False)

    assert get_extracted_symbols() == fetches / "symbols"


def test_extracts_fetched_zip(monkeypatch, tmp_path):
    fetches = tmp_path / "fetches"
    fetches.mkdir()
    make_symbol_zip(fetches / "target.crashreporter-symbols.zip")
    monkeypatch.setenv("MOZ_AUTOMATION", "1")
    monkeypatch.setenv("MOZ_FETCHES_DIR", str(fetches))
    monkeypatch.delenv("MOZ_SYMBOLS_URL", raising=False)

    symbol_dir = get_extracted_symbols()

    assert (symbol_dir / "xul.so" / "ABCDEF" / "xul.so.sym").is_file()


def test_downloads_zip_from_symbols_url(monkeypatch, tmp_path):
    fetches = tmp_path / "fetches"
    fetches.mkdir()
    served = make_symbol_zip(tmp_path / "served.zip")
    monkeypatch.setenv("MOZ_AUTOMATION", "1")
    monkeypatch.setenv("MOZ_FETCHES_DIR", str(fetches))
    monkeypatch.setenv("MOZ_SYMBOLS_URL", Path(served).as_uri())

    symbol_dir = get_extracted_symbols()

    assert (symbol_dir / "xul.so" / "ABCDEF" / "xul.so.sym").is_file()
    assert not (fetches / "target.crashreporter-symbols.zip").exists()


def test_symbolicate_profiles_skips_symbols_without_profiles(monkeypatch, tmp_path):
    from mozgeckoprofiler import profiling

    def fail(*args, **kwargs):
        raise AssertionError("symbols were fetched with no profile to symbolicate")

    monkeypatch.setattr(profiling, "get_extracted_symbols", fail)
    (tmp_path / "profile_resource-usage.json").write_text("{}")

    profiling.symbolicate_profiles(profile_dir=tmp_path)


def test_no_symbols_without_url(monkeypatch, tmp_path):
    fetches = tmp_path / "fetches"
    fetches.mkdir()
    monkeypatch.setenv("MOZ_AUTOMATION", "1")
    monkeypatch.setenv("MOZ_FETCHES_DIR", str(fetches))
    monkeypatch.delenv("MOZ_SYMBOLS_URL", raising=False)

    assert get_extracted_symbols() is None


if __name__ == "__main__":
    mozunit.main()
