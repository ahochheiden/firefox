# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import dataclasses
import os
import tempfile
import unittest

import mozpack.path as mozpath
import mozunit

from mozbuild.frontend.jar_manifest import (
    AB_CD_TOKEN,
    JarManifestError,
    JarSection,
    parse_jar_manifest,
    source_from_dict,
)
from mozbuild.jar import DeprecatedJarManifest

MANIFEST = """\
#filter substitution
browser.jar:
% content browser %content/browser/
  content/browser/main.xhtml     (main.xhtml)
* content/browser/built.js       (built.js)
  content/browser/wild/          (wild/*)
  locale/browser/thing.dtd       (%thing.dtd)
"""


def _write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)


class TestParseJarManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = mozpath.normsep(self._tmp.name)

    def _parse(self, body=MANIFEST, defines=None, includes=None, sources=None):
        for name, included in (includes or {}).items():
            _write(mozpath.join(self.tmpdir, name), included)
        path = mozpath.join(self.tmpdir, "jar.mn")
        _write(path, body)
        return parse_jar_manifest(path, defines or {}, localized_sources=sources)

    def test_one_section_is_named_and_based(self):
        sections, _ = self._parse()

        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].name.text, "chrome/browser")
        self.assertEqual(sections[0].base, "")

    def test_entries_carry_source_and_destination(self):
        sections, _ = self._parse()
        entries = {e.output.text: e for e in sections[0].entries}

        self.assertEqual(
            entries["content/browser/main.xhtml"].source.text, "main.xhtml"
        )
        self.assertEqual(entries["content/browser/wild/"].source.text, "wild/*")

    def test_a_star_marks_an_entry_for_preprocessing(self):
        sections, _ = self._parse()
        entries = {e.output.text: e for e in sections[0].entries}

        self.assertTrue(entries["content/browser/built.js"].preprocess)
        self.assertFalse(entries["content/browser/main.xhtml"].preprocess)

    def test_a_percent_marks_an_entry_as_localized(self):
        sections, _ = self._parse()
        entries = {e.output.text: e for e in sections[0].entries}

        self.assertTrue(entries["locale/browser/thing.dtd"].is_locale)
        self.assertFalse(entries["content/browser/main.xhtml"].is_locale)

    def test_chrome_manifest_lines_are_kept(self):
        sections, _ = self._parse()

        self.assertEqual(
            [m.text for m in sections[0].chrome_manifests],
            ["content browser %content/browser/"],
        )

    def test_the_manifest_is_reported_as_a_read_input(self):
        _, inputs = self._parse()

        self.assertIn(
            mozpath.join(self.tmpdir, "jar.mn"),
            [mozpath.normsep(path) for path in inputs],
        )

    def test_an_included_file_is_reported_as_a_read_input(self):
        body = MANIFEST.replace(
            "browser.jar:", "#include included.inc\nbrowser.jar:", 1
        )
        _, inputs = self._parse(
            body, includes={"included.inc": "#define FROM_INCLUDE 1\n"}
        )

        self.assertIn(
            mozpath.join(self.tmpdir, "included.inc"),
            [mozpath.normsep(path) for path in inputs],
        )

    def test_a_define_from_an_included_file_reaches_the_preprocessor(self):
        body = MANIFEST.replace(
            "browser.jar:", "#include included.inc\nbrowser.jar:", 1
        )
        body += "  content/browser/@FROM_INCLUDE@.js  (named.js)\n"
        sections, _ = self._parse(
            body, includes={"included.inc": "#define FROM_INCLUDE included\n"}
        )
        outputs = [e.output.text for e in sections[0].entries]

        self.assertIn("content/browser/included.js", outputs)

    def test_an_unresolved_locale_token_survives_parsing(self):
        """Parsing is locale independent, so @AB_CD@ must not be resolved."""
        body = MANIFEST.replace(
            "  content/browser/main.xhtml     (main.xhtml)",
            "  content/browser/@AB_CD@/main.xhtml  (main.xhtml)",
            1,
        )
        sections, _ = self._parse(body)
        outputs = [e.output.text for e in sections[0].entries]

        self.assertIn("content/browser/@AB_CD@/main.xhtml", outputs)

    def test_a_template_resolves_for_a_locale(self):
        body = MANIFEST.replace(
            "  content/browser/main.xhtml     (main.xhtml)",
            "  content/browser/@AB_CD@/main.xhtml  (main.xhtml)",
            1,
        )
        sections, _ = self._parse(body)
        entry = next(e for e in sections[0].entries if AB_CD_TOKEN in e.output.text)

        self.assertEqual(entry.output.resolve("de"), "content/browser/de/main.xhtml")

    def test_a_section_applies_to_every_locale_by_default(self):
        sections, _ = self._parse()
        section = sections[0]

        self.assertEqual(section.locales, "all")
        self.assertTrue(section.applies_to("en-US"))
        self.assertTrue(section.applies_to("de"))

    def test_a_translated_section_skips_the_source_locale(self):
        body = MANIFEST.replace("browser.jar:", "browser.jar:\nlocales translated:", 1)
        sections, _ = self._parse(body)
        section = sections[0]

        self.assertEqual(section.locales, "translated")
        self.assertFalse(section.applies_to("en-US"))
        self.assertTrue(section.applies_to("de"))

    def test_the_constraint_survives_a_relativesrcdir_split(self):
        """`relativesrcdir` starts a fresh section once entries exist, so the
        constraint has to carry across the split it causes.
        """
        body = (
            "#filter substitution\n"
            "[localization] @AB_CD@.jar:\n"
            "locales translated:\n"
            "relativesrcdir toolkit/locales:\n"
            "  toolkit/about   (%toolkit/about/aboutAbout.ftl)\n"
            "relativesrcdir dom/locales:\n"
            "  toolkit/dom     (%chrome/dom/dom.properties)\n"
        )
        sections, _ = self._parse(body)

        self.assertEqual(len(sections), 2)
        for section in sections:
            self.assertEqual(section.locales, "translated")
            self.assertFalse(section.applies_to("en-US"))

    def _mapped(self, entry, key="badge"):
        body = "#filter substitution\nbrowser.jar:\n  " + entry + "\n"
        return self._parse(
            body,
            sources={key: {"default": "en-US", "fr": "fr"}},
        )

    def test_a_mapping_chooses_the_directory_a_source_reads_from(self):
        sections, _ = self._mapped("content/icon.png  (@badge@/icon.png)")
        entry = sections[0].entries[0]

        self.assertEqual(entry.source.resolve("fr"), "fr/icon.png")
        self.assertEqual(entry.source.resolve("de"), "en-US/icon.png")

    def test_an_exact_locale_wins_over_a_pattern_and_the_first_pattern_wins(self):
        sections, _ = self._parse(
            "#filter substitution\nbrowser.jar:\n  content/icon.png  (@badge@/icon.png)\n",
            sources={
                "badge": {
                    "default": "en-US",
                    "es*": "es-ES",
                    "*-MX": "mx",
                    "es-MX": "es-MX",
                }
            },
        )
        entry = sections[0].entries[0]

        self.assertEqual(entry.source.resolve("es-MX"), "es-MX/icon.png")
        self.assertEqual(entry.source.resolve("es-AR"), "es-ES/icon.png")
        self.assertEqual(entry.source.resolve("pt-MX"), "mx/icon.png")
        self.assertEqual(entry.source.resolve("de"), "en-US/icon.png")

        again = source_from_dict(entry.source.to_dict())
        self.assertEqual(again.resolve("es-MX"), "es-MX/icon.png")
        self.assertEqual(again.resolve("es-AR"), "es-ES/icon.png")

    def test_a_mapping_must_start_the_source(self):
        with self.assertRaisesRegex(JarManifestError, "must start with @badge@"):
            self._mapped("content/icon.png  (below/@badge@/icon.png)")

    def test_a_mapping_named_twice_is_rejected(self):
        with self.assertRaisesRegex(JarManifestError, "names @badge@ more than once"):
            self._mapped("content/icon.png  (@badge@/@badge@/icon.png)")

    def test_a_mapping_cannot_name_a_destination(self):
        with self.assertRaisesRegex(
            JarManifestError,
            "content/@badge@/icon.png names the source mapping @badge@, "
            "which only a source may do",
        ):
            self._mapped("content/@badge@/icon.png  (en-US/icon.png)")

    def test_a_mapping_cannot_name_a_section(self):
        body = "#filter substitution\n[localization] @badge@.jar:\n  a  (en-US/a)\n"

        with self.assertRaisesRegex(
            JarManifestError, "names the source mapping @badge@, which only a source"
        ):
            self._parse(body, sources={"badge": {"default": "en-US", "fr": "fr"}})

    def test_a_mapping_cannot_name_a_chrome_registration(self):
        body = (
            "#filter substitution\n"
            "browser.jar:\n"
            "%  content browser %content/@badge@/\n"
            "  content/a  (en-US/a)\n"
        )

        with self.assertRaisesRegex(
            JarManifestError,
            "content browser %content/@badge@/ names the source mapping @badge@",
        ):
            self._parse(body, sources={"badge": {"default": "en-US", "fr": "fr"}})

    def test_a_mapping_needs_a_default(self):
        body = "#filter substitution\nbrowser.jar:\n  a  (@badge@/a)\n"

        with self.assertRaisesRegex(JarManifestError, "badge needs a default source"):
            self._parse(body, sources={"badge": {"fr": "fr"}})

    def test_a_mapping_cannot_be_called_ab_cd(self):
        body = "#filter substitution\nbrowser.jar:\n"

        with self.assertRaisesRegex(JarManifestError, "AB_CD names the locale itself"):
            self._parse(body, sources={"AB_CD": {"default": "en-US"}})

    def test_a_parsed_section_survives_being_written_and_read(self):
        """What is serialized has to reconstruct the same source, not a copy of
        it with different types.
        """
        sections, _ = self._mapped("content/icon.png  (@badge@/icon.png)")
        entry = sections[0].entries[0]

        again = source_from_dict(entry.source.to_dict())

        self.assertEqual(again, entry.source)
        self.assertEqual(again.resolve("fr"), entry.source.resolve("fr"))

    def test_a_locale_resolves_a_path_below_a_chosen_source(self):
        """A locale resolves every path it appears in, and a source mapping
        chooses only the directory above one.
        """
        sections, _ = self._mapped(
            "content/@AB_CD@/icon.png  (@badge@/@AB_CD@/icon.png)"
        )
        entry = sections[0].entries[0]

        self.assertEqual(entry.source.resolve("fr"), "fr/fr/icon.png")
        self.assertEqual(entry.output.resolve("fr"), "content/fr/icon.png")

    def test_a_locale_resolves_the_directory_a_mapping_chose(self):
        """A mapping value is a path like any other, so a locale resolves it."""
        sections, _ = self._parse(
            "#filter substitution\nbrowser.jar:\n  content/icon.png  (@badge@/icon.png)\n",
            sources={"badge": {"default": "shared/@AB_CD@", "fr": "only/@AB_CD@"}},
        )
        entry = sections[0].entries[0]

        self.assertEqual(entry.source.resolve("fr"), "only/fr/icon.png")
        self.assertEqual(entry.source.resolve("de"), "shared/de/icon.png")

    def test_the_constraint_must_precede_the_entries_it_governs(self):
        body = MANIFEST + "locales translated:\n"

        with self.assertRaisesRegex(DeprecatedJarManifest, "locales must come before"):
            self._parse(body)

    def test_applies_to_unknown_constraint(self):
        sections, _ = self._parse()
        section = dataclasses.replace(sections[0], locales="some")

        with self.assertRaisesRegex(ValueError, "unknown locale constraint some"):
            section.applies_to("de")

    def test_defines_reach_the_preprocessor(self):
        body = MANIFEST + "  content/browser/@THING@.js  (named.js)\n"
        sections, _ = self._parse(body, defines={"THING": "resolved"})
        outputs = [e.output.text for e in sections[0].entries]

        self.assertIn("content/browser/resolved.js", outputs)

    def test_a_locale_resolves_every_place_a_template_appears(self):
        body = (
            "#filter substitution\n"
            "[localization] @AB_CD@.jar:\n"
            "%  locale testing @AB_CD@ %locale/@AB_CD@/testing/\n"
            "  locale/@AB_CD@/testing/out.txt   (@AB_CD@/in.txt)\n"
        )
        sections, _ = self._parse(body)
        section = sections[0]
        entry = section.entries[0]

        self.assertEqual(section.name.resolve("de"), "de")
        self.assertEqual(entry.output.resolve("de"), "locale/de/testing/out.txt")
        self.assertEqual(entry.source.resolve("de"), "de/in.txt")
        self.assertEqual(
            section.chrome_manifests[0].resolve("de"),
            "locale testing de %locale/de/testing/",
        )

    def test_a_base_cannot_hold_the_locale_placeholder(self):
        body = "#filter substitution\n[@AB_CD@] browser.jar:\n  a  (a)\n"

        with self.assertRaisesRegex(
            JarManifestError, "base @AB_CD@ names @AB_CD@, which is only resolved"
        ):
            self._parse(body)

    def test_a_relativesrcdir_cannot_hold_the_locale_placeholder(self):
        body = (
            "#filter substitution\n"
            "browser.jar:\n"
            "relativesrcdir toolkit/@AB_CD@:\n"
            "  a  (%a)\n"
        )

        with self.assertRaisesRegex(
            JarManifestError,
            "relativesrcdir toolkit/@AB_CD@ names @AB_CD@, which is only resolved",
        ):
            self._parse(body)

    def test_a_preprocessor_condition_cannot_name_the_locale(self):
        body = "#filter substitution\nbrowser.jar:\n#if AB_CD == de\n  a  (a)\n#endif\n"

        with self.assertRaisesRegex(JarManifestError, "jar.mn:3"):
            self._parse(body)

    def test_an_elifdef_or_elifndef_on_the_locale_is_refused_too(self):
        for directive in ("elifdef", "elifndef"):
            body = f"browser.jar:\n#ifdef FOO\n#{directive} AB_CD\n  a  (a)\n#endif\n"

            with self.assertRaisesRegex(JarManifestError, "jar.mn:3"):
                self._parse(body)

    def test_an_included_condition_on_the_locale_is_refused_too(self):
        body = "#filter substitution\n#include included.inc\nbrowser.jar:\n  a  (a)\n"

        with self.assertRaisesRegex(JarManifestError, "included.inc:1"):
            self._parse(body, includes={"included.inc": "#ifdef AB_CD\n#endif\n"})

    def test_a_relativesrcdir_cannot_name_a_source_mapping(self):
        body = (
            "#filter substitution\n"
            "browser.jar:\n"
            "relativesrcdir @badge@/locales:\n"
            "  a  (%a)\n"
        )

        with self.assertRaisesRegex(
            JarManifestError,
            "relativesrcdir @badge@/locales names @badge@, which is only resolved "
            "in an entry source",
        ):
            self._parse(body, sources={"badge": {"default": "en-US", "fr": "fr"}})

    def test_a_section_survives_being_written_and_read(self):
        sections, _ = self._parse(
            "#filter substitution\n"
            "[localization] @AB_CD@.jar:\n"
            "locales translated:\n"
            "relativesrcdir toolkit/locales:\n"
            "%  locale testing @AB_CD@ %locale/@AB_CD@/testing/\n"
            "  toolkit/about  (%toolkit/about/aboutAbout.ftl)\n"
            "  content/icon.png  (@badge@/icon.png)\n",
            sources={"badge": {"default": "en-US", "fr": "fr"}},
        )
        section = sections[0]

        self.assertEqual(JarSection.from_dict(section.to_dict()), section)


class _Context(dict):
    def __init__(self, declared, configured):
        from types import SimpleNamespace

        super().__init__(DEFINES=declared)
        self.config = SimpleNamespace(defines=configured)


class TestJarManifestDefines(unittest.TestCase):
    def _defines(self, declared, configured):
        from mozbuild.frontend.jar_manifest import jar_manifest_defines

        return jar_manifest_defines(_Context(declared, configured))

    def test_a_configured_define_wins(self):
        defines = self._defines({"A": "moz"}, {"A": "cfg", "B": 1})

        self.assertEqual(defines, {"A": "cfg", "B": 1})

    def test_a_false_define_is_absent(self):
        defines = self._defines({"A": False, "B": True}, {"C": False, "D": ""})

        self.assertEqual(defines, {"B": True, "D": ""})


if __name__ == "__main__":
    mozunit.main()
