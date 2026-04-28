# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""In-process per-locale staging.

``stage_locale`` materializes ``dist/xpi-stage/locale-<ab_cd>/`` from the
locale-independent staging spec (produced by
``mozbuild.frontend.staging_spec.emit_staging_spec``) and a populated
merge tree at ``$topobjdir/l10n_merge/<ab_cd>/``. Replaces the legacy
``populate-locale-<ab_cd>`` make recipe.

The mach commands (``mach langpack``, ``mach repackage-zip``,
``mach package-multi-locale``, ``mach repackage-single-locales``) call
``stage_locale`` instead of going through make.
"""

import fnmatch
import importlib.util
import os
import re
import shutil
from glob import glob

import mozpack.path as mozpath
from mozpack.chrome.manifest import parse_manifest_line

from mozbuild.frontend.staging_spec import LOCALE_PLACEHOLDER, load_staging_spec
from mozbuild.preprocessor import Preprocessor
from mozbuild.util import FileAvoidWrite


def stage_locale(
    locale,
    spec_path,
    merge_tree,
    dest_xpi_stage,
    *,
    topsrcdir=None,
    topobjdir=None,
):
    """Materialize ``<dest_xpi_stage>/`` (typically
    ``$topobjdir/dist/xpi-stage/locale-<locale>/``) for ``locale``.

    Walks the staging spec at ``spec_path``, resolves paths through
    ``merge_tree`` (typically ``$topobjdir/l10n_merge/<locale>/``), and
    runs file copies, preprocessing, generator-script invocations, and
    chrome.manifest assembly. Returns nothing; raises on errors.
    """
    spec = load_staging_spec(spec_path)
    state = _StageState(
        locale=locale,
        spec=spec,
        merge_tree=merge_tree,
        dest=dest_xpi_stage,
        topsrcdir=topsrcdir,
        topobjdir=topobjdir,
    )
    if os.path.exists(dest_xpi_stage):
        shutil.rmtree(dest_xpi_stage)
    os.makedirs(dest_xpi_stage, exist_ok=True)

    for context in spec.contexts:
        _stage_context(state, context)

    _write_chrome_manifests(state)


class _StageState:
    """Per-invocation state for ``stage_locale``."""

    def __init__(self, *, locale, spec, merge_tree, dest, topsrcdir, topobjdir):
        self.locale = locale
        self.spec = spec
        self.merge_tree = merge_tree
        self.dest = dest
        self.topsrcdir = topsrcdir
        self.topobjdir = topobjdir
        # Map relpath-in-dest -> ordered list of chrome.manifest entry strings.
        self.manifest_entries = {}


def _stage_context(state, context):
    # Gen-file outputs come first so ``LOCALIZED_FILES`` / ``LOCALIZED_PP_FILES``
    # entries that reference them via ``!output`` can resolve and re-route to
    # the requested install subpath.
    for gen in context.localized_generated_files:
        _run_localized_generated(state, context, gen)
    for section in context.jar_sections:
        _stage_jar_section(state, context, section)
    for group in context.localized_files:
        _stage_file_group(state, context, group, preprocess=False)
    for group in context.localized_pp_files:
        _stage_file_group(state, context, group, preprocess=True)


def _stage_jar_section(state, context, section):
    """Process one jar.mn group's entries.

    Mirrors the original ``_process_localized_jar_section``'s
    ``block_keep_all_entries`` rule: a section participates in the
    langpack as a whole when it's a ``[localization]`` block or has any
    ``% locale ...`` chrome.manifest entry — all entries are staged in
    that case (en-US fallback sources resolve against the moz.build's
    srcdir; locale sources resolve against the merge tree). Sections
    that are neither only stage their explicitly ``%``-marked locale
    entries.

    The spec captures jar.mn data with ``@AB_CD@`` placeholders left
    intact (the staging-spec emitter parses with a sentinel for AB_CD so
    substitution is deferred). All locale-templated strings —
    ``section.name``, ``entry.source``, ``entry.output``,
    ``chrome_manifests`` — are substituted to ``state.locale`` here.
    """
    install_target = _resolve_jar_install_target(context, section)
    defines = _resolve_locale_defines(state, context)

    # ``section.relativesrcdir`` mirrors the jar.mn ``relativesrcdir``
    # directive when present; otherwise sources resolve against the
    # owning moz.build's directory.
    src_relsrcdir = section.relativesrcdir or context.relsrcdir or ""

    section_name = _sub_locale(section.name, state.locale)

    is_localization_block = section.base == "localization"
    block_keep_all_entries = is_localization_block or any(
        _sub_locale(m, state.locale).lstrip().startswith("locale ")
        for m in section.chrome_manifests
    )

    for entry in section.entries:
        if not (entry.is_locale or block_keep_all_entries):
            continue
        entry_source = _sub_locale(entry.source, state.locale)
        entry_output = _sub_locale(entry.output, state.locale)
        if any(c in entry_source for c in "*?["):
            for match_src, match_rel in _expand_wildcard_jar_source(
                state, src_relsrcdir, entry_source, entry.is_locale
            ):
                output_path = _resolve_wildcard_output(entry_output, match_rel)
                dest_rel = mozpath.join(install_target, section_name, output_path)
                dest_path = mozpath.join(state.dest, dest_rel)
                if entry.preprocess:
                    _preprocess_to(match_src, dest_path, defines, state)
                else:
                    _link_or_copy(match_src, dest_path)
            continue
        src = _resolve_jar_source(state, src_relsrcdir, entry_source, entry.is_locale)
        dest_rel = mozpath.join(install_target, section_name, entry_output)
        dest_path = mozpath.join(state.dest, dest_rel)

        if entry.preprocess:
            _preprocess_to(src, dest_path, defines, state)
        else:
            _link_or_copy(src, dest_path)

    # Chrome manifest registration entries for the section. The chromebase
    # substitution mirrors JarMaker's default (non-extension-manifest) mode:
    # entries land in <install_target>/<section.name>.manifest, with a
    # `manifest <section.name>.manifest` reference in the install target's
    # chrome.manifest.
    if section.chrome_manifests:
        manifest_relpath = mozpath.join(install_target, f"{section_name}.manifest")
        chromebase = mozpath.basename(section_name) + "/"
        base = mozpath.dirname(section_name)
        for raw in section.chrome_manifests:
            raw = _sub_locale(raw, state.locale)
            entry = parse_manifest_line(base, raw.replace("%", chromebase))
            state.manifest_entries.setdefault(manifest_relpath, []).append(str(entry))
        # Cross-reference from the top-level chrome.manifest.
        top_manifest = mozpath.join(install_target, "chrome.manifest")
        if top_manifest != manifest_relpath:
            ref = f"manifest {mozpath.relpath(manifest_relpath, install_target)}"
            state.manifest_entries.setdefault(top_manifest, []).append(ref)


def _resolve_jar_install_target(context, section):
    """Compute the per-locale install subdir within ``dest`` for ``section``.

    Mirrors the original ``_process_localized_jar_section`` layout: the
    xpi-stage tree mirrors the en-US dist tree, so ``dist_subdir`` (e.g.
    ``browser`` for browser-rooted moz.builds) leads, with the section's
    ``base`` joined onto it (e.g. ``localization`` for the addon
    [localization] block).
    """
    parts = []
    if context.dist_subdir:
        parts.append(context.dist_subdir)
    if section.base:
        parts.append(section.base)
    if not parts:
        return ""
    return mozpath.normpath("/".join(parts))


def _sub_locale(s, locale):
    """Substitute ``staging_spec.LOCALE_PLACEHOLDER`` -> ``locale`` in a
    captured jar.mn string.

    The staging-spec emitter parses jar.mn files with
    ``AB_CD=LOCALE_PLACEHOLDER`` (a word-only sentinel that fits
    JarManifestParser's regex) so substitution is deferred. Stage time
    replaces the placeholder with the actual locale.
    """
    return s.replace(LOCALE_PLACEHOLDER, locale) if s else s


def _resolve_jar_source(state, relsrcdir, source, is_locale):
    """Resolve a jar.mn entry's source path against the merge tree (locale
    entries) or the en-US source tree (non-locale entries inside a locale
    group, which we don't stage here but resolve for completeness).

    ``relsrcdir`` is the source directory the entry resolves against —
    typically ``section.relativesrcdir`` if the jar.mn declared one, else
    the owning moz.build's relsrcdir. ``source`` should already have its
    ``@AB_CD@`` placeholder substituted.
    """
    if is_locale:
        return mozpath.join(state.merge_tree, _merge_subdir_for(relsrcdir), source)
    # Non-locale entries: source is interpreted against topsrcdir or
    # the moz.build srcdir. We don't actually stage them, but compute
    # the path for symmetry.
    if source.startswith("/"):
        return mozpath.join(state.topsrcdir or "", source.lstrip("/"))
    return mozpath.join(state.topsrcdir or "", relsrcdir, source)


def _expand_wildcard_jar_source(state, relsrcdir, source, is_locale):
    """Glob-expand a wildcard ``source``.

    Locale-aware sources resolve through the merge tree; non-locale sources
    (en-US fallbacks within a localization-block section) resolve against
    topsrcdir (when prefixed with ``/``) or the moz.build srcdir.

    Mirrors JarMaker's wildcard handling: split the source at the first
    component containing a wildcard, treat the prefix as a base directory,
    and glob the remainder relative to that base. Yields
    ``(absolute_match, relative_match)`` pairs where the relative path is
    rooted at the wildcard split point — ready to combine with the
    entry's output template.
    """
    if is_locale:
        base = mozpath.join(state.merge_tree, _merge_subdir_for(relsrcdir))
    elif source.startswith("/"):
        base = state.topsrcdir or ""
        source = source.lstrip("/")
    else:
        base = mozpath.join(state.topsrcdir or "", relsrcdir)

    parts = source.split("/")
    prefix_parts = []
    pattern_parts = []
    for i, part in enumerate(parts):
        if any(c in part for c in "*?["):
            pattern_parts = parts[i:]
            break
        prefix_parts.append(part)
    if not pattern_parts:
        return

    pattern_base = mozpath.join(base, *prefix_parts) if prefix_parts else base
    full_pattern = mozpath.join(pattern_base, "/".join(pattern_parts))

    for match in sorted(glob(full_pattern, recursive=True)):
        if not os.path.isfile(match):
            continue
        match_norm = match.replace(os.sep, "/")
        rel = mozpath.relpath(match_norm, pattern_base)
        yield match_norm, rel


def _resolve_wildcard_output(output_template, match_rel):
    """Compute a per-match dest path given an entry's output template and
    the relative match path from ``_expand_wildcard_jar_source``.

    If ``output_template`` itself contains wildcards, splits on the first
    wildcard component and prepends the prefix to ``match_rel``. Otherwise
    treats the output template as a directory prefix and joins the match's
    relative path under it.
    """
    parts = output_template.split("/")
    prefix_parts = []
    for part in parts:
        if any(c in part for c in "*?["):
            break
        prefix_parts.append(part)
    prefix = "/".join(prefix_parts)
    return mozpath.join(prefix, match_rel) if prefix else match_rel


def _stage_file_group(state, context, group, *, preprocess):
    """Process a LOCALIZED_FILES or LOCALIZED_PP_FILES group.

    Sources may be ``en-US/...`` (resolves via the merge tree),
    ``/path/locales/en-US/...`` (topsrcdir-rooted, merge tree),
    ``!output`` (objdir-relative — typically a LOCALIZED_GENERATED_FILES
    output), or a glob pattern.
    """
    # Normalize ``..`` segments so e.g. ``LOCALIZED_FILES[".."]`` from a
    # context with ``DIST_SUBDIR=browser`` resolves to the dist root rather
    # than the literal ``browser/..`` string.
    install_target = mozpath.normpath(
        mozpath.join(_locale_target_tail(context), group.subpath)
    )
    if install_target == ".":
        install_target = ""
    defines = _resolve_locale_defines(state, context) if preprocess else None

    for src_template in group.sources:
        for src, dest_rel in _resolve_localized_sources(
            state, context, src_template, install_target
        ):
            dest_path = mozpath.join(state.dest, dest_rel)
            # ``!output`` references can resolve to the same path the gen
            # step already wrote (when ``LOCALIZED_FILES`` doesn't re-route
            # the output to a different subpath). Skip the redundant copy.
            if mozpath.normpath(src) == mozpath.normpath(dest_path):
                continue
            if preprocess:
                _preprocess_to(src, dest_path, defines, state)
            else:
                _link_or_copy(src, dest_path)


def _locale_target_tail(context):
    """Compute the per-locale install subdir within ``dest`` derived from
    ``context.install_target``. ``dist/bin/<rest>`` maps to ``<rest>``;
    other targets are rejected at extraction time.
    """
    target = context.install_target
    if target.startswith("dist/bin"):
        tail = target[len("dist/bin") :]
        return tail.lstrip("/")
    return ""


def _resolve_localized_sources(state, context, src_template, install_target):
    """Yield (src_abs, dest_rel) pairs for one LOCALIZED_FILES source template
    against the current locale's merge tree.

    A leading ``!`` references an objdir output (LOCALIZED_GENERATED_FILES);
    we map it to the merge tree's location for the per-locale rerun
    output. Other templates with ``en-US/`` prefix or ``/locales/en-US/``
    embedded resolve against the merge tree. Glob patterns expand against
    the resolved merge-tree directory.
    """
    if src_template.startswith("!"):
        # Output of a LOCALIZED_GENERATED_FILES script run for this locale.
        # _run_localized_generated writes outputs to dest directly; the
        # LOCALIZED_FILES reference here re-routes a generated output into
        # the desired install subpath. Source is the dest path computed
        # by the gen step.
        gen_output = src_template[1:]
        src_abs = mozpath.join(
            state.dest,
            _gen_output_install_subdir(context),
            mozpath.basename(gen_output),
        )
        dest_rel = mozpath.join(install_target, mozpath.basename(gen_output))
        if not os.path.exists(src_abs):
            # The gen step writes directly to its install target; if the
            # LOCALIZED_FILES re-route differs, treat the gen output as
            # the source and copy.
            src_abs = mozpath.join(state.dest, mozpath.basename(gen_output))
        if os.path.exists(src_abs):
            yield src_abs, dest_rel
        return

    if src_template.startswith("en-US/"):
        rest = src_template[len("en-US/") :]
        merge_subdir = _merge_subdir_for(context.relsrcdir)
        src_abs = mozpath.join(state.merge_tree, merge_subdir, rest)
    elif "/locales/en-US/" in src_template:
        # Topsrcdir- or srcdir-rooted with locales/en-US marker.
        if src_template.startswith("/"):
            rel = src_template.lstrip("/")
        else:
            rel = mozpath.join(context.relsrcdir, src_template)
        before, rest = rel.split("/locales/en-US/", 1)
        src_abs = mozpath.join(state.merge_tree, before, rest)
    else:
        # Plain pattern relative to the moz.build srcdir, resolved via
        # merge tree (no en-US/ prefix is allowed for hunspell-style
        # patterns; treated as already-merged).
        merge_subdir = _merge_subdir_for(context.relsrcdir)
        src_abs = mozpath.join(state.merge_tree, merge_subdir, src_template)

    if "*" in src_abs or "?" in src_abs or "[" in src_abs:
        for match in sorted(glob(src_abs)):
            if os.path.isfile(match):
                dest_rel = mozpath.join(install_target, mozpath.basename(match))
                yield match, dest_rel
        return

    if os.path.exists(src_abs):
        # Use the basename of the original template for the dest, mirroring
        # the en-US install behavior.
        dest_basename = mozpath.basename(src_template)
        dest_rel = mozpath.join(install_target, dest_basename)
        yield src_abs, dest_rel


def _merge_subdir_for(relsrcdir):
    """Mirrors EXPAND_LOCALE_SRCDIR: strip a trailing ``/locales`` so the
    merge subdir matches L10NBASEDIR layout.
    """
    if relsrcdir.endswith("/locales"):
        return relsrcdir[: -len("/locales")]
    if relsrcdir == "locales":
        return ""
    return relsrcdir


def _gen_output_install_subdir(context):
    """The directory inside ``dest`` that ``_run_localized_generated`` writes
    its outputs into for ``context``. By default outputs land at
    ``<locale_target_tail>/<basename>``; LOCALIZED_FILES re-routes can
    move them elsewhere via the ``!output`` reference.
    """
    return _locale_target_tail(context)


def _run_localized_generated(state, context, gen):
    """Invoke a LOCALIZED_GENERATED_FILES script for the current locale and
    write its outputs into the staging tree.

    Outputs may contain ``{AB_CD}`` / ``{AB_rCD}`` placeholders. Inputs
    with ``en-US/`` prefix or ``/locales/en-US/`` embedded resolve through
    the merge tree; other inputs resolve against srcdir.
    """
    substs = {"AB_CD": state.locale, "AB_rCD": _ab_rcd(state.locale)}
    resolved_outputs = []
    for output in gen.outputs:
        try:
            resolved_outputs.append(output.format(**substs))
        except KeyError as e:
            raise ValueError(
                f"{e.args[0]} not in {', '.join(sorted(substs))} is not a "
                f"valid substitution in {output}"
            )

    resolved_inputs = []
    for inp in gen.inputs:
        if inp.startswith("en-US/"):
            merge_subdir = _merge_subdir_for(context.relsrcdir)
            rest = inp[len("en-US/") :]
            resolved_inputs.append(mozpath.join(state.merge_tree, merge_subdir, rest))
        elif "/locales/en-US/" in inp:
            if inp.startswith("/"):
                rel = inp.lstrip("/")
            else:
                rel = mozpath.join(context.relsrcdir, inp)
            before, rest = rel.split("/locales/en-US/", 1)
            resolved_inputs.append(mozpath.join(state.merge_tree, before, rest))
        # Srcdir-relative or topsrcdir-rooted path.
        elif inp.startswith("/"):
            resolved_inputs.append(mozpath.join(state.topsrcdir or "", inp.lstrip("/")))
        else:
            resolved_inputs.append(
                mozpath.join(state.topsrcdir or "", context.relsrcdir, inp)
            )

    out_dir = mozpath.join(state.dest, _gen_output_install_subdir(context))
    os.makedirs(out_dir, exist_ok=True)
    out_paths = [mozpath.join(out_dir, mozpath.basename(o)) for o in resolved_outputs]

    # Skip up-to-date unless forced.
    if not gen.force and out_paths and all(os.path.exists(p) for p in out_paths):
        return

    main_fn = _load_script(gen.script, gen.method)

    # Convention from ``mozbuild.action.file_generate``: ``main(output,
    # *inputs, **kwargs)`` writes to ``output`` (a writable file). The
    # primary output is the first one; multi-output scripts derive
    # siblings from its directory. Pass ``locale=`` as a kwarg, mirroring
    # the file_generate ``--locale`` flag.
    # Use FileAvoidWrite to match ``mozbuild.action.file_generate``: scripts
    # may ``print(..., file=output)`` (str writes) or ``output.write(b"...")``
    # (bytes writes); FileAvoidWrite tolerates both.
    primary_output = out_paths[0]
    try:
        with FileAvoidWrite(primary_output, readmode="rb") as output:
            try:
                ret = main_fn(output, *resolved_inputs, locale=state.locale)
            except Exception:
                output.avoid_writing_to_file()
                raise
    except Exception:
        if os.path.exists(primary_output):
            os.unlink(primary_output)
        raise
    # Per file_generate convention: success is None, 0, False, or a set
    # naming the files written.
    if ret not in (None, 0, False) and not isinstance(ret, set):
        raise RuntimeError(
            f"LOCALIZED_GENERATED_FILES script {gen.script}:{gen.method} "
            f"returned {ret} for locale {state.locale}"
        )


def _load_script(script_path, method_name):
    """Load ``method_name`` from the Python file at ``script_path``."""
    spec = importlib.util.spec_from_file_location(
        "_localized_generated_script", script_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, method_name)


def _resolve_locale_defines(state, context):
    """Compose the preprocessor define dict for ``context`` and the current
    locale: context DEFINES + ACDEFINES + LOCALE_PP_DEFINES-resolved +
    AB_CD.
    """
    out = {}
    out.update(context.defines or {})
    # ACDEFINES would be threaded through state if needed; LOCALE_PP_DEFINES
    # below is the locale-aware piece.
    for define, locale_map in (context.locale_pp_defines or {}).items():
        resolved = _resolve_locale_pp_define(locale_map, state.locale)
        if resolved is not None:
            out[define] = resolved
    out["AB_CD"] = state.locale
    return out


def _resolve_locale_pp_define(locale_map, locale):
    """Look up ``locale`` in a LOCALE_PP_DEFINES inner-dict. Exact match wins
    over fnmatch patterns; returns None if neither matches.
    """
    if locale in locale_map:
        return locale_map[locale]
    for pattern, value in locale_map.items():
        if any(c in pattern for c in "*?["):
            if fnmatch.fnmatchcase(locale, pattern):
                return value
    return None


_LPROJ_RE = re.compile(r"^([a-z]{2,3})(?:-(.+))?$")


def _ab_rcd(locale):
    """Compute the AB_rCD form (used by some generator scripts): same as
    AB_CD except the region (after ``-``) is lower-cased and underscored
    rather than dashed (e.g. ``zh-TW`` -> ``zh-tw``).
    """
    m = _LPROJ_RE.match(locale)
    if not m:
        return locale
    base, region = m.group(1), m.group(2)
    if region:
        return f"{base}-{region.lower()}"
    return base


def _preprocess_to(src, dest, defines, state):
    """Run the preprocessor against ``src`` and write to ``dest`` using
    ``defines`` as the substitution dict. Suppresses missing-directive
    warnings (mirrors the legacy --silence-missing-directive-warnings
    flag used by LOCALIZED_PP_FILES).
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    pp = Preprocessor(defines=defines)
    pp.do_filter("substitution")
    with open(dest, "w", encoding="utf-8", newline="\n") as out:
        pp.out = out
        pp.do_include(src)


def _link_or_copy(src, dest):
    """Copy ``src`` to ``dest``, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)


def _write_chrome_manifests(state):
    """Write out all collected chrome.manifest files. Each file contains a
    sorted-stable list of unique entries (preserving insertion order
    within each file).
    """
    for relpath, entries in state.manifest_entries.items():
        seen = set()
        ordered = []
        for e in entries:
            if e in seen:
                continue
            seen.add(e)
            ordered.append(e)
        path = mozpath.join(state.dest, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(ordered) + "\n")
