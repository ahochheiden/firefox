# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Locale-independent staging spec for per-locale ``dist/xpi-stage/locale-X``
trees.

The emitter yields a ``StagingContext`` per moz.build context that contributes
locale-aware content (jar.mn, ``LOCALIZED_FILES`` / ``LOCALIZED_PP_FILES``,
``LOCALIZED_GENERATED_FILES``, ``LOCALE_PP_DEFINES``). A build backend
collects them and writes ``<topobjdir>/staging-spec.json``, the
locale-independent JSON descriptor consumed at command time by
``mozbuild.locale_staging`` to materialize ``dist/xpi-stage/locale-<ab_cd>/``.

Locale substitution happens at ``stage_locale`` time, not here.
"""

import json
import os
from dataclasses import dataclass, field

from mozbuild.frontend.context import SourcePath
from mozbuild.frontend.data import ContextDerived
from mozbuild.jar import DeprecatedJarManifest, JarManifestParser
from mozbuild.preprocessor import Preprocessor

# Format version; bumped when the on-disk shape changes incompatibly.
SPEC_VERSION = 1

# Sentinel substituted for ``@AB_CD@`` when parsing jar.mn at configure
# time. Stage time substitutes this back to the actual locale. Chosen to
# match ``JarManifestParser``'s ``[\w\d.\-_\/{}]+`` jar-name regex (no
# ``@``) and to be visibly obvious in any captured spec dump.
LOCALE_PLACEHOLDER = "MOZAB_CD_PLACEHOLDER"


@dataclass
class JarEntry:
    """One entry inside a jar.mn group."""

    source: str
    output: str
    is_locale: bool
    preprocess: bool


@dataclass
class JarSection:
    """One jar.mn group's locale-aware data, captured for any locale.

    ``relativesrcdir`` mirrors ``jarinfo.relativesrcdir`` and drives the
    merge-tree subdir for ``is_locale`` entries.
    """

    name: str
    base: str
    relativesrcdir: str
    chrome_manifests: list
    pp_includes: list
    entries: list  # of JarEntry


@dataclass
class LocalizedFileGroup:
    """One ``LOCALIZED_FILES``/``LOCALIZED_PP_FILES`` group for a context."""

    subpath: str
    sources: list


@dataclass
class LocalizedGenScript:
    """One ``LOCALIZED_GENERATED_FILES`` script invocation, locale-templated."""

    script: str
    method: str
    inputs: list
    outputs: list
    flags: list
    force: bool


@dataclass
class StagingContextData:
    """Per-moz.build-context staging data — the JSON-serializable shape."""

    relsrcdir: str
    install_target: str
    dist_subdir: str
    defines: dict
    locale_pp_defines: dict
    jar_sections: list = field(default_factory=list)
    localized_files: list = field(default_factory=list)
    localized_pp_files: list = field(default_factory=list)
    localized_generated_files: list = field(default_factory=list)


@dataclass
class StagingSpec:
    """Top-level on-disk staging spec."""

    version: int
    moz_app_id: str
    moz_app_version: str
    moz_app_displayname: str
    moz_build_app: str
    contexts: list = field(default_factory=list)


class StagingContext(ContextDerived):
    """Emitter-yielded wrapper around a ``StagingContextData``.

    A build backend collects these (one per moz.build context with
    locale-aware content) and assembles them into a ``StagingSpec`` written
    to ``<topobjdir>/staging-spec.json``.
    """

    __slots__ = ("data",)

    def __init__(self, context, data):
        ContextDerived.__init__(self, context)
        self.data = data


def emit_staging_spec(emitter, contexts):
    """Yield one ``StagingContext`` per moz.build context with l10n content.

    Pure: this function does no I/O. The build backend that consumes the
    yielded objects is responsible for writing the spec to disk.
    """
    for context in contexts.values():
        sd = _extract_staging_data(emitter, context)
        if sd is not None:
            yield StagingContext(context, sd)


def write_staging_spec(spec, path):
    """Serialize ``spec`` (a ``StagingSpec``) to ``path`` as JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(_to_json(spec), f, indent=2, sort_keys=True)
        f.write("\n")


