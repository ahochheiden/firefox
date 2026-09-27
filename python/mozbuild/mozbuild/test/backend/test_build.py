# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import shutil
import sys
import time
import unittest
from contextlib import contextmanager
from tempfile import mkdtemp

import buildconfig
import mozpack.path as mozpath
from mach.util import get_state_dir
from mozfile import which
from mozpack.files import FileFinder
from mozunit import main

from mozbuild.backend import get_backend_class
from mozbuild.backend.configenvironment import ConfigEnvironment
from mozbuild.backend.fastermake import FasterMakeBackend
from mozbuild.backend.recursivemake import RecursiveMakeBackend
from mozbuild.base import MozbuildObject
from mozbuild.dirutils import ensureParentDir
from mozbuild.frontend.emitter import TreeMetadataEmitter
from mozbuild.frontend.reader import BuildReader


def make_path():
    try:
        return buildconfig.substs["GMAKE"]
    except KeyError:
        fetches_dir = os.environ.get("MOZ_FETCHES_DIR")
        extra_search_dirs = (os.path.join(get_state_dir(), "mozmake"),)
        if fetches_dir:
            extra_search_dirs += (os.path.join(fetches_dir, "mozmake"),)
        # Fallback for when running the test without an objdir.
        for name in ("gmake", "make", "mozmake", "gnumake", "mingw32-make"):
            path = which(name, extra_search_dirs=extra_search_dirs)
            if path:
                return path


BASE_SUBSTS = [
    ("PYTHON", mozpath.normsep(sys.executable)),
    ("PYTHON3", mozpath.normsep(sys.executable)),
    ("MOZ_UI_LOCALE", "en-US"),
    ("GMAKE", make_path()),
]


