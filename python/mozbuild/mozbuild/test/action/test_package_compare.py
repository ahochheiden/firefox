# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import tempfile
import unittest
from io import StringIO
from unittest import mock

import mozunit

from mozbuild.action import package_compare

_SUBSTS = {"MOZ_PKG_BINPATH": "bin", "OS_ARCH": "Linux"}


@contextlib.contextmanager
def _patched(substs, topobjdir):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_compare.buildconfig, "substs", substs)
        )
        es.enter_context(
            mock.patch.object(package_compare.buildconfig, "topobjdir", topobjdir)
        )
        yield


class TestPackageCompare(unittest.TestCase):
    def _write(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)

    def _compare(self, manifest_lines, staged, substs=None, defines=()):
        substs = substs if substs is not None else _SUBSTS
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "package-manifest.in")
            lines = ("#filter substitution", *manifest_lines)
            body = "".join(f"{line}\n" for line in lines)
            self._write(manifest, body)
            for rel in staged:
                self._write(os.path.join(tmp, "dist", rel), "")

            out = StringIO()
            with _patched(substs, tmp):
                rc = package_compare.package_compare(
                    manifest=manifest,
                    source=os.path.join(tmp, "dist"),
                    defines=defines,
                    output=out,
                )
        return rc, out.getvalue().splitlines()

    def test_match(self):
        rc, lines = self._compare(["bin/one", "bin/two"], ["bin/one", "bin/two"])

        self.assertEqual(rc, 0)
        self.assertEqual(lines, [])

    def test_missing_from_manifest(self):
        rc, lines = self._compare(["bin/one"], ["bin/one", "bin/extra"])

        self.assertEqual(rc, 0)
        self.assertIn("+bin/extra", lines)

    def test_missing_from_tree(self):
        _, lines = self._compare(["bin/one", "bin/absent"], ["bin/one"])

        self.assertIn("-bin/absent", lines)

    def test_manifest_outside_binpath(self):
        _, lines = self._compare(
            ["bin/one", "[browser]", "other/thing", "binary/thing"], ["bin/one"]
        )

        self.assertEqual(lines, [])

    def test_manifest_absolute_path(self):
        _, lines = self._compare(["/bin/one"], [])

        self.assertEqual(lines, [])

    def test_tree_outside_binpath(self):
        _, lines = self._compare(["bin/one"], ["bin/one", "host/bin/mar"])

        self.assertEqual(lines, [])

    def test_defines(self):
        _, lines = self._compare(
            ["#ifdef MOZ_EXTRA", "bin/extra", "#endif", "bin/one"],
            ["bin/one", "bin/extra"],
            defines=["MOZ_EXTRA=1"],
        )

        self.assertEqual(lines, [])

    def test_defines_undefined(self):
        _, lines = self._compare(
            ["#ifdef MOZ_EXTRA", "bin/extra", "#endif", "bin/one"],
            ["bin/one", "bin/extra"],
        )

        self.assertIn("+bin/extra", lines)

    def test_binpath_default(self):
        substs = {"MOZ_PKG_BINPATH": "Nightly.app/Contents/MacOS", "OS_ARCH": "Darwin"}
        _, lines = self._compare(
            ["Nightly.app/Contents/MacOS/one"],
            ["Nightly.app/Contents/MacOS/one"],
            substs=substs,
        )

        self.assertEqual(lines, [])

    def test_binpath_substituted(self):
        _, lines = self._compare(
            ["@BINPATH@/one"], ["bin/one"], defines=["BINPATH=bin"]
        )

        self.assertEqual(lines, [])

    def test_staged_tree_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "package-manifest.in")
            self._write(manifest, "#filter substitution\nbin/one\n")

            out = StringIO()
            with _patched(_SUBSTS, tmp):
                rc = package_compare.package_compare(
                    manifest=manifest, source=os.path.join(tmp, "dist"), output=out
                )

        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")

    def test_source_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "package-manifest.in")
            self._write(manifest, "#filter substitution\nbin/one\n")
            self._write(os.path.join(tmp, "dist", "bin", "one"), "")

            out = StringIO()
            with _patched(_SUBSTS, tmp):
                rc = package_compare.package_compare(manifest=manifest, output=out)

        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    mozunit.main()
