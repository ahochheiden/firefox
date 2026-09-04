# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os

import mozpack.path as mozpath
from mozunit import main

from mozbuild.backend.recursivemake import RecursiveMakeBackend
from mozbuild.frontend.l10n_manifest import MANIFEST_VERSION
from mozbuild.test.backend.common import BackendTester


class TestL10nManifestBackendIntegration(BackendTester):
    def _context(self):
        env = self._consume("l10n-manifest", RecursiveMakeBackend)
        manifest_path = mozpath.join(env.topobjdir, "l10n-manifest.json")
        self.assertTrue(os.path.exists(manifest_path), manifest_path)
        with open(manifest_path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["version"], MANIFEST_VERSION)
        self.assertEqual(len(raw["contexts"]), 1)
        return raw["contexts"][0]

    def _entries(self):
        ctx = self._context()
        self.assertEqual(len(ctx["jar_sections"]), 1)
        return {e["output"]: e for e in ctx["jar_sections"][0]["entries"]}

    def test_a_plain_source_is_serialized_as_a_template(self):
        entry = self._entries()["locale/@AB_CD@/testing/plain.ftl"]

        self.assertEqual(entry["source"], {"kind": "template", "text": "plain.ftl"})

    def test_a_locale_chosen_source_is_serialized_with_its_mapping(self):
        entry = self._entries()["locale/@AB_CD@/testing/badge.png"]

        self.assertEqual(entry["source"]["kind"], "mapped")
        self.assertEqual(entry["source"]["key"], "android_marketplace")
        self.assertEqual(entry["source"]["below"], "badge.png")
        self.assertEqual(entry["source"]["default"], "en-US")
        self.assertEqual(
            entry["source"]["sources"],
            [["es*", "es-ES"], ["es-MX", "es-MX"], ["fr", "fr"]],
        )

    def test_a_section_records_its_locale_constraint(self):
        ctx = self._context()

        self.assertEqual(ctx["jar_sections"][0]["locales"], "all")

    def test_a_translated_only_manifest_is_still_a_build_input(self):
        """Every section is skipped for the source locale, so nothing else
        records the manifest and editing it would not regenerate the backend.
        """
        env = self._get_environment(
            "l10n-manifest-translated", srcdir_name="l10n-manifest-translated"
        )
        objs = self._emit("l10n-manifest-translated", env=env)[1]

        recorded = set()
        for obj in objs:
            recorded |= {mozpath.normsep(p) for p in obj.context_all_paths}

        srcdir = mozpath.normsep(env.topsrcdir)
        self.assertIn(mozpath.join(srcdir, "jar.mn"), recorded)
        self.assertIn(mozpath.join(srcdir, "included.inc"), recorded)

    def _written_contexts(self, config_name):
        env = self._get_environment(config_name, srcdir_name="l10n-manifest-roots")
        self._consume("l10n-manifest-roots", RecursiveMakeBackend, env=env)

        manifest_path = mozpath.join(env.topobjdir, "l10n-manifest.json")
        self.assertTrue(os.path.exists(manifest_path), manifest_path)

        with open(manifest_path, encoding="utf-8") as f:
            raw = json.load(f)

        return {ctx["relsrcdir"]: ctx for ctx in raw["contexts"]}

    def test_manifest_roots_drop_chrome_outside_the_roots(self):
        contexts = self._written_contexts("l10n-manifest-roots")

        self.assertEqual(sorted(contexts), ["app/locales", "shared/locales"])

        shared = contexts["shared/locales"]
        self.assertEqual(shared["jar_sections"], [])
        self.assertEqual(
            [group["sources"] for group in shared["localized_files"]],
            [["en-US/shared.ini"]],
        )

    def test_no_manifest_roots_writes_every_context(self):
        contexts = self._written_contexts("l10n-manifest-roots-unfiltered")

        self.assertEqual(
            sorted(contexts), ["app/locales", "chrome/locales", "shared/locales"]
        )
        self.assertNotEqual(contexts["shared/locales"]["jar_sections"], [])


if __name__ == "__main__":
    main()
