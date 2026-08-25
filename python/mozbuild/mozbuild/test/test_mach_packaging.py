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

_CATEGORIES = ("build", "post-build", "misc", "testing", "devenv", "build-dev")


def _load_mach_commands():
    from mach.registrar import Registrar

    for category in _CATEGORIES:
        if category not in Registrar.categories:
            Registrar.register_category(category, category, category)

    from mozbuild import mach_commands

    return mach_commands


mach_commands = _load_mach_commands()

from mozbuild.action import package_application as package_application_action  # noqa

_INVOCATION_ENV = (
    "RUN_FIND_DUPES",
    "RUN_MOZHARNESS_ZIP",
    "UPLOAD_DIR",
    "MOZ_FETCHES_DIR",
    "MOZ_CHROME_MULTILOCALE",
    "DESTDIR",
    "MOZ_PKG_DIR",
    "MOZ_SIMPLE_PACKAGE_NAME",
)


@contextlib.contextmanager
def _environ(values=None):
    values = values or {}
    with mock.patch.dict(os.environ, values):
        for name in _INVOCATION_ENV:
            if name not in values:
                os.environ.pop(name, None)
        yield


_SUBSTS = {
    "MOZ_BUILD_APP": "browser",
    "MOZ_PKG_APPNAME": "firefox",
    "MOZ_PKG_VERSION": "156.0a1",
    "MOZ_PKG_PLATFORM": "linux-x86_64",
    "PKG_SUFFIX": ".tar.xz",
    "MOZ_PKG_DIR_DEFAULT": "firefox",
    "JSSHELL_NAME": "jsshell-linux-x86_64.zip",
    "MOZ_PKG_BINPATH": "bin",
    "MOZ_PKG_RESPATH": "bin",
    "MOZ_PACKAGER_FORMAT": "omni",
    "MOZ_PKG_FORMAT": "XZ",
    "MOZ_PKG_MANIFEST": "/src/browser/installer/package-manifest.in",
    "MOZ_PKG_REMOVALS": "/src/browser/installer/removed-files.in",
    "MOZ_PKG_ALLOWED_DUPES": ["/src/browser/installer/allowed-dupes.mn"],
    "MOZ_CHROME_LOCALE_ENTRIES": ["@RESPATH@/browser/chrome/"],
    "PACKAGE_MANIFEST_DEFINES": "-DMOZ_APP_NAME=firefox",
    "PACKAGE_TOOLKIT_DEFINES": "-DDLL_SUFFIX=.so",
    "ACDEFINES": "-DXP_LINUX=1",
    "NO_PKG_FILES": ["core", "nsinstall"],
    "MOZ_PACKAGER_MINIFY": "1",
    "JAR_COMPRESSION": "none",
    "TAR": "tar",
    "OS_ARCH": "Linux",
    "OS_TARGET": "Linux",
    "MOZ_WIDGET_TOOLKIT": "gtk",
    "MOZ_AUTOMATION": "",
    "MOZ_UI_LOCALE": "en-US",
}


def _context(substs=None, mozconfig=None, **overrides):
    return mock.Mock(
        substs=dict(substs if substs is not None else _SUBSTS),
        topsrcdir="/src",
        topobjdir="/obj",
        distdir="/obj/dist",
        mozconfig=mozconfig if mozconfig is not None else {"make_extra": []},
        **overrides,
    )


@contextlib.contextmanager
def _routed():
    with contextlib.ExitStack() as es:
        es.enter_context(_environ())
        yield {
            "application": es.enter_context(
                mock.patch.object(mach_commands, "_package_application", return_value=0)
            ),
        }


class TestLegacySwitch(unittest.TestCase):
    def test_legacy(self):
        substs = {**_SUBSTS, "MOZ_USE_LEGACY_PACKAGING": "1"}
        self.assertTrue(mach_commands._use_legacy_packaging(_context(substs)))

    def test_unset(self):
        self.assertFalse(mach_commands._use_legacy_packaging(_context()))


