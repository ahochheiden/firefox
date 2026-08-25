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

from mozbuild.action import package_upload
from mozbuild.packaging import PackagePaths, files_to_upload

_SUBSTS = {
    "MOZ_BUILD_APP": "browser",
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "win64",
    "PKG_SUFFIX": ".zip",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-win64.zip",
    "PKG_LANGPACK_PATH": "win64/xpi/",
    "OS_ARCH": "WINNT",
    "OS_TARGET": "WINNT",
}


@contextlib.contextmanager
def _patched(substs, topobjdir, environ=None):
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.object(package_upload.buildconfig, "substs", substs)
        )
        es.enter_context(
            mock.patch.object(package_upload.buildconfig, "topsrcdir", "/src")
        )
        es.enter_context(
            mock.patch.object(package_upload.buildconfig, "topobjdir", topobjdir)
        )
        es.enter_context(mock.patch.dict(os.environ, environ or {}, clear=False))
        yield {
            "copy": es.enter_context(
                mock.patch.object(package_upload.upload, "CopyFilesLocally")
            ),
            "checksums": es.enter_context(
                mock.patch.object(package_upload.checksums, "process_files")
            ),
        }


class TestPackageUpload(unittest.TestCase):
    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("")

    def test_upload_path_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                with _patched(_SUBSTS, tmp) as calls:
                    rc = package_upload.package_upload()

        self.assertEqual(rc, 1)
        self.assertEqual(calls["copy"].call_count, 0)

    def test_upload_path_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched(_SUBSTS, tmp, {"UPLOAD_PATH": "/upload"}) as calls:
                rc = package_upload.package_upload()

        self.assertEqual(rc, 0)
        self.assertEqual(calls["copy"].call_args_list[0].args[0], "/upload")

    def test_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            self._touch(os.path.join(dist, "firefox-156.0a1.en-US.win64.zip"))
            self._touch(os.path.join(tmp, "config.status"))

            with _patched(_SUBSTS, tmp) as calls:
                rc = package_upload.package_upload(upload_path="/upload")

        self.assertEqual(rc, 0)
        uploaded = calls["copy"].call_args_list[0].args[1]
        self.assertEqual(
            sorted(os.path.basename(f) for f in uploaded),
            ["config.status", "firefox-156.0a1.en-US.win64.zip"],
        )

    def test_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            self._touch(os.path.join(dist, "firefox-156.0a1.en-US.win64.zip"))

            with _patched(_SUBSTS, tmp) as calls:
                rc = package_upload.package_upload(
                    upload_path="/upload", files=["/obj/source.tar.zst"]
                )

        self.assertEqual(rc, 0)
        self.assertEqual(
            calls["copy"].call_args_list[0].args[1], ["/obj/source.tar.zst"]
        )

    def test_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            checksum_file = os.path.join(tmp, "checks", "target.checksums")

            with _patched(_SUBSTS, tmp) as calls:
                rc = package_upload.package_upload(
                    upload_path="/upload", checksum_file=checksum_file
                )

            self.assertTrue(os.path.isdir(os.path.dirname(checksum_file)))

        self.assertEqual(rc, 0)
        self.assertEqual(
            calls["checksums"].call_args.args,
            (["/upload"], checksum_file, ["sha512", "md5", "sha1"]),
        )
        self.assertEqual(calls["copy"].call_args_list[1].args[1], [checksum_file])

    def test_checksum_file_bare_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_SUBSTS, tmp) as calls:
                rc = package_upload.package_upload(
                    upload_path="/upload", checksum_file="target.checksums"
                )

        self.assertEqual(rc, 0)
        self.assertEqual(calls["checksums"].call_args.args[1], "target.checksums")

    def test_checksum_file_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_SUBSTS, tmp) as calls:
                package_upload.package_upload(upload_path="/upload")

        self.assertTrue(
            calls["checksums"]
            .call_args.args[1]
            .endswith("dist/firefox-156.0a1.en-US.win64.checksums")
        )

    def test_simple_package_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_SUBSTS, tmp) as calls:
                package_upload.package_upload(
                    upload_path="/upload", simple_package_name="target"
                )

        self.assertTrue(
            calls["checksums"].call_args.args[1].endswith("dist/target.checksums")
        )

    def test_digests(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_SUBSTS, tmp) as calls:
                package_upload.package_upload(upload_path="/upload", digests=["sha256"])

        self.assertEqual(calls["checksums"].call_args.args[2], ["sha256"])

    def test_base_path_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "dist"))
            with _patched(_SUBSTS, tmp) as calls:
                package_upload.package_upload(upload_path="/upload")

        self.assertTrue(
            calls["copy"].call_args_list[0].kwargs["base_path"].endswith("dist")
        )


