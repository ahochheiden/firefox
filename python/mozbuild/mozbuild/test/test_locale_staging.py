# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import tempfile
import unittest

import mozpack.path as mozpath
from mozunit import main

from mozbuild.frontend.staging_spec import (
    SPEC_VERSION,
    JarEntry,
    JarSection,
    LocalizedFileGroup,
    StagingContextData,
    StagingSpec,
    write_staging_spec,
)
from mozbuild.locale_staging import (
    _ab_rcd,
    _resolve_locale_pp_define,
    stage_locale,
)


class TestLocalePpDefineResolution(unittest.TestCase):
    """Exact-then-fnmatch lookup against a LOCALE_PP_DEFINES inner dict."""

    def test_exact_match_wins_over_pattern(self):
        m = {"es*": "es-ES", "es-MX": "es-MX", "fr": "fr"}
        self.assertEqual(_resolve_locale_pp_define(m, "es-MX"), "es-MX")
        self.assertEqual(_resolve_locale_pp_define(m, "fr"), "fr")

    def test_pattern_fallback(self):
        m = {"es*": "es-ES", "fr": "fr"}
        self.assertEqual(_resolve_locale_pp_define(m, "es-AR"), "es-ES")

    def test_no_match_returns_none(self):
        m = {"es*": "es-ES", "fr": "fr"}
        self.assertIsNone(_resolve_locale_pp_define(m, "ja"))


class TestAbRcd(unittest.TestCase):
    """AB_rCD lower-cases the region segment."""

    def test_simple(self):
        self.assertEqual(_ab_rcd("fr"), "fr")

    def test_region(self):
        self.assertEqual(_ab_rcd("zh-TW"), "zh-tw")
        self.assertEqual(_ab_rcd("es-MX"), "es-mx")


