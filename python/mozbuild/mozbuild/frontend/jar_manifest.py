# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Parsing of jar manifests into a model no backend owns.

A jar manifest names the same installs `moz.build` already expresses. Parsing
it produces this model, and a separate step turns the model into build objects,
so every reader gets one description of what a jar manifest declares.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import re
from typing import Optional, Union

import mozpack.path as mozpath

from mozbuild.jar import (
    LOCALES_ALL,
    LOCALES_TRANSLATED,
    DeprecatedJarManifest,
    JarManifestParser,
)
from mozbuild.preprocessor import Preprocessor

AB_CD_TOKEN = "@AB_CD@"

# The locale every localizable string is authored in.
SOURCE_LOCALE = "en-US"


class JarManifestError(Exception):
    """A jar manifest declares something the model cannot represent."""


@dataclasses.dataclass(frozen=True)
class LocaleTemplate:
    """A string holding the `@AB_CD@` placeholder until a locale resolves it.

    A jar manifest names most of its paths once for every locale, so the model
    keeps the placeholder and each reader resolves it for the locale it is
    building.
    """

    text: str

    def resolve(self, ab_cd: str) -> str:
        return self.text.replace(AB_CD_TOKEN, ab_cd)

    def to_dict(self):
        """The template as the l10n manifest carries it."""
        return {"kind": "template", "text": self.text}


DEFAULT_SOURCE_KEY = "default"


@dataclasses.dataclass(frozen=True)
class LocaleMappedSource:
    """A source directory chosen by locale, with a default for every other
    locale.

    A locale that matches no key still has to read from somewhere, so the
    default is required.
    """

    key: str
    sources: tuple[tuple[str, str], ...]
    default: str

    def resolve(self, ab_cd: str) -> str:
        for locale, source in self.sources:
            if locale == ab_cd:
                return LocaleTemplate(source).resolve(ab_cd)
        for locale, source in self.sources:
            if fnmatch.fnmatchcase(ab_cd, locale):
                return LocaleTemplate(source).resolve(ab_cd)
        return LocaleTemplate(self.default).resolve(ab_cd)


@dataclasses.dataclass(frozen=True)
class MappedEntrySource:
    """An entry source rooted at a locale chosen directory."""

    mapping: LocaleMappedSource
    below: str

    def resolve(self, ab_cd: str) -> str:
        return mozpath.join(
            self.mapping.resolve(ab_cd), LocaleTemplate(self.below).resolve(ab_cd)
        )

    def to_dict(self):
        """The source and the mapping it names, as the l10n manifest carries
        them.
        """
        return {
            "kind": "mapped",
            "key": self.mapping.key,
            "sources": [list(pair) for pair in self.mapping.sources],
            "default": self.mapping.default,
            "below": self.below,
        }


@dataclasses.dataclass(frozen=True)
class JarEntry:
    """One `destination (source)` line."""

    source: Union[LocaleTemplate, MappedEntrySource]
    output: LocaleTemplate
    preprocess: bool
    is_locale: bool

    def to_dict(self):
        """The entry as the l10n manifest carries it."""
        return {
            "source": self.source.to_dict(),
            "output": self.output.text,
            "preprocess": self.preprocess,
            "is_locale": self.is_locale,
        }

    @classmethod
    def from_dict(cls, raw):
        return cls(
            source=source_from_dict(raw["source"]),
            output=LocaleTemplate(raw["output"]),
            preprocess=raw["preprocess"],
            is_locale=raw["is_locale"],
        )


@dataclasses.dataclass(frozen=True)
class JarSection:
    """One `name.jar:` section and the entries below it."""

    name: LocaleTemplate
    base: str
    relativesrcdir: Optional[str]
    entries: tuple[JarEntry, ...]
    chrome_manifests: tuple[LocaleTemplate, ...]
    locales: str = LOCALES_ALL

    def applies_to(self, ab_cd: str) -> bool:
        """Whether this section belongs in a build of ``ab_cd``."""
        if self.locales == LOCALES_ALL:
            return True
        if self.locales == LOCALES_TRANSLATED:
            return ab_cd != SOURCE_LOCALE
        raise ValueError(f"unknown locale constraint {self.locales}")

    def to_dict(self):
        """The section as the l10n manifest carries it."""
        return {
            "name": self.name.text,
            "base": self.base,
            "relativesrcdir": self.relativesrcdir,
            "chrome_manifests": [m.text for m in self.chrome_manifests],
            "locales": self.locales,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, raw):
        return cls(
            name=LocaleTemplate(raw["name"]),
            base=raw["base"],
            relativesrcdir=raw["relativesrcdir"],
            entries=tuple(JarEntry.from_dict(e) for e in raw["entries"]),
            chrome_manifests=tuple(LocaleTemplate(m) for m in raw["chrome_manifests"]),
            locales=raw["locales"],
        )


def source_from_dict(entry):
    """A jar entry source, from what its ``to_dict`` wrote."""
    kind = entry["kind"]
    if kind == "template":
        return LocaleTemplate(entry["text"])
    if kind == "mapped":
        return MappedEntrySource(
            mapping=LocaleMappedSource(
                key=entry["key"],
                sources=tuple(tuple(pair) for pair in entry["sources"]),
                default=entry["default"],
            ),
            below=entry["below"],
        )
    raise ValueError(f"unknown jar entry source kind {kind}")