class TestBuild(unittest.TestCase):
    def setUp(self):
        self._old_env = dict(os.environ)
        os.environ.pop("MOZCONFIG", None)
        os.environ.pop("MOZ_OBJDIR", None)
        os.environ.pop("MOZ_PGO", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    @contextmanager
    def do_test_backend(self, *backends, **kwargs):
        # Create the objdir in the srcdir to ensure that they share
        # the same drive on Windows.
        topobjdir = mkdtemp(dir=buildconfig.topsrcdir)
        try:
            config = ConfigEnvironment(buildconfig.topsrcdir, topobjdir, **kwargs)
            reader = BuildReader(config)
            emitter = TreeMetadataEmitter(config)
            moz_build = mozpath.join(config.topsrcdir, "test.mozbuild")
            definitions = list(emitter.emit(reader.read_mozbuild(moz_build, config)))
            for backend in backends:
                backend(config).consume(definitions)

            yield config
        except Exception:
            raise
        finally:
            if not os.environ.get("MOZ_NO_CLEANUP"):
                shutil.rmtree(topobjdir)

    @contextmanager
    def line_handler(self):
        lines = []

        def handle_make_line(line):
            lines.append(line)

        try:
            yield handle_make_line
        except Exception:
            print("\n".join(lines))
            raise

        if os.environ.get("MOZ_VERBOSE_MAKE"):
            print("\n".join(lines))

    def test_recursive_make(self):
        substs = list(BASE_SUBSTS)
        with self.do_test_backend(RecursiveMakeBackend, substs=substs) as config:
            build = MozbuildObject(config.topsrcdir, None, None, config.topobjdir)
            build._config_environment = config
            overrides = [
                "install_manifest_depends=",
                "TEST_MOZBUILD=1",
            ]
            with self.line_handler() as handle_make_line:
                build._run_make(
                    directory=config.topobjdir,
                    target=overrides,
                    silent=False,
                    line_handler=handle_make_line,
                )

            self.validate(config)

    NONDEFAULT_DIR = "python/mozbuild/mozbuild/test/backend/data/build"

    def _nondefault_stamp(self, config, role):
        return mozpath.join(
            config.topobjdir,
            self.NONDEFAULT_DIR,
            f"nondefault-{role}",
            f"{role}.stamp",
        )

    def _nondefault_runs(self, config, role):
        """How many times a generator ran.

        Counted from a side effect rather than a timestamp, because a whole
        build here takes less time than the resolution Make compares at.
        """
        runs = self._nondefault_stamp(config, role) + ".runs"
        if not os.path.exists(runs):
            return 0
        with open(runs) as fh:
            return len(fh.readlines())

    def _nondefault_stub(self, config, role):
        return mozpath.join(
            config.topobjdir,
            self.NONDEFAULT_DIR,
            f"nondefault-{role}",
            ".deps",
            f"{role}.stamp.stub",
        )

    def _age_nondefault(self, config):
        """Move the built artifacts into the past, producer before consumer.

        A whole build here takes less time than the resolution Make compares
        timestamps at, so without this a rebuilt file can look no newer than
        the one that consumed it. Ageing the producer further back than the
        consumer also makes "the producer ran but its output did not change"
        distinguishable from "its output changed".
        """
        now = time.time()
        for role, age in (("producer", 20), ("consumer", 10)):
            for path in (
                self._nondefault_stamp(config, role),
                self._nondefault_stub(config, role),
            ):
                os.utime(path, (now - age, now - age))

    def _make_nondefault_consumer(self, build, config):
        with self.line_handler() as handle_make_line:
            build._run_make(
                directory=config.topobjdir,
                target=[
                    "install_manifest_depends=",
                    "TEST_MOZBUILD=1",
                    mozpath.join(
                        self.NONDEFAULT_DIR, "nondefault-consumer/consumer.stamp"
                    ),
                ],
                silent=False,
                line_handler=handle_make_line,
            )

    def test_generated_files_default_target_on_request(self):
        substs = list(BASE_SUBSTS)
        with self.do_test_backend(RecursiveMakeBackend, substs=substs) as config:
            build = MozbuildObject(config.topsrcdir, None, None, config.topobjdir)
            build._config_environment = config
            producer_input = mozpath.join(
                config.topobjdir,
                self.NONDEFAULT_DIR,
                "nondefault-producer/producer-input",
            )
            os.makedirs(os.path.dirname(producer_input), exist_ok=True)
            with open(producer_input, "w", newline="") as fh:
                fh.write("first\n")

            def runs():
                return tuple(
                    self._nondefault_runs(config, w) for w in ("producer", "consumer")
                )

            # Requesting the consumer builds the producer first.
            self._make_nondefault_consumer(build, config)

            for role in ("producer", "consumer"):
                self.assertTrue(
                    os.path.exists(self._nondefault_stamp(config, role)), role
                )
            with open(self._nondefault_stamp(config, "producer")) as fh:
                self.assertEqual(fh.read(), "first\n")
            self.assertEqual(runs(), (1, 1))

            # Asking again runs neither generator.
            self._make_nondefault_consumer(build, config)
            self.assertEqual(runs(), (1, 1))

            # A changed producer input rebuilds both.
            self._age_nondefault(config)
            with open(producer_input, "w", newline="") as fh:
                fh.write("second\n")
            self._make_nondefault_consumer(build, config)

            with open(self._nondefault_stamp(config, "producer")) as fh:
                self.assertEqual(fh.read(), "second\n")
            self.assertEqual(runs(), (2, 2))

            # A producer run that leaves its output unchanged does not
            # rebuild the consumer.
            self._age_nondefault(config)
            os.utime(producer_input)
            self._make_nondefault_consumer(build, config)
            self.assertEqual(runs(), (3, 2))

    def test_generated_files_default_target_not_built(self):
        substs = list(BASE_SUBSTS)
        with self.do_test_backend(RecursiveMakeBackend, substs=substs) as config:
            build = MozbuildObject(config.topsrcdir, None, None, config.topobjdir)
            build._config_environment = config

            with self.line_handler() as handle_make_line:
                build._run_make(
                    directory=config.topobjdir,
                    target=[
                        "install_manifest_depends=",
                        "TEST_MOZBUILD=1",
                    ],
                    silent=False,
                    line_handler=handle_make_line,
                )

            for role in ("producer", "consumer"):
                self.assertFalse(
                    os.path.exists(self._nondefault_stamp(config, role)), role
                )

    def test_faster_recursive_make(self):
        substs = list(BASE_SUBSTS) + [
            ("BUILD_BACKENDS", "FasterMake+RecursiveMake"),
        ]
        with self.do_test_backend(
            get_backend_class("FasterMake+RecursiveMake"), substs=substs
        ) as config:
            buildid = mozpath.join(config.topobjdir, "config", "buildid")
            ensureParentDir(buildid)
            with open(buildid, "w") as fh:
                fh.write("20100101012345\n")

            build = MozbuildObject(config.topsrcdir, None, None, config.topobjdir)
            build._config_environment = config
            overrides = [
                "install_manifest_depends=",
                "TEST_MOZBUILD=1",
            ]
            with self.line_handler() as handle_make_line:
                build._run_make(
                    directory=config.topobjdir,
                    target=overrides,
                    silent=False,
                    line_handler=handle_make_line,
                )

            self.validate(config)

    def test_faster_make(self):
        substs = list(BASE_SUBSTS) + [
            ("MOZ_BUILD_APP", "dummy_app"),
            ("MOZ_WIDGET_TOOLKIT", "dummy_widget"),
        ]
        with self.do_test_backend(
            RecursiveMakeBackend, FasterMakeBackend, substs=substs
        ) as config:
            buildid = mozpath.join(config.topobjdir, "config", "buildid")
            ensureParentDir(buildid)
            with open(buildid, "w") as fh:
                fh.write("20100101012345\n")

            build = MozbuildObject(config.topsrcdir, None, None, config.topobjdir)
            build._config_environment = config
            overrides = [
                "TEST_MOZBUILD=1",
            ]
            with self.line_handler() as handle_make_line:
                build._run_make(
                    directory=mozpath.join(config.topobjdir, "faster"),
                    target=overrides,
                    silent=False,
                    line_handler=handle_make_line,
                )

            self.validate(config)

    def validate(self, config):
        self.maxDiff = None
        test_path = mozpath.join(
            "$SRCDIR",
            "python",
            "mozbuild",
            "mozbuild",
            "test",
            "backend",
            "data",
            "build",
        )

        result = {
            p: f.open().read().decode()
            for p, f in FileFinder(mozpath.join(config.topobjdir, "dist"))
        }
        self.assertTrue(len(result))
        self.assertEqual(
            result,
            {
                "bin/baz.ini": "baz.ini: FOO is foo\n",
                "bin/child/bar.ini": "bar.ini\n",
                "bin/child2/foo.css": "foo.css: FOO is foo\n",
                "bin/child2/qux.ini": "qux.ini: BAR is not defined\n",
                "bin/chrome.manifest": "manifest chrome/foo.manifest\n"
                "manifest components/components.manifest\n",
                "bin/chrome/foo.manifest": "content bar foo/child/\n"
                "content foo foo/\n"
                "override chrome://foo/bar.svg#hello "
                "chrome://bar/bar.svg#hello\n",
                "bin/chrome/foo/bar.js": "bar.js\n",
                "bin/chrome/foo/child/baz.sys.mjs": '//@line 2 "%s/baz.sys.mjs"\nbaz.sys.mjs: FOO is foo\n'
                % (test_path),
                "bin/chrome/foo/child/hoge.js": '//@line 2 "%s/bar.js"\nbar.js: FOO is foo\n'
                % (test_path),
                "bin/chrome/foo/foo.css": "foo.css: FOO is foo\n",
                "bin/chrome/foo/foo.js": "foo.js\n",
                "bin/chrome/foo/qux.js": "bar.js\n",
                "bin/components/bar.js": '//@line 2 "%s/bar.js"\nbar.js: FOO is foo\n'
                % (test_path),
                "bin/components/components.manifest": "component {foo} foo.js\ncomponent {bar} bar.js\n",  # NOQA: E501
                "bin/components/foo.js": "foo.js\n",
                "bin/defaults/pref/prefs.js": "prefs.js\n",
                "bin/foo.ini": "foo.ini\n",
                "bin/modules/baz.sys.mjs": '//@line 2 "%s/baz.sys.mjs"\nbaz.sys.mjs: FOO is foo\n'
                % (test_path),
                "bin/modules/child/bar.sys.mjs": "bar.sys.mjs\n",
                "bin/modules/child2/qux.sys.mjs": '//@line 4 "%s/qux.sys.mjs"\nqux.sys.mjs: BAR is not defined\n'  # NOQA: E501
                % (test_path),
                "bin/modules/foo.sys.mjs": "foo.sys.mjs\n",
                "bin/res/resource": "resource\n",
                "bin/res/child/resource2": "resource2\n",
                "bin/app/baz.ini": "baz.ini: FOO is bar\n",
                "bin/app/child/bar.ini": "bar.ini\n",
                "bin/app/child2/qux.ini": "qux.ini: BAR is defined\n",
                "bin/app/chrome.manifest": "manifest chrome/foo.manifest\n"
                "manifest components/components.manifest\n",
                "bin/app/chrome/foo.manifest": "content bar foo/child/\n"
                "content foo foo/\n"
                "override chrome://foo/bar.svg#hello "
                "chrome://bar/bar.svg#hello\n",
                "bin/app/chrome/foo/bar.js": "bar.js\n",
                "bin/app/chrome/foo/child/baz.sys.mjs": '//@line 2 "%s/baz.sys.mjs"\nbaz.sys.mjs: FOO is bar\n'
                % (test_path),
                "bin/app/chrome/foo/child/hoge.js": '//@line 2 "%s/bar.js"\nbar.js: FOO is bar\n'
                % (test_path),
                "bin/app/chrome/foo/foo.css": "foo.css: FOO is bar\n",
                "bin/app/chrome/foo/foo.js": "foo.js\n",
                "bin/app/chrome/foo/qux.js": "bar.js\n",
                "bin/app/components/bar.js": '//@line 2 "%s/bar.js"\nbar.js: FOO is bar\n'
                % (test_path),
                "bin/app/components/components.manifest": "component {foo} foo.js\ncomponent {bar} bar.js\n",  # NOQA: E501
                "bin/app/components/foo.js": "foo.js\n",
                "bin/app/defaults/preferences/prefs.js": "prefs.js\n",
                "bin/app/foo.css": "foo.css: FOO is bar\n",
                "bin/app/foo.ini": "foo.ini\n",
                "bin/app/modules/baz.sys.mjs": '//@line 2 "%s/baz.sys.mjs"\nbaz.sys.mjs: FOO is bar\n'
                % (test_path),
                "bin/app/modules/child/bar.sys.mjs": "bar.sys.mjs\n",
                "bin/app/modules/child2/qux.sys.mjs": '//@line 2 "%s/qux.sys.mjs"\nqux.sys.mjs: BAR is defined\n'  # NOQA: E501
                % (test_path),
                "bin/app/modules/foo.sys.mjs": "foo.sys.mjs\n",
            },
        )


if __name__ == "__main__":
    main()
