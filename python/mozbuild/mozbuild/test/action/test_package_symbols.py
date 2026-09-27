# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import mozpack.path as mozpath
import mozunit

from mozbuild.action import package_symbols

_SUBSTS = {
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "linux-x86_64",
    "PKG_SUFFIX": ".tar.xz",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-linux-x86_64.zip",
    "MOZ_CRASHREPORTER": "1",
}

_BASENAME = "firefox-156.0a1.en-US.linux-x86_64"


@contextlib.contextmanager
def _patched(tmp, substs=None, environ=None):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(
                package_symbols.buildconfig,
                "substs",
                substs if substs is not None else _SUBSTS,
            )
        )
        es.enter_context(
            mock.patch.object(package_symbols.buildconfig, "topobjdir", tmp)
        )
        es.enter_context(
            mock.patch.object(package_symbols.buildconfig, "topsrcdir", "/src")
        )
        es.enter_context(mock.patch.dict(os.environ, environ or {}, clear=True))
        yield {
            "archive": es.enter_context(
                mock.patch.object(package_symbols.symbols_archive, "main")
            ),
            "subprocess": es.enter_context(
                mock.patch.object(package_symbols.subprocess, "call", return_value=0)
            ),
        }


class TestPackageSymbols(unittest.TestCase):
    def test_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = mozpath.join(tmp, "dist")
            with _patched(tmp) as calls:
                rc = package_symbols.main([])

            self.assertTrue(os.path.isdir(dist))

        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.args[0] for call in calls["archive"].call_args_list],
            [
                [
                    mozpath.join(dist, f"{_BASENAME}.crashreporter-symbols.zip"),
                    mozpath.abspath(mozpath.join(dist, "crashreporter-symbols")),
                ]
            ],
        )

    def test_full_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = mozpath.join(tmp, "dist")
            with _patched(tmp, environ={"MOZ_ENABLE_FULL_SYMBOLS": "1"}) as calls:
                package_symbols.main([])

        symbols = mozpath.abspath(mozpath.join(dist, "crashreporter-symbols"))
        self.assertEqual(
            [call.args[0] for call in calls["archive"].call_args_list],
            [
                [
                    mozpath.join(
                        dist, f"{_BASENAME}.crashreporter-symbols-full.tar.zst"
                    ),
                    symbols,
                    "--full-archive",
                ],
                [
                    mozpath.join(dist, f"{_BASENAME}.crashreporter-symbols.zip"),
                    symbols,
                ],
            ],
        )

    def test_stale_archive_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = mozpath.join(tmp, "dist")
            os.makedirs(dist)
            stale = mozpath.join(dist, f"{_BASENAME}.crashreporter-symbols.zip")
            with open(stale, "w") as f:
                f.write("stale")

            with _patched(tmp):
                package_symbols.main([])

            self.assertFalse(os.path.exists(stale))

    def test_no_crashreporter(self):
        substs = {**_SUBSTS}
        del substs["MOZ_CRASHREPORTER"]
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(tmp, substs=substs) as calls:
                rc = package_symbols.main([])

        self.assertEqual(rc, 0)
        self.assertEqual(calls["archive"].call_count, 0)

    def test_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(tmp, environ={"MOZ_ENABLE_FULL_SYMBOLS": "1"}) as calls:
                calls["subprocess"].return_value = 4
                rc = package_symbols.main(["--upload"])

        self.assertEqual(rc, 4)
        argv = calls["subprocess"].call_args.args[0]
        self.assertEqual(
            argv[1:],
            [
                "-u",
                "/src/toolkit/crashreporter/tools/upload_symbols.py",
                mozpath.join(
                    tmp, "dist", f"{_BASENAME}.crashreporter-symbols-full.tar.zst"
                ),
            ],
        )
        self.assertEqual(calls["archive"].call_count, 0)

    def test_upload_without_full_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(tmp) as calls:
                rc = package_symbols.main(["--upload"])

        self.assertEqual(rc, 0)
        self.assertEqual(calls["subprocess"].call_count, 0)


if __name__ == "__main__":
    mozunit.main()