def _locale_mapped_sources(declarations):
    """Turn the declared source mappings into the model's own type."""
    mapped = {}
    for key, table in (declarations or {}).items():
        if _token(key) == AB_CD_TOKEN:
            raise JarManifestError(
                f"{key} names the locale itself, which a source mapping cannot redefine"
            )
        if DEFAULT_SOURCE_KEY not in table:
            raise JarManifestError(
                f"{key} needs a {DEFAULT_SOURCE_KEY} source for locales it "
                f"does not name"
            )
        mapped[key] = LocaleMappedSource(
            key=key,
            sources=tuple(
                (locale, source)
                for locale, source in table.items()
                if locale != DEFAULT_SOURCE_KEY
            ),
            default=table[DEFAULT_SOURCE_KEY],
        )
    return mapped


def _token(key):
    return f"@{key}@"


def _destination(text, mapped, path):
    """An entry's destination. It may hold the locale placeholder but not a
    source mapping key.
    """
    _reject_mapping_outside_source(text, mapped, path)
    return LocaleTemplate(text)


def _source_for(text, mapped, path):
    """An entry's source, as a mapping when it names one."""
    mapping = _mapped_source_for(text, mapped, path)
    if mapping is None:
        return LocaleTemplate(text)
    return MappedEntrySource(
        mapping=mapping,
        below=text[len(_token(mapping.key)) :].lstrip("/"),
    )


def _mapped_source_for(text, mapped, path):
    """The mapping an entry's source refers to, if it refers to one."""
    found = [source for key, source in mapped.items() if _token(key) in text]
    if not found:
        return None
    if len(found) > 1:
        raise JarManifestError(f"{path}: {text} refers to more than one source mapping")
    source = found[0]
    token = _token(source.key)
    if text.count(token) > 1:
        raise JarManifestError(f"{path}: {text} names {token} more than once")
    if not text.startswith(token):
        raise JarManifestError(f"{path}: {text} must start with {token}")
    return source


def _reject_locale_placeholder(what, text, mapped, path):
    """A base or a relativesrcdir is read as written, so it may not hold the
    locale placeholder. Only a section name, an entry and a chrome manifest
    line are resolved for a locale.
    """
    if not text:
        return
    if AB_CD_TOKEN in text:
        raise JarManifestError(
            f"{path}: {what} {text} names {AB_CD_TOKEN}, which is only resolved "
            f"in a section name, an entry or a chrome manifest line"
        )
    for key in mapped:
        token = _token(key)
        if token in text:
            raise JarManifestError(
                f"{path}: {what} {text} names {token}, which is only resolved "
                f"in an entry source"
            )


def _reject_mapping_outside_source(text, mapped, path):
    """A mapping chooses where to read, so only a source may name one."""
    for key in mapped:
        if _token(key) in text:
            raise JarManifestError(
                f"{path}: {text} names the source mapping {_token(key)}, "
                f"which only a source may do"
            )


def jar_manifest_defines(context):
    """The defines a jar manifest is preprocessed with.

    A configured define wins over one that the declaring `moz.build` sets, and
    a define set to False is absent, as it is on a preprocessor command line.
    """
    defines = {**(context.get("DEFINES") or {}), **context.config.defines}
    return {name: value for name, value in defines.items() if value is not False}


_LOCALE_CONDITION = re.compile(
    r"^\s*#\s*(if|elif|ifdef|ifndef|elifdef|elifndef)\b.*\bAB_CD\b"
)


def _reject_locale_condition(paths):
    """A preprocessor condition on the locale is refused.

    The manifest is parsed once for every locale, so the condition cannot be
    decided per locale. Evaluating it against the placeholder would pick one
    branch for all of them.
    """
    for read in sorted(paths):
        with open(read, encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                if _LOCALE_CONDITION.match(line):
                    raise JarManifestError(
                        f"{read}:{number}: a preprocessor condition names AB_CD, "
                        f"which a jar manifest leaves open for every locale"
                    )


def parse_jar_manifest(path, defines, localized_sources=None):
    """Read a jar manifest and return its sections with every file that was read.

    The caller records the read paths so that editing the manifest or any of
    its includes regenerates whatever consumed the sections.
    """
    mapped = _locale_mapped_sources(localized_sources)

    pp = Preprocessor()
    pp.context.update(defines)
    # The preprocessor fails on an unknown variable, so AB_CD is defined as
    # its own placeholder and comes out of substitution unchanged. A source
    # mapping key is handled the same way and turned into its mapping below.
    pp.context.update(AB_CD=AB_CD_TOKEN)
    pp.context.update({key: _token(key) for key in mapped})
    pp.out = JarManifestParser()
    try:
        pp.do_include(path)
    except DeprecatedJarManifest as e:
        raise DeprecatedJarManifest(
            f"Parsing error while processing {path}: {e}"
        ) from e

    _reject_locale_condition(pp.includes)
    for jarinfo in pp.out:
        _reject_locale_placeholder("base", jarinfo.base, mapped, path)
        _reject_locale_placeholder(
            "relativesrcdir", jarinfo.relativesrcdir, mapped, path
        )
        _reject_mapping_outside_source(jarinfo.name, mapped, path)
        for line in jarinfo.chrome_manifests:
            _reject_mapping_outside_source(line, mapped, path)

    sections = tuple(
        JarSection(
            name=LocaleTemplate(jarinfo.name),
            base=jarinfo.base,
            relativesrcdir=jarinfo.relativesrcdir,
            entries=tuple(
                JarEntry(
                    source=_source_for(entry.source, mapped, path),
                    output=_destination(entry.output, mapped, path),
                    preprocess=entry.preprocess,
                    is_locale=entry.is_locale,
                )
                for entry in jarinfo.entries
            ),
            chrome_manifests=tuple(
                LocaleTemplate(line) for line in jarinfo.chrome_manifests
            ),
            locales=jarinfo.locales,
        )
        for jarinfo in pp.out
    )

    return sections, pp.includes
