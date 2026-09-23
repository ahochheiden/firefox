# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

import unittest
from io import StringIO
from types import SimpleNamespace

import mozpack.path as mozpath
from mozunit import main

from mozbuild.backend.ninja.rust import RustMixin
from mozbuild.backend.ninja_syntax import NinjaWriter


class FakeBackend(RustMixin):
    def __init__(self, substs):
        self._topobjdir = "/obj"
        self.environment = SimpleNamespace(substs=substs)
        self._rust_tests = [SimpleNamespace(objdir="/obj/crate")]

    def _write_cargo_test_spec(self, path, obj):
        pass

    def _gkrust_order_only(self):
        return []

    def _clang_plugin_order_only(self):
        return []

    def _rel_n_path(self, path):
        return mozpath.relpath(path, self._topobjdir)

    def _emit_run_edge(
        self, writer, output, steps, description, implicit, order_only, **kwargs
    ):
        writer.build(
            self._rel_n_path(output), "run", implicit=implicit, order_only=order_only
        )


class TestNinjaRustTests(unittest.TestCase):
    def emit(self, substs):
        fh = StringIO()
        FakeBackend(substs)._emit_rust_tests_statements(NinjaWriter(fh))
        return fh.getvalue().splitlines()

    def test_rusttests_without_rust_tests_enabled(self):
        lines = self.emit({})
        self.assertIn("build rusttests: phony crate/.cargo-tests.stamp", lines)

    def test_rusttests_run_on_every_request(self):
        lines = self.emit({"MOZ_RUST_TESTS": "1"})
        self.assertIn("build .ninja-force-cargo-tests: phony", lines)
        self.assertIn(
            "build crate/.cargo-tests.stamp: run | "
            "crate/.cargo-tests-spec.json .ninja-force-cargo-tests "
            "|| .ninja-rust-prereqs",
            lines,
        )


if __name__ == "__main__":
    main()
