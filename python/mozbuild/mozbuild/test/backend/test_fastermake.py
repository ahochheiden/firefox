# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import mozpack.path as mozpath
from mozpack.copier import FileRegistry
from mozpack.manifests import InstallManifest
from mozunit import main

from mozbuild.backend.fastermake import FasterMakeBackend
from mozbuild.test.backend.common import BackendTester


class TestFasterMakeBackend(BackendTester):
    def test_basic(self):
        """Ensure the FasterMakeBackend works without error."""
        env = self._consume("stub0", FasterMakeBackend)
        self.assertTrue(
            os.path.exists(mozpath.join(env.topobjdir, "backend.FasterMakeBackend"))
        )
        self.assertTrue(
            os.path.exists(mozpath.join(env.topobjdir, "backend.FasterMakeBackend.in"))
        )

    def test_final_target_files_wildcard(self):
        """Ensure that wildcards in FINAL_TARGET_FILES work properly."""
        env = self._consume("final-target-files-wildcard", FasterMakeBackend)
        m = InstallManifest(
            path=mozpath.join(env.topobjdir, "faster", "install_dist_bin")
        )
        self.assertEqual(len(m), 1)
        reg = FileRegistry()
        m.populate_registry(reg)
        expected = [("foo/bar.xyz", "bar.xyz"), ("foo/foo.xyz", "foo.xyz")]
        actual = [(path, mozpath.relpath(f.path, env.topsrcdir)) for (path, f) in reg]
        self.assertEqual(expected, actual)

    def test_final_target_files_directory_wildcard(self):
        env = self._consume("final-target-files-directory-wildcard", FasterMakeBackend)
        m = InstallManifest(
            path=mozpath.join(env.topobjdir, "faster", "install_dist_bin")
        )
        reg = FileRegistry()
        m.populate_registry(reg)

        self.assertEqual(
            sorted(reg.paths()),
            ["foo/a/config.json", "foo/a/x.js", "foo/b/config.json", "foo/b/y.js"],
        )

    def test_generated_files_default_target(self):
        env = self._consume("generated-files-default-target", FasterMakeBackend)

        with open(mozpath.join(env.topobjdir, "faster", "Makefile")) as fh:
            lines = [l.rstrip() for l in fh]
        self.assertIn("default: $(TOPOBJDIR)/$(MDDEPDIR)/foo.c.stub", lines)
        self.assertEqual([l for l in lines if "bar.c" in l], [])
        self.assertIn("install-dist/bin: $(TOPOBJDIR)/baz.js", lines)

    def test_pp_files_extra_deps(self):
        env = self._consume("pp-files-extra-deps-generated", FasterMakeBackend)

        with open(mozpath.join(env.topobjdir, "faster", "Makefile")) as fh:
            lines = [l.rstrip() for l in fh]
        self.assertIn("install-dist/bin: $(TOPOBJDIR)/generated-header.h", lines)


class TestJarLocaleConstraint(BackendTester):
    """A `locales translated:` section reaches the backend only for a repack."""

    def _install_manifests(self, locale):
        env = self._get_environment(
            "jar-locale-constraint", substs={"MOZ_UI_LOCALE": locale}
        )
        self._consume("jar-locale-constraint", FasterMakeBackend, env=env)
        faster = mozpath.join(env.topobjdir, "faster")
        return {
            name: InstallManifest(path=mozpath.join(faster, name))
            for name in os.listdir(faster)
            if name.startswith("install_")
        }

    def _destinations(self, manifests, name):
        if name not in manifests:
            return []
        reg = FileRegistry()
        manifests[name].populate_registry(reg)
        return sorted(path for path, _ in reg)

    def test_the_unconstrained_section_is_present_for_the_source_locale(self):
        manifests = self._install_manifests("en-US")

        self.assertIn(
            "chrome/browser/content/browser/always.txt",
            self._destinations(manifests, "install_dist_bin"),
        )

    def test_the_constrained_section_is_absent_for_the_source_locale(self):
        manifests = self._install_manifests("en-US")

        self.assertEqual(
            self._destinations(manifests, "install_dist_bin_localization"), []
        )

    def test_the_constrained_section_is_present_for_another_locale(self):
        manifests = self._install_manifests("de")

        self.assertIn(
            "de/testing/only.ftl",
            self._destinations(manifests, "install_dist_bin_localization"),
        )


if __name__ == "__main__":
    main()