class TestPackageApplicationOptions(unittest.TestCase):
    def _options(self, substs=None, environ=None, **overrides):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return 0

        with _environ(environ):
            with mock.patch.object(
                package_application_action, "package_application", side_effect=capture
            ):
                rc = mach_commands._package_application(_context(substs), **overrides)
        return rc, seen

    def test_options(self):
        rc, options = self._options()

        self.assertEqual(rc, 0)
        self.assertEqual(options["mode"], "package")
        self.assertEqual(
            options["manifest"], "/src/browser/installer/package-manifest.in"
        )

    def test_overrides(self):
        _, options = self._options(mode="stage", pkg_dir="snap/source")

        self.assertEqual(options["mode"], "stage")
        self.assertEqual(options["pkg_dir"], "snap/source")

    def test_pkg_dir_override(self):
        _, options = self._options(
            environ={"MOZ_PKG_DIR": "custom"}, pkg_dir="snap/source"
        )
        self.assertEqual(options["pkg_dir"], "snap/source")


class TestUploadExtraFiles(unittest.TestCase):
    def test_extra_files(self):
        self.assertEqual(
            mach_commands._upload_extra_files({
                "MOZ_UPLOAD_EXTRA_FILES": "one.txt  two.txt\nthree.txt"
            }),
            ["one.txt", "two.txt", "three.txt"],
        )

    def test_extra_files_empty(self):
        self.assertEqual(mach_commands._upload_extra_files({}), [])
        self.assertEqual(
            mach_commands._upload_extra_files({"MOZ_UPLOAD_EXTRA_FILES": ""}), []
        )

    def test_extra_files_upload(self):
        with mock.patch.dict(os.environ, {"MOZ_UPLOAD_EXTRA_FILES": "one.txt"}):
            with mock.patch(
                "mozbuild.action.package_upload.package_upload", return_value=0
            ) as upload:
                rc = mach_commands._package_upload(upload_path="/upload")

        self.assertEqual(rc, 0)
        self.assertEqual(upload.call_args.kwargs["extra_files"], ["one.txt"])


class TestPackageCommand(unittest.TestCase):
    def test_package_application(self):
        context = _context()
        with _routed() as calls:
            with mock.patch.object(mach_commands, "_print_package_name"):
                rc = mach_commands.package(context)

        self.assertEqual(rc, 0)
        self.assertEqual(context._run_make.call_count, 0)
        self.assertEqual(calls["application"].call_args.args[0], context)

    def test_legacy(self):
        context = _context({**_SUBSTS, "MOZ_USE_LEGACY_PACKAGING": "1"})
        context._run_make.return_value = 0
        with _routed() as calls:
            with mock.patch.object(mach_commands, "_print_package_name"):
                rc = mach_commands.package(context)

        self.assertEqual(rc, 0)
        self.assertEqual(calls["application"].call_count, 0)
        self.assertEqual(context._run_make.call_args.kwargs["target"], "package")


class TestInstallCommand(unittest.TestCase):
    @contextlib.contextmanager
    def _desktop(self):
        with mock.patch.object(mach_commands.conditions, "is_android", return_value=0):
            with mock.patch.object(
                mach_commands.conditions, "is_jsshell", return_value=False
            ):
                with mock.patch.object(
                    mach_commands.conditions, "is_ios", return_value=False
                ):
                    yield

    def test_package_application(self):
        context = _context()
        with self._desktop():
            with _routed() as calls:
                rc = mach_commands.install(context)

        self.assertEqual(rc, 0)
        self.assertEqual(context._run_make.call_count, 0)
        self.assertEqual(calls["application"].call_args.kwargs["mode"], "install")

    def test_legacy(self):
        context = _context({**_SUBSTS, "MOZ_USE_LEGACY_PACKAGING": "1"})
        context._run_make.return_value = 0
        with self._desktop():
            with _routed() as calls:
                rc = mach_commands.install(context)

        self.assertEqual(rc, 0)
        self.assertEqual(calls["application"].call_count, 0)
        self.assertEqual(context._run_make.call_args.kwargs["target"], "install")

    def test_js_standalone(self):
        context = _context({**_SUBSTS, "JS_STANDALONE": "1"})
        context._run_make.return_value = 0
        with self._desktop():
            with _routed() as calls:
                rc = mach_commands.install(context)

        self.assertEqual(rc, 0)
        self.assertEqual(calls["application"].call_count, 0)
        self.assertEqual(context._run_make.call_args.kwargs["target"], "install")

    def test_destdir(self):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return 0

        with self._desktop():
            with _environ({"DESTDIR": "/tmp/stage"}):
                with mock.patch.object(
                    package_application_action,
                    "package_application",
                    side_effect=capture,
                ):
                    rc = mach_commands.install(_context())

        self.assertEqual(rc, 0)
        self.assertEqual(seen["mode"], "install")
        self.assertEqual(seen["destdir"], "/tmp/stage")


