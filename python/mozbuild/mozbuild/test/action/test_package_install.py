# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import mozunit

from mozbuild.action import package_install

_SUBSTS = {
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "linux-x86_64",
    "PKG_SUFFIX": ".tar.xz",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-linux-x86_64.zip",
    "MOZ_APP_NAME": "firefox",
    "OS_ARCH": "Linux",
    "OS_TARGET": "Linux",
    "TAR": "tar",
    "libdir": "/usr/local/lib",
    "bindir": "/usr/local/bin",
}


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = mock.MagicMock()

    def wait(self):
        return self.returncode


@contextlib.contextmanager
def _patched(substs, topobjdir, returncodes=(0, 0)):
    procs = [_Proc(rc) for rc in returncodes]
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_install.buildconfig, "substs", substs)
        )
        es.enter_context(
            mock.patch.object(package_install.buildconfig, "topobjdir", topobjdir)
        )
        popen = es.enter_context(
            mock.patch.object(
                package_install.subprocess, "Popen", side_effect=list(procs)
            )
        )
        yield popen


class TestPackageInstall(unittest.TestCase):
    def test_unsupported_platform(self):
        for os_target in ("WINNT", "Darwin"):
            substs = {**_SUBSTS, "OS_TARGET": os_target}
            with tempfile.TemporaryDirectory() as tmp:
                with _patched(substs, tmp) as popen:
                    rc = package_install.package_install()

            self.assertEqual(rc, 1, os_target)
            self.assertEqual(popen.call_count, 0, os_target)

    def test_tar_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp) as popen:
                rc = package_install.package_install(destdir=tmp)

            self.assertEqual(rc, 0)
            create, extract = popen.call_args_list
            self.assertEqual(
                create.args[0],
                [
                    "tar",
                    "--exclude=precomplete",
                    "--exclude=.mkdir.done",
                    "-chf",
                    "-",
                    ".",
                ],
            )
            self.assertTrue(create.kwargs["cwd"].endswith("dist/firefox"))
            self.assertEqual(extract.args[0], ["tar", "-xf", "-"])
            self.assertEqual(extract.kwargs["cwd"], f"{tmp}/usr/local/lib/firefox")

    def test_launcher_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp):
                rc = package_install.package_install(destdir=tmp)

            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(f"{tmp}/usr/local/lib/firefox"))
            link = f"{tmp}/usr/local/bin/firefox"
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), "/usr/local/lib/firefox/firefox")

    def test_launcher_symlink_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(f"{tmp}/usr/local/bin")
            os.symlink("/somewhere/else", f"{tmp}/usr/local/bin/firefox")

            with _patched(_SUBSTS, tmp):
                rc = package_install.package_install(destdir=tmp)

            self.assertEqual(rc, 0)
            self.assertEqual(
                os.readlink(f"{tmp}/usr/local/bin/firefox"),
                "/usr/local/lib/firefox/firefox",
            )

    def test_installdir_bindir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp) as popen:
                rc = package_install.package_install(
                    destdir=tmp, installdir="/opt/firefox", bindir="/opt/bin"
                )

            self.assertEqual(rc, 0)
            self.assertEqual(
                popen.call_args_list[1].kwargs["cwd"], f"{tmp}/opt/firefox"
            )
            self.assertEqual(
                os.readlink(f"{tmp}/opt/bin/firefox"), "/opt/firefox/firefox"
            )

    def test_pkg_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp) as popen:
                package_install.package_install(destdir=tmp, pkg_dir="geckoview")

            self.assertTrue(
                popen.call_args_list[0].kwargs["cwd"].endswith("dist/geckoview")
            )

    def test_archive_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp, returncodes=(2, 0)):
                rc = package_install.package_install(destdir=tmp)

            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(f"{tmp}/usr/local/bin/firefox"))

    def test_extract_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp, returncodes=(0, 3)):
                rc = package_install.package_install(destdir=tmp)

            self.assertEqual(rc, 3)
            self.assertFalse(os.path.exists(f"{tmp}/usr/local/bin/firefox"))


if __name__ == "__main__":
    mozunit.main()