def load_staging_spec(path):
    """Load a ``StagingSpec`` previously written by ``write_staging_spec``."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return _from_json(raw)


def build_staging_spec(emitter, staging_contexts):
    """Build a ``StagingSpec`` from a list of yielded ``StagingContext``
    objects (typically collected by a backend).
    """
    return StagingSpec(
        version=SPEC_VERSION,
        moz_app_id=emitter.config.substs.get("MOZ_APP_ID") or "",
        moz_app_version=emitter.config.substs.get("MOZ_APP_VERSION") or "",
        moz_app_displayname=emitter.config.substs.get("MOZ_APP_DISPLAYNAME") or "",
        moz_build_app=emitter.config.substs.get("MOZ_BUILD_APP") or "",
        contexts=[sc.data for sc in staging_contexts],
    )


def build_staging_spec_from_substs(substs, staging_data_list):
    """Same as ``build_staging_spec`` but takes a substs dict and a list of
    ``StagingContextData``s directly. Useful from backend ``consume_finished``
    where the emitter object is no longer in scope.
    """
    return StagingSpec(
        version=SPEC_VERSION,
        moz_app_id=substs.get("MOZ_APP_ID") or "",
        moz_app_version=substs.get("MOZ_APP_VERSION") or "",
        moz_app_displayname=substs.get("MOZ_APP_DISPLAYNAME") or "",
        moz_build_app=substs.get("MOZ_BUILD_APP") or "",
        contexts=list(staging_data_list),
    )


def _to_json(obj):
    """Recursively convert dataclass instances to plain dict/list values."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_json(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {k: _to_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json(v) for v in obj]
    if isinstance(obj, tuple):
        return [_to_json(v) for v in obj]
    return obj


def _from_json(raw):
    """Reconstruct a ``StagingSpec`` from a parsed JSON dict."""
    contexts = []
    for c in raw.get("contexts", []):
        contexts.append(
            StagingContextData(
                relsrcdir=c["relsrcdir"],
                install_target=c["install_target"],
                dist_subdir=c["dist_subdir"],
                defines=dict(c.get("defines") or {}),
                locale_pp_defines={
                    k: dict(v) for k, v in (c.get("locale_pp_defines") or {}).items()
                },
                jar_sections=[
                    JarSection(
                        name=s["name"],
                        base=s["base"],
                        relativesrcdir=s["relativesrcdir"],
                        chrome_manifests=list(s["chrome_manifests"]),
                        pp_includes=list(s["pp_includes"]),
                        entries=[JarEntry(**e) for e in s["entries"]],
                    )
                    for s in c.get("jar_sections") or []
                ],
                localized_files=[
                    LocalizedFileGroup(subpath=g["subpath"], sources=list(g["sources"]))
                    for g in c.get("localized_files") or []
                ],
                localized_pp_files=[
                    LocalizedFileGroup(subpath=g["subpath"], sources=list(g["sources"]))
                    for g in c.get("localized_pp_files") or []
                ],
                localized_generated_files=[
                    LocalizedGenScript(
                        script=g["script"],
                        method=g["method"],
                        inputs=list(g["inputs"]),
                        outputs=list(g["outputs"]),
                        flags=list(g["flags"]),
                        force=g["force"],
                    )
                    for g in c.get("localized_generated_files") or []
                ],
            )
        )
    return StagingSpec(
        version=raw["version"],
        moz_app_id=raw["moz_app_id"],
        moz_app_version=raw["moz_app_version"],
        moz_app_displayname=raw["moz_app_displayname"],
        moz_build_app=raw["moz_build_app"],
        contexts=contexts,
    )


def _extract_staging_data(emitter, context):
    """Return a ``StagingContextData`` for ``context`` if it has any
    locale-relevant content, else ``None``.
    """
    has_jar = bool(context.get("JAR_MANIFESTS"))
    localized_files = context.get("LOCALIZED_FILES")
    localized_pp_files = context.get("LOCALIZED_PP_FILES")
    has_localized_files = bool(localized_files and any(localized_files.walk()))
    has_localized_pp_files = bool(localized_pp_files and any(localized_pp_files.walk()))
    has_localized_gen = bool(context.get("LOCALIZED_GENERATED_FILES"))
    has_locale_pp_defines = bool(context.get("LOCALE_PP_DEFINES"))

    if not (
        has_jar
        or has_localized_files
        or has_localized_pp_files
        or has_localized_gen
        or has_locale_pp_defines
    ):
        return None

    sd = StagingContextData(
        relsrcdir=context.relsrcdir,
        install_target=context.get("FINAL_TARGET") or "dist/bin",
        dist_subdir=context.get("DIST_SUBDIR") or "",
        defines=_coerce_defines(context.get("DEFINES")),
        locale_pp_defines=_coerce_locale_pp_defines(context.get("LOCALE_PP_DEFINES")),
    )

    if has_jar:
        for path in context["JAR_MANIFESTS"]:
            sd.jar_sections.extend(_extract_jar_sections(emitter, context, path))

    if has_localized_files:
        sd.localized_files = _extract_localized_files(localized_files)

    if has_localized_pp_files:
        sd.localized_pp_files = _extract_localized_files(localized_pp_files)

    if has_localized_gen:
        sd.localized_generated_files = _extract_localized_generated(context)

    # Skip contexts that ended up with no actual locale content (e.g. a
    # JAR_MANIFESTS context whose jar.mn has no locale entries).
    if not (
        sd.jar_sections
        or sd.localized_files
        or sd.localized_pp_files
        or sd.localized_generated_files
        or sd.locale_pp_defines
    ):
        return None
    return sd


