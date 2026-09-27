# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import tempfile
import unittest
from unittest import mock

import mozunit
from mozversioncontrol import InvalidRepoPath

from mozbuild.controller import building
from mozbuild.controller.building import SOURCE_REPO_FILE, write_source_repo

_SUBSTS = {"MOZ_BUILD_APP": "browser"}


class TestWriteSourceRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.topobjdir = self._tmp.name
        self.path = os.path.join(self.topobjdir, SOURCE_REPO_FILE)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, substs=None, head_rev=None, remote=None, side_effect=None):
        vcs = mock.Mock(head_rev=head_rev)
        vcs.get_remote_url.return_value = remote
        with mock.patch.object(
            building,
            "get_repository_object",
            return_value=vcs,
            side_effect=side_effect,
        ) as get_repo:
            write_source_repo(self.topobjdir, "/src", {**_SUBSTS, **(substs or {})})
        return get_repo

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_configured(self):
        get_repo = self._write({
            "MOZ_SOURCE_REPO": "https://hg.mozilla.org/mozilla-central",
            "MOZ_SOURCE_CHANGESET": "abc123",
        })

        self.assertEqual(
            self._read(), "abc123\nhttps://hg.mozilla.org/mozilla-central\n"
        )
        self.assertEqual(get_repo.call_count, 0)

    def test_from_vcs(self):
        self._write(head_rev="def456", remote="ssh://hg.mozilla.org/mozilla-central/")

        self.assertEqual(
            self._read(), "def456\nhttps://hg.mozilla.org/mozilla-central\n"
        )

    def test_from_vcs_without_remote(self):
        self._write(head_rev="def456")

        self.assertEqual(self._read(), "def456\n\n")

    def test_configured_repo_changeset_from_vcs(self):
        self._write(
            {"MOZ_SOURCE_REPO": "ssh://example.com/repo/"},
            head_rev="def456",
            remote="https://elsewhere.example.com",
        )

        self.assertEqual(self._read(), "def456\nssh://example.com/repo/\n")

    def test_configured_repo_without_changeset(self):
        with self.assertRaisesRegex(Exception, "could not resolve changeset"):
            self._write(
                {"MOZ_SOURCE_REPO": "https://example.com/repo"},
                side_effect=InvalidRepoPath("no repo"),
            )

    def test_no_vcs(self):
        self._write(side_effect=InvalidRepoPath("no repo"))

        self.assertEqual(self._read(), "\n\n")

    def test_android_keeps_first(self):
        substs = {"MOZ_BUILD_APP": "mobile/android"}
        self._write(substs, head_rev="first")
        self._write(substs, head_rev="second")

        self.assertEqual(self._read(), "first\n\n")


if __name__ == "__main__":
    mozunit.main()