class TestFilesToUpload(unittest.TestCase):
    def _touch(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("")

    def _files(self, substs, ab_cd="en-US", present=(), extra_files=()):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            for rel in present:
                self._touch(os.path.join(tmp, rel))
            paths = PackagePaths(substs=substs, dist_dir=dist, ab_cd=ab_cd)
            files = files_to_upload(
                substs=substs,
                paths=paths,
                topsrcdir=tmp,
                topobjdir=tmp,
                ab_cd=ab_cd,
                extra_files=extra_files,
            )
            return [os.path.relpath(f, tmp).replace(os.sep, "/") for f in files]

    def test_mar_tools(self):
        files = self._files(
            _SUBSTS,
            present=["dist/host/bin/mar.exe", "dist/host/bin/mbsdiff.exe"],
        )

        self.assertEqual(files, ["dist/host/bin/mar.exe", "dist/host/bin/mbsdiff.exe"])

    def test_mar_tools_other_locale(self):
        files = self._files(
            _SUBSTS,
            ab_cd="de",
            present=["dist/host/bin/mar.exe", "dist/host/bin/mbsdiff.exe"],
        )

        self.assertEqual(files, [])

    def test_installers_other_locale(self):
        files = self._files(
            _SUBSTS,
            ab_cd="de",
            present=[
                "browser/installer/windows/l10ngen/setup.exe",
                "browser/installer/windows/l10ngen/setup-stub.exe",
            ],
        )

        self.assertEqual(
            files,
            [
                "browser/installer/windows/l10ngen/setup.exe",
                "browser/installer/windows/l10ngen/setup-stub.exe",
            ],
        )

    def test_coverage(self):
        substs = {**_SUBSTS, "MOZ_CODE_COVERAGE": "1"}
        files = self._files(
            substs,
            present=[
                "dist/firefox-156.0a1.en-US.win64.code-coverage-gcno.zip",
                "chrome-map.json",
            ],
        )

        self.assertEqual(
            files,
            [
                "dist/firefox-156.0a1.en-US.win64.code-coverage-gcno.zip",
                "chrome-map.json",
            ],
        )

    def test_mozsearch(self):
        substs = {**_SUBSTS, "ENABLE_MOZSEARCH_PLUGIN": "1"}
        files = self._files(
            substs,
            present=[
                "dist/firefox-156.0a1.en-US.win64.mozsearch-index.zip",
                "dist/firefox-156.0a1.en-US.win64.mozsearch-distinclude.map",
            ],
        )

        self.assertEqual(
            files,
            [
                "dist/firefox-156.0a1.en-US.win64.mozsearch-index.zip",
                "dist/firefox-156.0a1.en-US.win64.mozsearch-distinclude.map",
            ],
        )

    def test_coverage_and_mozsearch(self):
        substs = {**_SUBSTS, "MOZ_CODE_COVERAGE": "1", "ENABLE_MOZSEARCH_PLUGIN": "1"}
        files = self._files(substs, present=["chrome-map.json"])

        self.assertEqual(files, ["chrome-map.json"])

    def test_java_index(self):
        substs = {
            **_SUBSTS,
            "ENABLE_MOZSEARCH_PLUGIN": "1",
            "MOZ_BUILD_APP": "mobile/android",
        }
        files = self._files(
            substs,
            present=["dist/firefox-156.0a1.en-US.win64.mozsearch-java-index.zip"],
        )

        self.assertEqual(
            files, ["dist/firefox-156.0a1.en-US.win64.mozsearch-java-index.zip"]
        )

    def test_stub_installer(self):
        stub = "dist/firefox-156.0a1.en-US.win64.installer-stub.exe"
        self.assertEqual(self._files(_SUBSTS, present=[stub]), [])

        substs = {**_SUBSTS, "MOZ_STUB_INSTALLER": "1"}
        self.assertEqual(self._files(substs, present=[stub]), [stub])

    def test_build_metadata(self):
        files = self._files(
            _SUBSTS,
            present=["toolchains.json", "sbom.json", "config.status", "licenses.json"],
        )

        self.assertEqual(
            files, ["toolchains.json", "sbom.json", "config.status", "licenses.json"]
        )

    def test_update_framework_archive(self):
        archive = "dist/firefox-156.0a1.en-US.win64.update_framework_artifacts.zip"
        self.assertEqual(self._files(_SUBSTS, present=[archive]), [])

        substs = {**_SUBSTS, "OS_ARCH": "Darwin"}
        self.assertEqual(self._files(substs, present=[archive]), [archive])

    def test_simple_package_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "dist")
            os.makedirs(dist)
            self._touch(os.path.join(dist, "target.zip"))
            paths = PackagePaths(
                substs=_SUBSTS, dist_dir=dist, ab_cd="de", simple_name="target"
            )
            files = files_to_upload(
                substs=_SUBSTS,
                paths=paths,
                topsrcdir=tmp,
                topobjdir=tmp,
                ab_cd="de",
            )

        self.assertEqual(files, [mozpath.join(dist, "target.zip")])

    def test_extra_files(self):
        files = self._files(
            _SUBSTS,
            present=["dist/host/bin/mar.exe", "dist/extra/thing.txt"],
            extra_files=["extra/thing.txt"],
        )

        self.assertEqual(
            files,
            [
                "dist/extra/thing.txt",
                "dist/host/bin/mar.exe",
            ],
        )

    def test_extra_files_missing(self):
        files = self._files(_SUBSTS, present=[], extra_files=["extra/absent.txt"])

        self.assertEqual(files, [])

    def test_langpack(self):
        langpack = "dist/win64/xpi/firefox-156.0a1.de.langpack.xpi"
        self.assertEqual(
            self._files(_SUBSTS, ab_cd="de", present=[langpack]), [langpack]
        )


if __name__ == "__main__":
    mozunit.main()