def _coerce_defines(defines):
    """Coerce a context DEFINES value (InitializedDefines or dict-like) to a
    plain JSON-friendly dict.
    """
    if not defines:
        return {}
    items = defines.items() if hasattr(defines, "items") else dict(defines).items()
    out = {}
    for k, v in items:
        if isinstance(v, (str, int, bool, float)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _coerce_locale_pp_defines(defines):
    """Coerce a ``LOCALE_PP_DEFINES`` value (dict-of-dicts) to a JSON-friendly
    nested dict.
    """
    if not defines:
        return {}
    return {k: dict(v) for k, v in defines.items()}


def _extract_jar_sections(emitter, context, path):
    """Parse a jar.mn at emit time and yield ``JarSection``s for groups that
    contain at least one locale entry.

    Runs the preprocessor with ``AB_CD=en-US`` purely to satisfy
    ``#filter`` substitutions in the jar.mn syntax; the resulting
    ``source``/``output`` pairs are passed through verbatim to the spec.
    Locale-specific path resolution (against the merge tree) happens at
    stage_locale time.
    """
    pp = Preprocessor()
    defines = context.get("DEFINES")
    if defines:
        pp.context.update(defines)
    pp.context.update(emitter.config.defines)
    # Defer per-locale resolution to stage time. We can't leave
    # ``@AB_CD@`` literal because ``JarManifestParser``'s jar-name regex
    # rejects ``@`` characters. Substitute to a sentinel placeholder
    # composed of word characters (matches ``\w+``); ``stage_locale``
    # replaces the placeholder with the actual locale per-invocation.
    pp.context.update(AB_CD=LOCALE_PLACEHOLDER)
    pp.out = JarManifestParser()
    try:
        pp.do_include(path.full_path)
    except DeprecatedJarManifest as e:
        raise DeprecatedJarManifest(
            f"Parsing error while processing {path.full_path}: {e}"
        )

    for jarinfo in pp.out:
        has_locale_entry = any(e.is_locale for e in jarinfo.entries)
        has_locale_manifest = any(
            m.lstrip().startswith("locale ") for m in jarinfo.chrome_manifests
        )
        is_localization_block = jarinfo.base == "localization"
        if not (has_locale_entry or has_locale_manifest or is_localization_block):
            continue
        yield JarSection(
            name=jarinfo.name,
            base=jarinfo.base or "",
            relativesrcdir=jarinfo.relativesrcdir or "",
            chrome_manifests=list(jarinfo.chrome_manifests),
            pp_includes=sorted(pp.includes),
            entries=[
                JarEntry(
                    source=e.source,
                    output=e.output,
                    is_locale=e.is_locale,
                    preprocess=e.preprocess,
                )
                for e in jarinfo.entries
            ],
        )


def _extract_localized_files(files):
    """Convert a HierarchicalStringList of LOCALIZED_FILES/LOCALIZED_PP_FILES
    into a list of ``LocalizedFileGroup`` (one per install subpath).
    """
    groups = []
    for subpath, entries in files.walk():
        sources = [str(f) for f in entries]
        if sources:
            groups.append(LocalizedFileGroup(subpath=subpath, sources=sources))
    return groups


def _extract_localized_generated(context):
    """Convert a context's ``LOCALIZED_GENERATED_FILES`` table into a list of
    ``LocalizedGenScript``s. Outputs are stored verbatim — they may contain
    ``{AB_CD}``/``{AB_rCD}`` placeholders that ``stage_locale`` resolves.
    Script paths are resolved to absolute paths through ``SourcePath`` so
    ``stage_locale`` can ``importlib.util.spec_from_file_location`` them
    directly without re-resolving.
    """
    table = context["LOCALIZED_GENERATED_FILES"]
    out = []
    for entry in table:
        flags = table[entry]
        outputs = list(entry) if isinstance(entry, tuple) else [entry]
        if not flags.script:
            continue
        script = flags.script
        method = "main"
        if ".py:" in script:
            script, method = script.rsplit(".py:", 1)
            script += ".py"
        script = SourcePath(context, script).full_path
        out.append(
            LocalizedGenScript(
                script=script,
                method=method,
                inputs=[str(i) for i in (flags.inputs or [])],
                outputs=outputs,
                flags=list(flags.flags or []),
                force=bool(flags.force),
            )
        )
    return out
