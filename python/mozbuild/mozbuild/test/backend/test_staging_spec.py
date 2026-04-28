# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os
import unittest

import mozpack.path as mozpath
from mozunit import main

from mozbuild.backend.recursivemake import RecursiveMakeBackend
from mozbuild.frontend.staging_spec import (
    SPEC_VERSION,
    JarEntry,
    JarSection,
    LocalizedFileGroup,
    LocalizedGenScript,
    StagingContextData,
    StagingSpec,
    load_staging_spec,
    write_staging_spec,
)
from mozbuild.test.backend.common import BackendTester


class TestStagingSpecRoundTrip(unittest.TestCase):
    """write_staging_spec / load_staging_spec round-trip the spec
    structure unchanged, including nested dataclasses.
    """

    def test_round_trip(self):
        import tempfile

        spec = StagingSpec(
            version=SPEC_VERSION,
            moz_app_id="{ec8030f7-c20a-464f-9b0e-13a3a9e97384}",
            moz_app_version="121.0",
            moz_app_displayname="Firefox",
            moz_build_app="browser",
            contexts=[
                StagingContextData(
                    relsrcdir="browser/locales",
                    install_target="dist/bin",
                    dist_subdir="browser",
                    defines={"FOO": "bar"},
                    locale_pp_defines={
                        "ANDROID_MARKETPLACE_AB_CD": {
                            "es*": "es-ES",
                            "es-MX": "es-MX",
                            "fr": "fr",
                        },
                    },
                    jar_sections=[
                        JarSection(
                            name="browser",
                            base="",
                            relativesrcdir="browser/locales",
                            chrome_manifests=["locale browser %en-US %"],
                            pp_includes=[],
                            entries=[
                                JarEntry(
                                    source="en-US/foo.ftl",
                                    output="foo.ftl",
                                    is_locale=True,
                                    preprocess=False,
                                ),
                            ],
                        ),
                    ],
                    localized_files=[
                        LocalizedFileGroup(subpath="..", sources=["!updater.ini"]),
                    ],
                    localized_pp_files=[],
                    localized_generated_files=[
                        LocalizedGenScript(
                            script="/topsrcdir/browser/locales/generate_ini.py",
                            method="main",
                            inputs=["en-US/updater/updater.ini"],
                            outputs=["updater.ini"],
                            flags=[],
                            force=False,
                        ),
                    ],
                ),
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "staging-spec.json")
            write_staging_spec(spec, path)
            self.assertTrue(os.path.exists(path))

            loaded = load_staging_spec(path)

        self.assertEqual(loaded.version, spec.version)
        self.assertEqual(loaded.moz_app_id, spec.moz_app_id)
        self.assertEqual(len(loaded.contexts), 1)

        ctx = loaded.contexts[0]
        self.assertEqual(ctx.relsrcdir, "browser/locales")
        self.assertEqual(
            ctx.locale_pp_defines["ANDROID_MARKETPLACE_AB_CD"]["es-MX"], "es-MX"
        )
        self.assertEqual(len(ctx.jar_sections), 1)
        self.assertEqual(ctx.jar_sections[0].name, "browser")
        self.assertEqual(ctx.jar_sections[0].entries[0].is_locale, True)
        self.assertEqual(ctx.localized_files[0].subpath, "..")
        self.assertEqual(ctx.localized_files[0].sources, ["!updater.ini"])
        self.assertEqual(
            ctx.localized_generated_files[0].script,
            "/topsrcdir/browser/locales/generate_ini.py",
        )


class TestStagingSpecBackendIntegration(BackendTester):
    """End-to-end test: run a backend against a fixture moz.build tree and
    verify that ``staging-spec.json`` lands in topobjdir with the expected
    structure. Confirms the emitter→backend handoff works.
    """

    def test_locale_pp_defines_in_spec(self):
        env = self._consume("staging-spec", RecursiveMakeBackend)

        spec_path = mozpath.join(env.topobjdir, "staging-spec.json")
        self.assertTrue(os.path.exists(spec_path), spec_path)

        with open(spec_path, encoding="utf-8") as f:
            raw = json.load(f)

        self.assertEqual(raw["version"], SPEC_VERSION)
        self.assertEqual(len(raw["contexts"]), 1)

        ctx = raw["contexts"][0]
        self.assertEqual(
            ctx["locale_pp_defines"],
            {
                "ANDROID_MARKETPLACE_AB_CD": {
                    "es*": "es-ES",
                    "es-MX": "es-MX",
                    "fr": "fr",
                },
            },
        )
        self.assertEqual(ctx["jar_sections"], [])
        self.assertEqual(ctx["localized_files"], [])


if __name__ == "__main__":
    main()