class TestStageLocale(unittest.TestCase):
    """End-to-end stage_locale against a fake merge tree and spec."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.merge_tree = mozpath.join(self._tmp, "merge")
        self.dest = mozpath.join(self._tmp, "stage")
        os.makedirs(self.merge_tree, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_merge(self, relpath, contents):
        full = mozpath.join(self.merge_tree, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(contents)

    def _spec(self, *, contexts):
        return StagingSpec(
            version=SPEC_VERSION,
            moz_app_id="{ec8030f7-c20a-464f-9b0e-13a3a9e97384}",
            moz_app_version="121.0",
            moz_app_displayname="Firefox",
            moz_build_app="browser",
            contexts=contexts,
        )

    def _stage(self, spec, locale="fr"):
        spec_path = mozpath.join(self._tmp, "staging-spec.json")
        write_staging_spec(spec, spec_path)
        stage_locale(
            locale=locale,
            spec_path=spec_path,
            merge_tree=self.merge_tree,
            dest_xpi_stage=self.dest,
        )

    def test_jar_section_locale_entry_copied(self):
        """A jar.mn locale entry resolves through the merge tree and copies
        verbatim into ``dest/<jar>/<output>``.
        """
        self._write_merge("browser/foo.ftl", "key = value\n")

        spec = self._spec(
            contexts=[
                StagingContextData(
                    relsrcdir="browser/locales",
                    install_target="dist/bin",
                    dist_subdir="",
                    defines={},
                    locale_pp_defines={},
                    jar_sections=[
                        JarSection(
                            name="browser",
                            base="",
                            relativesrcdir="browser/locales",
                            chrome_manifests=[],
                            pp_includes=[],
                            entries=[
                                JarEntry(
                                    source="foo.ftl",
                                    output="locale/browser/foo.ftl",
                                    is_locale=True,
                                    preprocess=False,
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        self._stage(spec)

        out = mozpath.join(self.dest, "browser", "locale", "browser", "foo.ftl")
        self.assertTrue(os.path.exists(out), out)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "key = value\n")

    def test_localized_files_with_glob(self):
        """A LOCALIZED_FILES glob pattern expands against the merge tree."""
        self._write_merge("hunspell/en_US.aff", "AFF\n")
        self._write_merge("hunspell/en_US.dic", "DIC\n")
        self._write_merge("hunspell/skip.txt", "SKIP\n")

        spec = self._spec(
            contexts=[
                StagingContextData(
                    relsrcdir="extensions/spellcheck/locales",
                    install_target="dist/bin",
                    dist_subdir="",
                    defines={},
                    locale_pp_defines={},
                    localized_files=[
                        LocalizedFileGroup(
                            subpath="dictionaries",
                            sources=[
                                "en-US/hunspell/*.aff",
                                "en-US/hunspell/*.dic",
                            ],
                        ),
                    ],
                ),
            ],
        )
        # The merge subdir is computed by stripping a trailing /locales,
        # so files were placed under merge/extensions/spellcheck/.
        self._write_merge("extensions/spellcheck/hunspell/en_US.aff", "AFF\n")
        self._write_merge("extensions/spellcheck/hunspell/en_US.dic", "DIC\n")
        self._stage(spec)

        self.assertTrue(
            os.path.exists(mozpath.join(self.dest, "dictionaries", "en_US.aff"))
        )
        self.assertTrue(
            os.path.exists(mozpath.join(self.dest, "dictionaries", "en_US.dic"))
        )

    def test_locale_pp_defines_resolves_at_stage_time(self):
        """Preprocess directives that reference a LOCALE_PP_DEFINES define
        get the locale-resolved value substituted in at stage time.
        """
        # Source uses the preprocessor's @VAR@ substitution with a value
        # that comes from LOCALE_PP_DEFINES at stage time.
        self._write_merge(
            "browser/marketplace.dtd",
            "<!ENTITY market.locale @ANDROID_MARKETPLACE_AB_CD@>\n",
        )

        spec = self._spec(
            contexts=[
                StagingContextData(
                    relsrcdir="browser/locales",
                    install_target="dist/bin",
                    dist_subdir="",
                    defines={},
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
                            chrome_manifests=[],
                            pp_includes=[],
                            entries=[
                                JarEntry(
                                    source="marketplace.dtd",
                                    output="locale/browser/marketplace.dtd",
                                    is_locale=True,
                                    preprocess=True,
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        self._stage(spec, locale="es-MX")

        out = mozpath.join(self.dest, "browser", "locale", "browser", "marketplace.dtd")
        self.assertTrue(os.path.exists(out), out)
        with open(out, encoding="utf-8") as f:
            content = f.read()
        # es-MX exact key wins over es* pattern.
        self.assertIn("es-MX", content)

    def test_chrome_manifest_assembled_from_jar_section(self):
        """jar.mn ``% ...`` lines accumulate into per-jar
        ``<jarname>.manifest`` plus a top-level ``chrome.manifest``
        cross-reference.
        """
        self._write_merge("browser/foo.ftl", "k=v\n")

        spec = self._spec(
            contexts=[
                StagingContextData(
                    relsrcdir="browser/locales",
                    install_target="dist/bin",
                    dist_subdir="",
                    defines={},
                    locale_pp_defines={},
                    jar_sections=[
                        JarSection(
                            name="browser",
                            base="",
                            relativesrcdir="browser/locales",
                            chrome_manifests=["locale browser %en-US %"],
                            pp_includes=[],
                            entries=[
                                JarEntry(
                                    source="foo.ftl",
                                    output="locale/browser/foo.ftl",
                                    is_locale=True,
                                    preprocess=False,
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        self._stage(spec)

        per_jar = mozpath.join(self.dest, "browser.manifest")
        top = mozpath.join(self.dest, "chrome.manifest")
        self.assertTrue(os.path.exists(per_jar), per_jar)
        self.assertTrue(os.path.exists(top), top)
        # `% ...` lines have `%` substituted with chromebase
        # (basename(jarname) + "/"), mirroring JarMaker's default mode.
        with open(per_jar, encoding="utf-8") as f:
            self.assertIn("locale browser browser/en-US browser/", f.read())
        with open(top, encoding="utf-8") as f:
            self.assertIn("manifest browser.manifest", f.read())


if __name__ == "__main__":
    main()
