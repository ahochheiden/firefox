# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import contextlib
import os
import sys
import tempfile
import unittest
from unittest import mock

import mozunit

from mozbuild.action import package_prepare

_DEFINES = {"ALLDEFINES": {}}


@contextlib.contextmanager
def _patched():
    test_archive = mock.MagicMock()
    with contextlib.ExitStack() as es:
        es.enter_context(
            mock.patch.dict(sys.modules, {"mozbuild.action.test_archive": test_archive})
        )
        yield {
            "find_dupes": es.enter_context(
                mock.patch.object(package_prepare.find_dupes, "main", return_value=0)
            ),
            "test_archive": test_archive.main,
            "zip": es.enter_context(
                mock.patch.object(package_prepare.zip_action, "main")
            ),
            "libmegazord": es.enter_context(
                mock.patch.object(
                    package_prepare.package_libmegazord_artifacts,
                    "main",
                    return_value=0,
                )
            ),
            "subprocess": es.enter_context(
                mock.patch.object(package_prepare.subprocess, "call", return_value=0)
            ),
        }


class TestPackagePrepare(unittest.TestCase):
    def test_nothing_requested(self):
        with _patched() as calls:
            self.assertEqual(package_prepare.package_prepare(staged_dir="staged"), 0)

        for name, call in calls.items():
            self.assertEqual(call.call_count, 0, name)

    def test_find_dupes(self):
        with _patched() as calls:
            rc = package_prepare.package_prepare(
                staged_dir="dist/firefox",
                check_dupes=True,
                defines=["MOZ_APP_NAME=firefox", "MOZ_DEBUG"],
                warn_dupes=True,
                allowed_dupes=["browser/dupes.txt", "suite/dupes.txt"],
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            calls["find_dupes"].call_args.args[0],
            [
                "-DMOZ_APP_NAME=firefox",
                "-DMOZ_DEBUG",
                "-w",
                "-f",
                "browser/dupes.txt",
                "-f",
                "suite/dupes.txt",
                "dist/firefox",
            ],
        )

    def test_find_dupes_default(self):
        with _patched() as calls:
            package_prepare.package_prepare(staged_dir="staged", check_dupes=True)

        self.assertEqual(calls["find_dupes"].call_args.args[0], ["staged"])

    def test_find_dupes_failure(self):
        with _patched() as calls:
            calls["find_dupes"].return_value = 1
            rc = package_prepare.package_prepare(
                staged_dir="staged",
                check_dupes=True,
                mozharness_zip="dist/mozharness.zip",
            )

        self.assertEqual(rc, 1)
        self.assertEqual(calls["test_archive"].call_count, 0)

    def test_symbol_archives_missing(self):
        with _patched() as calls:
            rc = package_prepare.package_prepare(
                staged_dir="staged",
                symbol_archives=["upload/target.crashreporter-symbols.zip"],
                check_dupes=True,
            )

        self.assertEqual(rc, 1)
        self.assertEqual(calls["find_dupes"].call_count, 0)

    def test_symbol_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = os.path.join(tmp, "target.crashreporter-symbols.zip")
            with open(archive, "w") as f:
                f.write("")

            with _patched() as calls:
                rc = package_prepare.package_prepare(
                    staged_dir="staged", symbol_archives=[archive], check_dupes=True
                )

        self.assertEqual(rc, 0)
        self.assertEqual(calls["find_dupes"].call_count, 1)

    def test_mozharness_archive(self):
        with _patched() as calls:
            package_prepare.package_prepare(
                staged_dir="staged", mozharness_zip="dist/mozharness.zip"
            )

        self.assertEqual(
            calls["test_archive"].call_args.args[0],
            ["mozharness", "dist/mozharness.zip"],
        )

    def test_jsshell_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "jsshell-win64.zip")
            with open(output, "w") as f:
                f.write("stale")

            with _patched() as calls:
                package_prepare.package_prepare(
                    staged_dir="staged",
                    jsshell=[
                        os.path.join(tmp, "dist", "bin"),
                        output,
                        os.path.join(tmp, "jsshell-archive.list"),
                    ],
                )

            self.assertFalse(os.path.exists(output))

        self.assertEqual(
            calls["zip"].call_args.args[0],
            [
                "-C",
                os.path.join(tmp, "dist", "bin"),
                "--strip",
                os.path.abspath(output),
                "--files-from",
                os.path.join(tmp, "jsshell-archive.list"),
            ],
        )

    def test_jsshell_archive_missing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _patched() as calls:
                rc = package_prepare.package_prepare(
                    staged_dir="staged",
                    jsshell=[
                        tmp,
                        os.path.join(tmp, "absent.zip"),
                        os.path.join(tmp, "list"),
                    ],
                )

        self.assertEqual(rc, 0)
        self.assertEqual(calls["zip"].call_count, 1)

    def test_archives(self):
        with _patched() as calls:
            package_prepare.package_prepare(
                staged_dir="staged",
                archives=[
                    [
                        "/obj/config/makefiles/xpidl",
                        "/obj/dist/target.xpt.zip",
                        "*.xpt",
                    ],
                    [
                        "/obj/dist/update_framework_artifacts",
                        "/obj/dist/uf.zip",
                        "*.framework",
                    ],
                ],
            )

        self.assertEqual(
            [call.args[0] for call in calls["zip"].call_args_list],
            [
                [
                    "--error-if-empty",
                    "-C",
                    "/obj/config/makefiles/xpidl",
                    "/obj/dist/target.xpt.zip",
                    "*.xpt",
                ],
                [
                    "--error-if-empty",
                    "-C",
                    "/obj/dist/update_framework_artifacts",
                    "/obj/dist/uf.zip",
                    "*.framework",
                ],
            ],
        )

    def test_libmegazord_artifacts(self):
        with _patched() as calls:
            package_prepare.package_prepare(
                staged_dir="staged",
                libmegazord_artifacts=["/obj/dist/bin", "/obj/dist/megazord.zip"],
            )

        self.assertEqual(
            calls["libmegazord"].call_args.args[0],
            ["--dist-bin", "/obj/dist/bin", "/obj/dist/megazord.zip"],
        )

    def test_libmegazord_artifacts_failure(self):
        with _patched() as calls:
            calls["libmegazord"].return_value = 5
            rc = package_prepare.package_prepare(
                staged_dir="staged",
                libmegazord_artifacts=["/obj/dist/bin", "/obj/dist/megazord.zip"],
            )

        self.assertEqual(rc, 5)

    def test_sanitizer_dylib_rewrite(self):
        with _patched() as calls:
            package_prepare.package_prepare(
                staged_dir="staged",
                topsrcdir="/src",
                rewrite_sanitizer_dylib="/obj/dist/firefox/Nightly.app/Contents/MacOS",
            )

        argv = calls["subprocess"].call_args.args[0]
        self.assertEqual(argv[1], "/src/build/unix/rewrite_sanitizer_dylib.py")
        self.assertEqual(argv[2], "/obj/dist/firefox/Nightly.app/Contents/MacOS")

    def test_sanitizer_dylib_rewrite_failure(self):
        with _patched() as calls:
            calls["subprocess"].return_value = 2
            rc = package_prepare.package_prepare(
                staged_dir="staged",
                topsrcdir="/src",
                rewrite_sanitizer_dylib="/obj/dist/firefox",
                archives=[["/obj/a", "/obj/a.zip", "*"]],
            )

        self.assertEqual(rc, 2)
        self.assertEqual(calls["zip"].call_count, 0)

    def test_step_order(self):
        order = []
        with _patched() as calls:
            calls["find_dupes"].side_effect = lambda *a: order.append("dupes") or 0
            calls["test_archive"].side_effect = lambda *a: order.append("mozharness")
            calls["zip"].side_effect = lambda *a: order.append("zip")
            calls["libmegazord"].side_effect = lambda *a: order.append("libmegazord")
            calls["subprocess"].side_effect = lambda *a: order.append("rewrite") or 0

            package_prepare.package_prepare(
                staged_dir="staged",
                topsrcdir="/src",
                check_dupes=True,
                mozharness_zip="dist/mozharness.zip",
                rewrite_sanitizer_dylib="dist/firefox",
                archives=[["/obj/a", "/obj/a.zip", "*"]],
                libmegazord_artifacts=["/obj/dist/bin", "/obj/m.zip"],
            )

        self.assertEqual(
            order, ["dupes", "mozharness", "rewrite", "zip", "libmegazord"]
        )


