# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import tempfile
import unittest
from unittest import mock

import mozunit

from mozbuild.action import package_generated_sources

_SUBSTS = {
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "linux-x86_64",
    "PKG_SUFFIX": ".tar.xz",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-linux-x86_64.zip",
}


@contextlib.contextmanager
def _patched(tmp):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_generated_sources.buildconfig, "substs", _SUBSTS)
        )
        es.enter_context(
            mock.patch.object(package_generated_sources.buildconfig, "topobjdir", tmp)
        )
        es.enter_context(
            mock.patch.object(
                package_generated_sources, "get_generated_sources", return_value=[]
            )
        )
        yield


class TestPackageGeneratedSources(unittest.TestCase):
    def test_default_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(tmp):
                package_generated_sources.main([])

            self.assertEqual(
                os.listdir(os.path.join(tmp, "dist")),
                ["firefox-156.0a1.en-US.linux-x86_64.generated-files.tar.gz"],
            )

    def test_explicit_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "sources.tar.gz")
            with _patched(tmp):
                package_generated_sources.main([output])

            self.assertTrue(os.path.exists(output))


if __name__ == "__main__":
    mozunit.main()