class TestSingleLocaleUpload(unittest.TestCase):
    def test_simple_package_name(self):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return 0

        with mock.patch(
            "mozbuild.action.package_upload.package_upload", side_effect=capture
        ):
            rc = mach_commands._package_upload(
                upload_path="/upload/de",
                ab_cd="de",
                simple_package_name="target",
            )

        self.assertEqual(rc, 0)
        self.assertEqual(seen["simple_package_name"], "target")
        self.assertEqual(seen["ab_cd"], "de")

    def test_locales(self):
        context = _context({**_SUBSTS, "BUILD_BACKENDS": ["RecursiveMake"]})
        with contextlib.ExitStack() as es:
            es.enter_context(_environ())
            es.enter_context(mock.patch.object(mach_commands, "ensure_l10n_central"))
            es.enter_context(
                mock.patch(
                    "mozbuild.action.l10n_repackage.uses_local_package",
                    return_value=False,
                )
            )
            upload = es.enter_context(
                mock.patch.object(mach_commands, "_package_upload", return_value=0)
            )
            rc = mach_commands.repackage_single_locales(
                context, locales=["fr", "de"], dest="/upload"
            )

        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.kwargs for call in upload.call_args_list],
            [
                {
                    "upload_path": mozpath.join(os.path.abspath("/upload"), locale),
                    "ab_cd": locale,
                    "simple_package_name": "target",
                }
                for locale in ("de", "fr")
            ],
        )


class TestSnapRepackage(unittest.TestCase):
    def _run(self, staging_result):
        with tempfile.TemporaryDirectory() as tmp:
            context = _context(bindir=tmp)
            with mock.patch.object(
                mach_commands.conditions, "is_firefox", return_value=True
            ):
                with _routed() as calls:
                    calls["application"].return_value = staging_result
                    rc = mach_commands.repackage_snap(context, snapcraft="snapcraft")
        return rc, calls

    def test_stage_failure(self):
        rc, calls = self._run(3)

        self.assertEqual(rc, 3)
        self.assertEqual(calls["application"].call_args.kwargs["mode"], "stage")


class TestMultiLocaleCommand(unittest.TestCase):
    @contextlib.contextmanager
    def _prerequisites(self):
        with mock.patch.object(mach_commands, "ensure_l10n_central"):
            yield

    def _substs(self, **overrides):
        return {
            **_SUBSTS,
            "BUILD_BACKENDS": ["RecursiveMake"],
            **overrides,
        }

    def test_locales(self):
        context = _context(self._substs())
        with self._prerequisites():
            with _routed() as calls:
                rc = mach_commands.package_l10n(context, locales=["de", "fr"])

        self.assertEqual(rc, 0)
        kwargs = calls["application"].call_args.kwargs
        self.assertEqual(kwargs["locales"], ["de", "fr"])
        self.assertIsNone(kwargs["ab_cd"])

    def test_android(self):
        context = _context(self._substs(MOZ_BUILD_APP="mobile/android"))
        with self._prerequisites():
            with _routed() as calls:
                mach_commands.package_l10n(context, locales=["de"])

        self.assertEqual(calls["application"].call_args.kwargs["ab_cd"], "multi")

    def test_en_us(self):
        context = _context(self._substs())
        with self._prerequisites():
            with _routed() as calls:
                mach_commands.package_l10n(context, locales=["en-US", "de"])

        self.assertEqual(calls["application"].call_args.kwargs["locales"], ["de"])


if __name__ == "__main__":
    mozunit.main()