class TestPackagePrepareMain(unittest.TestCase):
    def test_command_line(self):
        with _patched() as calls:
            rc = package_prepare.main([
                "--staged-dir",
                "dist/firefox",
                "--topsrcdir",
                "/src",
                "--find-dupes",
                "--warn-dupes",
                "--allowed-dupes",
                "allowed-dupes.txt",
                "-DMOZ_APP_NAME=firefox",
                "--archive",
                "/obj/xpidl",
                "/obj/dist/xpt.zip",
                "*.xpt",
                "--libmegazord-artifacts",
                "/obj/dist/bin",
                "/obj/dist/m.zip",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(
            calls["find_dupes"].call_args.args[0],
            [
                "-DMOZ_APP_NAME=firefox",
                "-w",
                "-f",
                "allowed-dupes.txt",
                "dist/firefox",
            ],
        )
        self.assertEqual(
            calls["zip"].call_args.args[0],
            ["--error-if-empty", "-C", "/obj/xpidl", "/obj/dist/xpt.zip", "*.xpt"],
        )
        self.assertEqual(
            calls["libmegazord"].call_args.args[0],
            ["--dist-bin", "/obj/dist/bin", "/obj/dist/m.zip"],
        )


class TestPackagePrepareReentrancy(unittest.TestCase):
    def _tree(self, root):
        staged = os.path.join(root, "staged")
        os.makedirs(staged)
        for name in ("one.txt", "two.txt"):
            with open(os.path.join(staged, name), "w") as f:
                f.write("identical")
        return staged

    def _allowlist(self, root, name, body):
        path = os.path.join(root, name)
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_find_dupes_allowlists_per_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = self._tree(tmp)
            permissive = self._allowlist(tmp, "allowed.txt", "one.txt\ntwo.txt\n")
            strict = self._allowlist(tmp, "none.txt", "# nothing\n")

            with mock.patch.object(
                package_prepare.find_dupes.buildconfig, "defines", _DEFINES
            ):
                first = package_prepare.package_prepare(
                    staged_dir=staged, check_dupes=True, allowed_dupes=[permissive]
                )
                second = package_prepare.package_prepare(
                    staged_dir=staged, check_dupes=True, allowed_dupes=[strict]
                )

        self.assertEqual(first, 0)
        self.assertEqual(second, 1)


if __name__ == "__main__":
    mozunit.main()
