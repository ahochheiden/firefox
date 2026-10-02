# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import pytest
from mozunit import main

from mach import func_cache


@pytest.fixture
def topsrcdir(tmp_path, monkeypatch):
    for path in (
        "a/x.gradle",
        "a/b/y.gradle",
        "a/b/.hidden/z.gradle",
        "a/b/gradle.properties",
        "a/b/c/settings.gradle.kts",
        "a/b/c/x.gradle.pyc",
        "a/d.gradle/inner.txt",
        "a/other.txt",
        "e/w.gradle",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(path)
    monkeypatch.setattr(func_cache, "_topsrcdir", tmp_path)
    return tmp_path


def test_resolve_inputs_shared_walk(topsrcdir):
    patterns = [
        "a/**/*.gradle",
        "a/**/*.gradle.kts",
        "a/**/gradle.properties",
        "e/**/*.gradle",
        "a/*.txt",
    ]
    expected = set()
    for pattern in patterns:
        expected.update(
            p
            for p in topsrcdir.glob(pattern)
            if p.is_file() and not p.name.endswith((".pyc", ".pyd", ".pyo"))
        )
    assert func_cache._resolve_inputs(patterns) == sorted(expected)


def test_hash_inputs_reuses_unchanged_files(topsrcdir, tmp_path, monkeypatch):
    hashes_file = tmp_path / "cache" / "_file_hashes.json"
    old = 1_000_000_000_000_000_000
    for path in topsrcdir.rglob("*"):
        if path.is_file():
            os.utime(path, ns=(old, old))

    first = func_cache._hash_inputs(["a", "e"], hashes_file=hashes_file)
    assert first == func_cache._hash_inputs(["a", "e"])

    reads = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        reads.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    assert func_cache._hash_inputs(["a", "e"], hashes_file=hashes_file) == first
    assert not [r for r in reads if not r.startswith(str(hashes_file))]

    changed = topsrcdir / "a" / "x.gradle"
    changed.write_text("changed")
    os.utime(changed, ns=(old + 1, old + 1))
    reads.clear()
    second = func_cache._hash_inputs(["a", "e"], hashes_file=hashes_file)
    assert second != first
    assert [r for r in reads if not r.startswith(str(hashes_file))] == [str(changed)]


def test_hash_inputs_rereads_recent_files(topsrcdir, tmp_path, monkeypatch):
    hashes_file = tmp_path / "cache" / "_file_hashes.json"
    func_cache._hash_inputs(["e"], hashes_file=hashes_file)

    reads = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        reads.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    func_cache._hash_inputs(["e"], hashes_file=hashes_file)
    assert str(topsrcdir / "e" / "w.gradle") in reads


if __name__ == "__main__":
    main()
