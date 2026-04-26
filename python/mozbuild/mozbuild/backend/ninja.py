# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja backend.

Emits a single `build.ninja` at `$topobjdir`.
"""

import json
import os
from collections import defaultdict

import mozpack.path as mozpath

from mozbuild.backend.common import CommonBackend
from mozbuild.backend.ninja_syntax import (
    NinjaWriter,
    response_arg,
)
from mozbuild.backend.ninja_syntax import (
    path as n_path,
)
from mozbuild.backend.ninja_syntax import (
    value as n_value,
)
from mozbuild.dirutils import ensureParentDir
from mozbuild.frontend.context import AbsolutePath, ObjDirPath
from mozbuild.frontend.data import (
    ChromeManifestEntry,
    ComputedFlags,
    ContextDerived,
    Defines,
    FinalTargetFiles,
    FinalTargetPreprocessedFiles,
    GeneratedFile,
    HostLibrary,
    HostProgram,
    HostSimpleProgram,
    HostSources,
    IPDLCollection,
    JARManifest,
    LocalInclude,
    LocalizedFiles,
    LocalizedPreprocessedFiles,
    PerSourceFlag,
    Program,
    RustLibrary,
    SandboxedWasmLibrary,
    SharedLibrary,
    SimpleProgram,
    Sources,
    StaticLibrary,
    TestManifest,
    UnifiedSources,
    VariablePassthru,
    WasmSources,
)


def _strip_tests(path):
    """Strip a leading `_tests/` from a manifest destination path.

    `TestHarnessFiles.install_target == "_tests"`, so destinations look
    like `_tests/<sub>/<file>`. The `_ninja_test_files` install manifest
    is installed with `_tests/` as its `install_dir`, so its entries
    must be relative to `_tests/`.
    """
    if path == "_tests":
        return ""
    if path.startswith("_tests/"):
        return path[len("_tests/") :]
    return path


class NinjaBackend(CommonBackend):
    """Emits `build.ninja`."""

    def _init(self):
        super()._init()

        env = self.environment
        self._topsrcdir = mozpath.normsep(env.topsrcdir)
        self._topobjdir = mozpath.normsep(env.topobjdir)

        # Collected objects, keyed by the directory they belong to.
        self._sources_by_dir = defaultdict(list)  # relobjdir -> [Sources, ...]
        self._unified_by_dir = defaultdict(list)  # relobjdir -> [UnifiedSources, ...]
        self._host_sources_by_dir = defaultdict(list)  # relobjdir -> [HostSources, ...]
        self._static_libs = []
        self._shared_libs = []
        self._rust_libs = []
        self._programs = []
        self._host_libraries = []
        self._host_programs = []
        self._generated_files = []
        # Per-directory ComputedFlags. The emitter yields TWO ComputedFlags
        # objects per context — one from COMPILE_FLAGS (keys like CXXFLAGS,
        # CFLAGS, CXX_LDFLAGS, C_LDFLAGS) and one from LINK_FLAGS (key
        # LDFLAGS only, including `-DEF:<deffile>`). Keep a list so neither
        # overwrites the other on lookup.
        self._computed_flags = defaultdict(list)  # relobjdir -> [ComputedFlags, ...]
        self._per_source_flags = defaultdict(list)  # relobjdir -> [PerSourceFlag, ...]
        self._variable_passthru = {}  # relobjdir -> VariablePassthru

        # File install targets: Exports (→ dist/include), ObjdirFiles (→
        # objdir root), ObjdirPreprocessedFiles (→ objdir root, run through
        # preprocessor).
        self._installs = []  # [(src, dest)] — simple copies
        self._pp_installs = []  # [(src, dest, defines_dict)] — preprocess + copy

        # ChromeManifestEntry objects: per-manifest-path collection of
        # entry strings. `XPCOM_MANIFESTS` in moz.build emits these to
        # add `manifest components/X.manifest` lines to the top-level
        # chrome.manifest at install time.
        self._chrome_manifest_entries = defaultdict(set)

        # TEST_HARNESS_FILES entries: per-entry tuples deferred to
        # `consume_finished`, where they're folded into the same
        # `_ninja_test_files` `InstallManifest` we build for TestManifest.
        # Tuple shape: ("link", src, dest) | ("pattern", base, pattern,
        # dest_dir) | ("optional", dest).
        self._test_harness_entries = []

        # TestManifest objects of every flavor (mochitest, xpcshell,
        # python, browser-chrome, marionette, reftest, crashtest, etc.).
        # Mozmake's recursivemake backend folds these into the
        # `_test_files` install manifest plus a per-flavor master
        # `<install_prefix>/<flavor>.toml`. We mirror that natively so
        # the ninja path stages tests without depending on RecursiveMake
        # co-run. Per-binary `OPTIONAL_EXISTS` markers for test support
        # programs/libraries (`_process_test_support_file` in
        # recursivemake) are folded in alongside.
        self._test_manifests = []

        # Per-context Defines / LocalInclude, used to expand make-style
        # `$(DEFINES)` / `$(LOCAL_INCLUDES)` references in GeneratedFile
        # flags (e.g. config/external/ffi/preprocess_libffi_asm.py).
        self._defines_by_dir = defaultdict(list)  # relobjdir -> [Defines, ...]
        self._local_includes_by_dir = defaultdict(
            list
        )  # relobjdir -> [LocalInclude, ...]

        # IPDLCollection is a singleton — the emitter produces exactly one
        # IPDLCollection for the whole tree, gathered from every
        # IPDL_SOURCES / PREPROCESSED_IPDL_SOURCES declaration. Track the
        # most recent one we receive and emit a single ipdl.py invocation
        # for it at write time.
        self._ipdl_collection = None
        # Populated by `_emit_ipdl_statements`; consumed by
        # `_emit_compile_statements` to fold into `.ninja-generated`.
        self._ipdl_outputs = []

        # WebIDL: captured by the `_handle_webidl_build` override (called by
        # `CommonBackend._handle_webidl_collection` after writing
        # file-lists.json and the unified source files). We stash the
        # parameters and emit the rule + outputs at write time.
        self._webidl = None
        # Populated by `_emit_webidl_statements`; folded into
        # `.ninja-generated` by `_emit_compile_statements`.
        self._webidl_outputs = []

        # XPIDL: captured by the `_handle_idl_manager` override (called from
        # `CommonBackend.consume_finished` if any XPIDL_MODULE was seen).
        # We stash the manager and emit per-module + aggregate-link edges
        # at write time.
        self._xpidl_manager = None
        # Populated by `_emit_xpidl_statements`; folded into
        # `.ninja-generated` by `_emit_compile_statements`.
        self._xpidl_outputs = []

        # Wasm: WASM_SOURCES compile to wasm objects via WASM_CC/WASM_CXX,
        # and SANDBOXED_WASM_LIBRARY_NAME links them into a single .wasm
        # binary that downstream `GeneratedFile` rules (e.g. wasm2c) consume.
        self._wasm_sources_by_dir = defaultdict(list)  # relobjdir -> [WasmSources, ...]
        self._wasm_libraries = []

        # jar.mn manifests packaged via mozbuild.action.jar_maker.
        self._jar_manifests = []

    def consume_object(self, obj):
        if not isinstance(obj, ContextDerived):
            return False

        relobjdir = obj.relobjdir

        # Backend bookkeeping first (CommonBackend returns True for some
        # types like UnifiedSources and would short-circuit this).
        if isinstance(obj, UnifiedSources):
            self._unified_by_dir[relobjdir].append(obj)
        elif isinstance(obj, Sources):
            self._sources_by_dir[relobjdir].append(obj)
        elif isinstance(obj, HostSources):
            self._host_sources_by_dir[relobjdir].append(obj)
        elif isinstance(obj, WasmSources):
            self._wasm_sources_by_dir[relobjdir].append(obj)
        elif isinstance(obj, RustLibrary):
            self._rust_libs.append(obj)
        elif isinstance(obj, SandboxedWasmLibrary):
            self._wasm_libraries.append(obj)
        elif isinstance(obj, StaticLibrary):
            self._static_libs.append(obj)
        elif isinstance(obj, SharedLibrary):
            self._shared_libs.append(obj)
        elif isinstance(obj, HostLibrary):
            self._host_libraries.append(obj)
        elif isinstance(obj, (Program, SimpleProgram)):
            self._programs.append(obj)
        elif isinstance(obj, (HostProgram, HostSimpleProgram)):
            self._host_programs.append(obj)
        elif isinstance(obj, GeneratedFile):
            self._generated_files.append(obj)
        elif isinstance(obj, IPDLCollection):
            self._ipdl_collection = obj
        elif isinstance(obj, JARManifest):
            self._jar_manifests.append(obj)
        elif isinstance(obj, PerSourceFlag):
            self._per_source_flags[relobjdir].append(obj)
        elif isinstance(obj, ComputedFlags):
            self._computed_flags[relobjdir].append(obj)
        elif isinstance(obj, Defines):
            self._defines_by_dir[relobjdir].append(obj)
        elif isinstance(obj, LocalInclude):
            self._local_includes_by_dir[relobjdir].append(obj)
        elif isinstance(obj, VariablePassthru):
            self._variable_passthru[relobjdir] = obj
        elif isinstance(obj, LocalizedPreprocessedFiles):
            # LOCALIZED_PP_FILES: en-US preprocessed install. en-US source
            # is `<srcdir>/en-US/<rest>` (already what `f.full_path`
            # resolves to). Mozmake adds `-DAB_CD=en-US` at preprocess
            # time (config/config.mk:317); mirror that in the per-edge
            # defines so AB_CD-conditional `#filter` blocks resolve.
            # Non-en-US locales are staged at command time via
            # `mach langpack` / `mach repackage-zip`.
            install_target = obj.install_target
            defines = {"AB_CD": "en-US"}
            for subpath, files in obj.files.walk():
                for f in files:
                    if isinstance(f, ObjDirPath) or "*" in f:
                        continue
                    src = mozpath.normsep(f.full_path)
                    basename = FinalTargetPreprocessedFiles.get_obj_basename(f)
                    dst = mozpath.join(
                        self._topobjdir, install_target, subpath, basename
                    )
                    self._pp_installs.append((src, dst, defines))
        elif isinstance(obj, LocalizedFiles):
            # LOCALIZED_FILES: en-US install. Non-en-US locales are
            # staged at command time via `mach langpack`.
            install_target = obj.install_target
            for subpath, files in obj.files.walk():
                for f in files:
                    if isinstance(f, ObjDirPath) or "*" in f:
                        continue
                    src = mozpath.normsep(f.full_path)
                    dst = mozpath.join(
                        self._topobjdir, install_target, subpath, f.target_basename
                    )
                    self._installs.append((src, dst))
        elif isinstance(obj, FinalTargetPreprocessedFiles):
            # Preprocessed files aren't in the install manifests — they
            # need a preprocessor step. Track them so we emit ninja rules
            # for each.
            install_target = obj.install_target
            defines = {}
            if getattr(obj, "defines", None):
                defines = obj.defines.defines
            for subpath, files in obj.files.walk():
                for f in files:
                    src = mozpath.normsep(f.full_path)
                    basename = FinalTargetPreprocessedFiles.get_obj_basename(f)
                    dst = mozpath.join(
                        self._topobjdir,
                        install_target,
                        subpath,
                        basename,
                    )
                    self._pp_installs.append((src, dst, defines))
        elif isinstance(obj, FinalTargetFiles):
            # Mozmake's `_process_final_target_files` (recursivemake.py:
            # 1576) routes entries by install_target. Two distinct paths
            # matter to us:
            #
            # 1. install_target starts with `_tests` (TestHarnessFiles,
            #    or any moz.build that sets `FINAL_TARGET = "_tests/..."`):
            #    SourcePath → LINK, wildcard → PATTERN_LINK, ObjDirPath
            #    → OPTIONAL_EXISTS (real install via the `_installs`
            #    post-batch), AbsolutePath → OPTIONAL_EXISTS. Fold into
            #    `_test_harness_entries`; consume_finished merges them
            #    into the `_ninja_test_files` `InstallManifest`.
            #
            # 2. Any other install_target: only ObjDirPath entries get a
            #    real install edge here; SourcePath entries are covered
            #    by the appropriate install manifest mozmake wrote at
            #    configure time.
            install_target = obj.install_target
            is_test = install_target.startswith("_tests")
            for subpath, files in obj.files.walk():
                dest_dir = mozpath.join(install_target, subpath)
                for f in files:
                    dest_file = mozpath.join(dest_dir, f.target_basename)
                    if isinstance(f, ObjDirPath):
                        src = mozpath.normsep(f.full_path)
                        dst = mozpath.join(self._topobjdir, dest_file)
                        self._installs.append((src, dst))
                        if is_test:
                            self._test_harness_entries.append(("optional", dest_file))
                    elif not is_test:
                        # Source-tree entries for non-test install_targets
                        # are covered by mozmake's install manifests.
                        continue
                    elif isinstance(f, AbsolutePath):
                        self._test_harness_entries.append(("optional", dest_file))
                    elif "*" in f:
                        # Wildcard SourcePath. Mozmake distinguishes
                        # topsrcdir-absolute (leading `/`) from srcdir-
                        # relative for the pattern base; mirror that.
                        if f.startswith("/"):
                            basepath, pattern = os.path.split(f.full_path)
                            self._test_harness_entries.append((
                                "pattern",
                                basepath,
                                pattern,
                                dest_dir,
                            ))
                        else:
                            self._test_harness_entries.append((
                                "pattern",
                                f.srcdir,
                                str(f),
                                dest_dir,
                            ))
                    else:
                        # Plain SourcePath → LINK from full path to dest.
                        self._test_harness_entries.append((
                            "link",
                            mozpath.normsep(f.full_path),
                            dest_file,
                        ))
        elif isinstance(obj, ChromeManifestEntry):
            self._chrome_manifest_entries[obj.path].add(str(obj.entry))
        elif isinstance(obj, TestManifest):
            self._test_manifests.append(obj)

        # Side effects from CommonBackend (writes Unified_cpp_*.cpp files,
        # tracks generated sources, etc).
        CommonBackend.consume_object(self, obj)

        return True

    def _handle_ipdl_sources(
        self,
        ipdl_dir,
        sorted_ipdl_sources,
        sorted_nonstatic_ipdl_sources,
        sorted_static_ipdl_sources,
    ):
        # Required by CommonBackend.consume_object for IPDLCollection. The
        # NinjaBackend reads everything off the collection directly in
        # `_emit_ipdl_statements`, so this stub exists only to satisfy the
        # CommonBackend dispatch.
        pass

    def _handle_webidl_build(
        self,
        bindings_dir,
        unified_source_mapping,
        webidls,
        expected_build_output_files,
        global_define_files,
    ):
        # Required by CommonBackend._handle_webidl_collection (which itself
        # has already written file-lists.json and the unified .cpp shards
        # before reaching us). Stash the parameters; emit the ninja rule
        # at write time.
        self._webidl = (
            mozpath.normsep(bindings_dir),
            sorted(unified_source_mapping),
            webidls,
            sorted(mozpath.normsep(f) for f in expected_build_output_files),
            sorted(global_define_files),
        )

    def _handle_idl_manager(self, manager):
        # Required by CommonBackend.consume_finished. Stash the XPIDLManager
        # so `_emit_xpidl_statements` can iterate `manager.modules`.
        self._xpidl_manager = manager

    def consume_finished(self):
        CommonBackend.consume_finished(self)

        # Resolve install srcs that point at a linkable's default objdir
        # location to the linkable's actual `output_path`. An ObjDirPath
        # like `!TestArguments.exe` from FINAL_TARGET_FILES resolves to
        # the moz.build's objdir (xpcom/tests/TestArguments.exe), but a
        # SimpleProgram with DIST_INSTALL=True is built directly at
        # dist/bin/TestArguments.exe — so the install_batch source must
        # point there. Mozmake handles this at install-action time via
        # OPTIONAL_EXISTS in the manifest; we resolve at graph-generation
        # time so ninja's input check sees a real producer.
        output_redirect = {}
        linkables = (
            self._programs
            + self._host_programs
            + self._shared_libs
            + self._host_libraries
        )
        for lk in linkables:
            default = mozpath.normsep(mozpath.join(lk.objdir, lk.name))
            actual = mozpath.normsep(lk.output_path.full_path)
            if default != actual:
                output_redirect[default] = actual
        if output_redirect:
            self._installs = [
                (output_redirect.get(src, src), dst) for src, dst in self._installs
            ]

        # Write chrome.manifest fragments collected from ChromeManifestEntry
        # (XPCOM_MANIFESTS in moz.build). Mozmake builds these incrementally
        # via `buildlist` actions in the misc tier; we write the full set
        # at backend-generation time. `addEntriesToListFile` is idempotent,
        # so jar_maker's later additions to the same file merge cleanly.
        from mozbuild.action.buildlist import addEntriesToListFile

        for path, entries in self._chrome_manifest_entries.items():
            addEntriesToListFile(path, sorted(entries))

        # TestManifest staging: build a single `mozpack.manifests.InstallManifest`
        # covering every TestManifest flavor + test-support-file
        # `OPTIONAL_EXISTS` markers, mirroring recursivemake's
        # `_process_test_manifest` + `_process_test_support_file` (which
        # populate `_test_files` and `_tests` respectively). We write to a
        # ninja-private path so we don't race with RecursiveMake co-run;
        # `process_install_manifest` is invoked via the existing
        # `run_install_manifest` rule. Per-flavor master
        # `<install_prefix>/<flavor>.toml` files are written here too.
        from mozpack.manifests import InstallManifest

        ninja_test_manifest = InstallManifest()
        master_manifests = {}
        for tm in self._test_manifests:
            install_prefix = mozpath.normsep(tm.install_prefix)
            for source, (dest, is_test) in tm.installs.items():
                try:
                    ninja_test_manifest.add_link(source, dest)
                except ValueError:
                    if not tm.dupe_manifest and is_test:
                        raise
            for base, pattern, dest in tm.pattern_installs:
                try:
                    ninja_test_manifest.add_pattern_link(base, pattern, dest)
                except ValueError:
                    if not tm.dupe_manifest:
                        raise
            for dest in tm.external_installs:
                try:
                    ninja_test_manifest.add_optional_exists(dest)
                except ValueError:
                    if not tm.dupe_manifest:
                        raise
            for src in tm.source_relpaths:
                self.backend_input_files.add(
                    mozpath.normsep(mozpath.join(self._topsrcdir, src))
                )
            # Reftest flavors emit empty `installs` but still contribute
            # to the master manifest list.
            master_manifests.setdefault((tm.flavor, install_prefix), set()).add(
                tm.manifest_relpath
            )

        # `_process_test_support_file` analogue: any linkable whose
        # `install_target` lives under `_tests/` gets an OPTIONAL_EXISTS
        # marker so the test packager can find it.
        for lk in (
            self._programs
            + self._host_programs
            + self._shared_libs
            + self._static_libs
            + self._host_libraries
        ):
            install_target = getattr(lk, "install_target", "")
            if not install_target.startswith("_tests"):
                continue
            basename = getattr(lk, "lib_name", None) or getattr(lk, "program", None)
            if not basename:
                continue
            try:
                ninja_test_manifest.add_optional_exists(
                    mozpath.join(install_target[len("_tests") + 1 :], basename)
                )
            except ValueError:
                pass

        # TEST_HARNESS_FILES: the destinations are all rooted at `_tests/`;
        # strip that prefix to get a manifest-relative path (the manifest
        # is installed with `_tests/` as its install_dir).
        for entry in self._test_harness_entries:
            kind = entry[0]
            try:
                if kind == "link":
                    _, src, dest = entry
                    ninja_test_manifest.add_link(src, _strip_tests(dest))
                elif kind == "pattern":
                    _, base, pattern, dest_dir = entry
                    ninja_test_manifest.add_pattern_link(
                        base, pattern, _strip_tests(dest_dir)
                    )
                elif kind == "optional":
                    _, dest = entry
                    ninja_test_manifest.add_optional_exists(_strip_tests(dest))
            except ValueError:
                pass

        # Persist the manifest. `_emit_install_statements` reuses
        # `run_install_manifest` to install from it into `_tests/`.
        # Master `<install_prefix>/<flavor>.toml`: one `["include:<rel>"]`
        # line per per-directory manifest. Mirrors recursivemake's
        # `_write_master_test_manifest`. Mozmake also drops an
        # OPTIONAL_EXISTS marker into its install manifest for each
        # master so the test packager finds them; do the same.
        for (flavor, install_prefix), manifests in master_manifests.items():
            master_rel = mozpath.join(install_prefix, f"{flavor}.toml")
            master_path = mozpath.join(self._topobjdir, "_tests", master_rel)
            with self._write_file(master_path) as master:
                master.write(
                    "# THIS FILE WAS AUTOMATICALLY GENERATED. "
                    "DO NOT MODIFY BY HAND.\n\n"
                )
                for m in sorted(manifests):
                    master.write(f'["include:{m}"]\n')
            try:
                ninja_test_manifest.add_optional_exists(master_rel)
            except ValueError:
                pass

        ninja_test_manifest_path = mozpath.join(
            self._topobjdir, "_build_manifests/install/_ninja_test_files"
        )
        if len(ninja_test_manifest):
            ensureParentDir(ninja_test_manifest_path)
            ninja_test_manifest.write(path=ninja_test_manifest_path)
            self._ninja_test_manifest_path = ninja_test_manifest_path
        else:
            self._ninja_test_manifest_path = None

        ninja_path = mozpath.join(self._topobjdir, "build.ninja")
        with self._write_file(ninja_path) as fh:
            self._write_ninja(fh)

    def build(self, config, output, jobs, verbose, what=None):
        """Invoke ninja for `mach build`. Targets default to all."""
        import subprocess

        cmd = ["ninja", "-C", config.topobjdir, "--jobserver-pool"]
        if jobs:
            cmd += ["-j", str(jobs)]
        if verbose:
            cmd.append("-v")
        if what:
            cmd += list(what)
        # Mirror mozmake's `export INCLUDE` / `export LIB` (config/config.mk):
        # cl/ml64/link rely on these env vars to find SDK headers and libs.
        env = os.environ.copy()
        for var in ("INCLUDE", "LIB"):
            val = config.substs.get(var)
            if val:
                env[var] = val
        return subprocess.call(cmd, env=env)

    # ---------------------------------------------------------------------
    # Data helpers
    # ---------------------------------------------------------------------

    def _computed_flag_list(self, relobjdir, var):
        """Return the list of flags for a given flag variable in a
        directory, merging across all ComputedFlags objects the emitter
        produced for that context (one from COMPILE_FLAGS, one from
        LINK_FLAGS, etc)."""
        out = []
        for cf in self._computed_flags.get(relobjdir, ()):
            out.extend(dict(cf.get_flags()).get(var, []))
        return out

    def _per_source_flags_for(self, relobjdir, source_full_path):
        """Return the per-source flags for a given source full path.

        PerSourceFlag.file_name is the source's full_path (set by the
        emitter at `all_flags[full_path] = context_flags`)."""
        target = mozpath.normsep(source_full_path)
        out = []
        for psf in self._per_source_flags.get(relobjdir, []):
            fn = getattr(psf, "file_name", None)
            if fn and mozpath.normsep(fn) == target:
                out.extend(getattr(psf, "flags", []))
        return out

    def _obj_path(self, source_path, linkable):
        """Compute the object file path for a source, matching the emitter's
        Linkable._get_objs logic."""
        obj_prefix = "host_" if getattr(linkable, "KIND", None) == "host" else ""
        obj_suffix = linkable.config.substs.get("OBJ_SUFFIX", "obj")
        basename = mozpath.splitext(mozpath.basename(source_path))[0]
        return mozpath.join(linkable.objdir, f"{obj_prefix}{basename}.{obj_suffix}")

    def _expand_num_outputs_outputs(self, primary, declared_outputs, flags):
        """If `flags` contains `--num-outputs N`, replace the primary
        output's `<base>.<ext>` form with the N split forms
        `<base>_0.<ext>` ... `<base>_{N-1}.<ext>` that wasm2c produces.

        Used by `_emit_generated_file_statements`: GeneratedFile only
        declares the primary, but wasm2c with `--num-outputs N` actually
        writes split files instead of the primary. Ninja needs the
        actual output set declared on the edge for downstream SOURCES
        references to resolve. Recursive-make doesn't need this fixup
        because make trusts the recipe to produce what consumers ask
        for.
        """
        flags_list = list(flags)
        for i, f in enumerate(flags_list):
            if str(f) == "--num-outputs" and i + 1 < len(flags_list):
                try:
                    n = int(flags_list[i + 1])
                except (ValueError, TypeError):
                    return declared_outputs
                if n <= 1:
                    return declared_outputs
                base, ext = mozpath.splitext(primary)
                split = [f"{base}_{i}{ext}" for i in range(n)]
                # Drop the primary from declared_outputs and prepend the
                # split set; preserve any other declared outputs (e.g. a
                # paired .h that wasm2c also writes).
                tail = [o for o in declared_outputs if o != primary]
                return split + tail
        return declared_outputs

    def _expand_make_flag_refs(self, relobjdir, flag):
        """Expand make-style `$(DEFINES)` and `$(LOCAL_INCLUDES)`
        references in a GeneratedFile flag string.

        Returns a single-element list with the joined value, matching
        the make recipe's `'$(DEFINES)' '$(LOCAL_INCLUDES)'` shell
        quoting. The recipe passes each as one argv entry; consumer
        scripts (e.g. preprocess_libffi_asm.py) then `shlex.split` the
        string into individual flags. A flag with no `$(...)` ref
        passes through as a single-element list.
        """
        if flag == "$(DEFINES)":
            parts = []
            for d in self._defines_by_dir.get(relobjdir, ()):
                parts.extend(d.get_defines())
            return [" ".join(parts)]
        if flag == "$(LOCAL_INCLUDES)":
            parts = [
                f"-I{self._rel_plain(li.path.full_path)}"
                for li in self._local_includes_by_dir.get(relobjdir, ())
            ]
            return [" ".join(parts)]
        return [flag]

    def _resolve_src(self, source_path, linkable):
        """Resolve a source path into an absolute path.

        Linkable.sources for UnifiedSources stores just the unified-file
        basename (e.g. `Unified_cpp_js_src_vm0.cpp`). Those files are
        written by CommonBackend at `linkable.objdir/<basename>`. Static
        sources are already absolute paths from the emitter."""
        if os.path.isabs(source_path) or source_path.startswith("/"):
            return mozpath.normsep(source_path)
        return mozpath.join(linkable.objdir, source_path)

    def _rel(self, path):
        """Convert an absolute path to ninja-form (unescaped): bare relative
        under topobjdir, `$topsrcdir/<rel>` under topsrcdir, otherwise
        the input absolute path. The unescaped form is returned because
        the `$topsrcdir` reference (if produced) must reach ninja
        unescaped so it expands."""
        norm = mozpath.normsep(str(path))
        objdir = self._topobjdir
        srcdir = self._topsrcdir
        if norm == objdir:
            return "."
        if norm.startswith(objdir + "/"):
            return mozpath.relpath(norm, objdir)
        if norm == srcdir:
            return "$topsrcdir"
        if norm.startswith(srcdir + "/"):
            return "$topsrcdir/" + mozpath.relpath(norm, srcdir)
        return norm

    def _n_rel(self, path):
        """ninja-syntax version of _rel: relative parts go through n_path
        for `:` / ` ` / `$` escaping; the `$topsrcdir` prefix (if
        produced) is left intact so ninja expands it."""
        rel = self._rel(path)
        if rel == "$topsrcdir":
            return "$topsrcdir"
        if rel.startswith("$topsrcdir/"):
            return "$topsrcdir/" + n_path(rel[len("$topsrcdir/") :])
        return n_path(rel)

    def _rel_plain(self, path):
        """Plain relative-from-topobjdir form of `path` (no
        `$topsrcdir` reference). Use when the result is destined for a
        `response_arg`-encoded slot — `response_arg` escapes `$` to
        `$$`, which would convert ninja's `$topsrcdir` reference into
        a literal `$topsrcdir` in the recipe argv."""
        return mozpath.relpath(mozpath.normsep(str(path)), self._topobjdir)

    # ---------------------------------------------------------------------
    # build.ninja emission
    # ---------------------------------------------------------------------

    def _write_ninja(self, fh):
        env = self.environment
        substs = env.substs
        writer = NinjaWriter(fh)

        writer.comment("Auto-generated by NinjaBackend. Do not edit.")
        writer.variable("ninja_required_version", "1.13")
        writer.newline()

        writer.variable("topobjdir", ".")
        writer.variable(
            "topsrcdir",
            n_path(mozpath.relpath(self._topsrcdir, self._topobjdir)),
        )

        # Tools. substs["CC"]/["CXX"] include base flags embedded in the
        # string (e.g. "-fms-compatibility-version=19.50 -std:c++20"); we
        # emit them verbatim as the command prefix.
        def _subst_cmd(key):
            v = substs.get(key, "")
            if isinstance(v, list):
                v = " ".join(v)
            return v

        cc = _subst_cmd("CC")
        cxx = _subst_cmd("CXX")
        host_cc = _subst_cmd("HOST_CC") or cc
        host_cxx = _subst_cmd("HOST_CXX") or cxx
        wasm_cc = _subst_cmd("WASM_CC") or cc
        wasm_cxx = _subst_cmd("WASM_CXX") or cxx
        ar = _subst_cmd("AR")
        linker = _subst_cmd("LINKER")
        host_linker = _subst_cmd("HOST_LINKER") or linker
        make = _subst_cmd("GMAKE") or "mozmake"
        python = _subst_cmd("PYTHON3") or "python"
        # On Windows, `mach` is a shell script that CreateProcess cannot
        # execute directly; use the `mach.cmd` batch wrapper.
        if os.name == "nt":
            mach = mozpath.join(self._topsrcdir, "mach.cmd")
        else:
            mach = mozpath.join(self._topsrcdir, "mach")

        # Host-OS defines and the NSPR include path are global per-build:
        # `config/rules.mk` adds them to every host-compile invocation
        # (`$(HOST_CC) $(HOST_CPPFLAGS) $(HOST_CFLAGS) $(NSPR_CFLAGS) ...`).
        host_cppflags = " ".join(
            response_arg(f) for f in self.environment.substs.get("HOST_CPPFLAGS", [])
        )
        nspr_cflags = " ".join(
            response_arg(f) for f in self.environment.substs.get("NSPR_CFLAGS", [])
        )

        for key, val in (
            ("CC", cc),
            ("CXX", cxx),
            ("HOST_CC", host_cc),
            ("HOST_CXX", host_cxx),
            ("WASM_CC", wasm_cc),
            ("WASM_CXX", wasm_cxx),
            ("AR", ar),
            ("LINKER", linker),
            ("HOST_LINKER", host_linker),
            ("MAKE", make),
            ("PYTHON", python),
            ("HOST_CPPFLAGS", host_cppflags),
            ("NSPR_CFLAGS", nspr_cflags),
        ):
            writer.variable(key, n_value(val))
        # MACH bypasses n_value escaping so the `$topsrcdir` reference
        # produced by _n_rel survives for ninja to expand.
        writer.variable("MACH", self._n_rel(mach))
        writer.newline()

        # Compile rules. The compiler reads its own response file via
        # `@$out.rsp` using MSVC argument-parsing rules (clang-cl matches
        # cl.exe here). response_arg below produces those rules, so the
        # quoted flags pass straight through with no shell involvement.
        # clang-cl depfile via -Xclang -dependency-file; we feed those
        # into ninja via deps = gcc + depfile = $out.d so ninja manages
        # implicit deps natively.
        clang_depfile_args = (
            "-Xclang -MP -Xclang -dependency-file -Xclang $out.d "
            "-Xclang -MT -Xclang $out"
        )
        writer.rule(
            "cxx",
            command="$CXX @$out.rsp",
            description="CXX $out",
            rspfile="$out.rsp",
            rspfile_content=f"$cxxflags {clang_depfile_args} -o $out -c $in",
            deps="gcc",
            depfile="$out.d",
        )
        writer.newline()
        writer.rule(
            "cc",
            command="$CC @$out.rsp",
            description="CC $out",
            rspfile="$out.rsp",
            rspfile_content=f"$cflags {clang_depfile_args} -o $out -c $in",
            deps="gcc",
            depfile="$out.d",
        )
        writer.newline()
        writer.rule(
            "host_cxx",
            command="$HOST_CXX @$out.rsp",
            description="HOST_CXX $out",
            rspfile="$out.rsp",
            rspfile_content=(
                "$HOST_CPPFLAGS $host_cxxflags $NSPR_CFLAGS -o $out -c $in"
            ),
        )
        writer.newline()
        writer.rule(
            "host_cc",
            command="$HOST_CC @$out.rsp",
            description="HOST_CC $out",
            rspfile="$out.rsp",
            rspfile_content=("$HOST_CPPFLAGS $host_cflags $NSPR_CFLAGS -o $out -c $in"),
        )
        writer.newline()
        # Assembly via clang-cl's integrated assembler (USE_INTEGRATED_CLANGCL_AS).
        writer.rule(
            "asm",
            command="$CC @$out.rsp",
            description="AS $out",
            rspfile="$out.rsp",
            rspfile_content="$asflags -o $out -c $in",
        )
        writer.newline()
        # Native assembler rule for `.asm` files (Intel-syntax). Per
        # `config/rules.mk`, the recipe is:
        #   $(AS) $(ASOUTOPTION)$@ $(ASFLAGS) $(per_source) $(AS_DASH_C_FLAG) $<
        # Each per-edge build supplies $as / $as_dash_c_flag /
        # $asoutoption from the directory's `VariablePassthru` (the
        # emitter sets these from `USE_NASM` or
        # `USE_INTEGRATED_CLANGCL_AS`), or from substs when neither is
        # set (e.g. libffi on clang-cl uses ml64 from substs.AS), plus
        # $asflags from `ComputedFlags["ASFLAGS"]`. No rspfile: nasm's
        # `-@` parser is buggy in 3.x and assembler invocations are
        # short enough to fit on a command line directly (matching
        # mozmake's recipe).
        writer.rule(
            "asm_native",
            command="$as $asoutoption$out $asflags $as_dash_c_flag $in",
            description="AS $out",
        )
        writer.newline()

        # Archive / link rules: the rspfile holds the linker input list so
        # it can be arbitrarily long (js_static.lib archives ~700 objs).
        writer.rule(
            "archive",
            command="$AR -nologo -out:$out @$out.rsp",
            description="AR $out",
            rspfile="$out.rsp",
            rspfile_content="$in",
        )
        writer.newline()
        writer.rule(
            "link_shared",
            command="$LINKER -NOLOGO -DLL -OUT:$out $ldflags -IMPLIB:$implib @$out.rsp",
            description="LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()
        writer.rule(
            "link_exe",
            command="$LINKER -NOLOGO -OUT:$out $ldflags @$out.rsp",
            description="LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()

        writer.rule(
            "host_link_exe",
            command="$HOST_LINKER -NOLOGO -OUT:$out $ldflags @$out.rsp",
            description="HOST_LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()

        # Wasm compile / link. WASM_CC and WASM_CXX are clang targeting
        # wasm32-wasi; flag handling matches the regular cxx/cc rules
        # (rspfile + clang depfile via -Xclang -dependency-file). The
        # link rule invokes WASM_CXX with the wasm-specific linker flags
        # (--export-all, --stack-first, etc.) the recursive-make rules.mk
        # bakes into the link command.
        writer.rule(
            "wasm_cxx",
            command="$WASM_CXX @$out.rsp",
            description="WASM_CXX $out",
            rspfile="$out.rsp",
            rspfile_content=f"$wasm_cxxflags {clang_depfile_args} -o $out -c $in",
            deps="gcc",
            depfile="$out.d",
        )
        writer.newline()
        writer.rule(
            "wasm_cc",
            command="$WASM_CC @$out.rsp",
            description="WASM_CC $out",
            rspfile="$out.rsp",
            rspfile_content=f"$wasm_cflags {clang_depfile_args} -o $out -c $in",
            deps="gcc",
            depfile="$out.d",
        )
        writer.newline()
        writer.rule(
            "wasm_link",
            command="$WASM_CXX -o $out $wasm_ldflags @$out.rsp",
            description="WASM_LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()

        # process_install_manifest: delegates to mozmake's install_manifest
        # driver so ninja gets identical pattern/wildcard handling as the
        # recursive-make backend. One invocation covers a whole install
        # target (e.g. dist/include ~600 entries) in one Python process.
        writer.rule(
            "run_install_manifest",
            command="$PYTHON -m mozbuild.action.process_install_manifest --track $track $install_dir $in",
            description="INSTALL $install_dir",
            restat=True,
        )
        writer.newline()
        # Single-file install via hardlink (fallback to copy). Kept for
        # any edge that doesn't batch well.
        writer.rule(
            "install_file",
            command="$PYTHON -m mozbuild.action.ninja_install $in $out",
            description="INSTALL $out",
            restat=True,
        )
        writer.newline()
        # Batch install (tab-separated src->dst manifest). Used for
        # generated-file EXPORTS installs to amortize the Python startup
        # cost across many files.
        writer.rule(
            "install_batch",
            command="$PYTHON -m mozbuild.action.ninja_install $manifest",
            description="INSTALL (batch)",
            restat=True,
        )
        writer.newline()
        # OBJDIR_PP_FILES preprocess-and-install (not covered by any
        # install manifest — mozmake has a dedicated rule for it).
        writer.rule(
            "pp_install",
            command="$PYTHON -m mozbuild.action.preprocessor $defines -o $out $in",
            description="PP $out",
            restat=True,
        )
        writer.newline()

        # Python generator (for GeneratedFile). `pygen_runner` invokes
        # mozbuild.action.file_generate (same semantics as mozmake's
        # py_action(file_generate, ...)) and then post-processes the
        # resulting depfile in-process. The post-process is required
        # because file_generate writes Makefile-style depfiles that wrap
        # conditional inputs in `$(wildcard X)`. Ninja's gcc-depfile
        # parser cannot read that and would mark every pygen output
        # dirty on every build. `filter_depfile` unwraps the wildcards
        # and drops missing-file deps so ninja's up-to-date check works.
        writer.rule(
            "pygen",
            command=(
                "$PYTHON -m mozbuild.action.pygen_runner $depfile "
                "$locale$script $method $primary $depfile $primary $extra"
            ),
            description="GEN $primary",
            deps="gcc",
            depfile="$depfile",
            restat=True,
        )
        writer.newline()

        # IPDL codegen: a single ipdl.py invocation produces .cpp/.h files
        # for every protocol in the tree. The recursive-make backend models
        # this the same way (one ipdl.track target). Outputs are declared
        # explicitly for the .cpp files (UnifiedSources reference them);
        # .h files emerge as side effects whose paths depend on the
        # protocol's namespace and are tracked downstream via depfiles
        # from the consuming compile.
        writer.rule(
            "ipdl",
            command=(
                "$PYTHON $script "
                "--sync-msg-list=$sync_msg_list "
                "--msg-metadata=$msg_metadata "
                "--outheaders-dir=$headers_dir "
                "--outcpp-dir=$cpp_dir "
                "$include_args "
                "--file-list=$file_list"
            ),
            description="IPDL codegen",
            restat=True,
        )
        writer.newline()

        # WebIDL codegen: `mozbuild.action.webidl` reads file-lists.json
        # (written at backend-write time by CommonBackend._handle_webidl_collection)
        # and runs WebIDLCodegenManager.generate_build_files(), which writes
        # all bindings and a Make-format depfile (codegen.pp). Ninja consumes
        # the depfile via deps=gcc; restat=1 catches writeifmodified-style
        # no-ops on rerun.
        writer.rule(
            "webidl",
            command="$PYTHON -m mozbuild.action.webidl",
            description="WebIDL codegen",
            deps="gcc",
            depfile="$depfile",
            restat=True,
        )
        writer.newline()

        # XPIDL per-module: `xpidl-process.py` reads the module's .idl files
        # and writes one .h + two .rs files per stem, plus `<module>.xpt`
        # and `<module>.d.json` for the module. The action also writes a
        # gcc-style depfile (`<deps_dir>/<module>.pp`) tracking included
        # IDLs and the Python modules of the parser.
        writer.rule(
            "xpidl_batch",
            command=(
                "$PYTHON -m mozbuild.action.xpidl_batch "
                "--depsdir $deps_dir "
                "--bindings-conf $bindings_conf "
                "--header-dir $header_dir "
                "--xpcrs-dir $xpcrs_dir "
                "--xpt-dir $xpt_dir "
                "--combined-depfile $depfile "
                "--combined-target $primary "
                "$include_args "
                "--manifest $manifest"
            ),
            description="XPIDL (batch)",
            deps="gcc",
            depfile="$depfile",
            restat=True,
        )
        writer.newline()

        # XPIDL aggregate link: `xptcodegen.py` consumes every per-module
        # `<module>.xpt` and produces the singleton `xptdata.cpp` + the
        # `xptdata.h` that lands in dist/include.
        writer.rule(
            "xpidl_link",
            command="$PYTHON $script $outfile $outheader $xpts",
            description="XPIDL link",
            restat=True,
        )
        writer.newline()

        # jar.mn packaging: `jar_runner` runs jar_maker and touches the
        # stamp file in one Python process (ninja's `CreateProcess`
        # invocation has no shell, so `&& touch` would not work).
        # jar_maker preprocesses jar.mn entries and writes them to
        # `$final_target` in the configured format (typically `flat`,
        # files copied to dist/bin). The stamp is the declared output;
        # actual chrome files are tracked indirectly. restat=1 so
        # re-runs that produce identical output don't dirty downstream.
        writer.rule(
            "jar_maker",
            command=(
                "$PYTHON -m mozbuild.action.jar_runner $stamp "
                "-d $final_target -t $topsrcdir -f $jar_format "
                "$jar_args $defines $jar_manifest"
            ),
            description="JAR $jar_manifest",
            restat=True,
        )
        writer.newline()

        # Cargo: delegate to mozmake which handles CARGO_TARGET_DIR etc.
        # RecursiveMake's per-rust-library target is
        # `<relobjdir>/target-objects`, invoked from the topobjdir
        # Makefile so the config/makefiles/rust.mk machinery
        # (CARGO_TARGET_DIR, RUSTFLAGS, etc) is in scope. No shell wrap;
        # mozmake's argv is plain with no embedded quoting.
        writer.rule(
            "cargo_build",
            command="$MAKE -C $topobjdir $cargo_target",
            description="CARGO $out",
            depfile="$depfile",
            deps="gcc",
            restat=True,
        )
        writer.newline()


        # Regenerator: re-runs the backend so build.ninja reflects the
        # current state of moz.build / config.status when any tracked
        # input changes. `generator = 1` tells ninja to invoke this rule
        # specially: before any other work, with no complaint about
        # being older than its outputs, and ninja re-reads build.ninja
        # afterward. The set of inputs comes from
        # `BuildBackend.backend_input_files`, which already includes
        # every moz.build the emitter visited plus every Python module
        # under topsrcdir/topobjdir that could affect emitter output.
        writer.rule(
            "regenerator",
            command="$MACH build-backend",
            description="Regenerating build.ninja",
            generator=True,
        )
        writer.newline()

        # Install manifests first — compile rules read self._install_tracks
        # to know which install targets must land before compile can start.
        self._emit_install_statements(writer)

        # GeneratedFile rules (CONFIGURE_DEFINE_FILES, opcode tables, etc).
        self._emit_generated_file_statements(writer)

        # IPDL codegen (one rule, many declared outputs).
        self._emit_ipdl_statements(writer)

        # WebIDL codegen (one rule, many declared outputs).
        self._emit_webidl_statements(writer)

        # XPIDL codegen (one rule per module, one aggregate link rule).
        self._emit_xpidl_statements(writer)

        # jar.mn packaging (one rule per JARManifest).
        self._emit_jar_statements(writer)

        # Emit compile build statements.
        self._emit_compile_statements(writer)

        # Rust libraries are built by delegating to mozmake, which already
        # knows how to invoke cargo with the right env.
        self._emit_rust_statements(writer)

        # Emit link build statements for static libs, shared libs, and programs.
        self._emit_archive_statements(writer)
        self._emit_shared_link_statements(writer)
        self._emit_program_statements(writer)
        self._emit_host_archive_statements(writer)
        self._emit_host_program_statements(writer)

        # Wasm: compile WASM_SOURCES, then link them into the
        # SANDBOXED_WASM_LIBRARY .wasm output that downstream wasm2c
        # GeneratedFile rules consume.
        self._emit_wasm_compile_statements(writer)
        self._emit_wasm_link_statements(writer)

        # build.ninja regen statement. Inputs include every moz.build
        # the emitter consumed plus the Python modules under
        # topsrcdir/topobjdir, both populated in
        # `BuildBackend.backend_input_files`. config.status is added
        # explicitly because configure changes (e.g. a different
        # --enable-build-backend) must invalidate the existing graph.
        regen_inputs = sorted(mozpath.normsep(f) for f in self.backend_input_files)
        regen_inputs.append(mozpath.join(self._topobjdir, "config.status"))
        writer.newline()
        writer.build(
            "build.ninja",
            "regenerator",
            inputs=[self._n_rel(f) for f in regen_inputs],
        )

        # Named phony targets matching mach's vocabulary, plus an `all`
        # umbrella that becomes the default. `ninja binaries` skips the
        # install staging; `ninja install` runs only the install tracks;
        # `ninja` (or `ninja all`) does both.
        #
        # Linkables tagged with `output_category` (e.g. gtest/xul.dll
        # with category "gtest") are non-default in recursive-make
        # (recursivemake.py:824-832 strips them from `_compile_graph`).
        # Mirror that here: keep them OUT of the `binaries` phony, and
        # emit a per-category phony (`gtest`, etc.) so they remain
        # buildable on demand via `ninja <category>`.
        binary_outputs = []
        category_outputs = defaultdict(list)
        for p in self._programs + self._host_programs + self._shared_libs:
            out_path = self._n_rel(p.output_path.full_path)
            cat = getattr(p, "output_category", None)
            if cat:
                category_outputs[cat].append(out_path)
            else:
                binary_outputs.append(out_path)

        install_outputs = [
            self._n_rel(track)
            for track in getattr(self, "_install_tracks", {}).values()
        ]
        install_outputs += [
            self._n_rel(o) for o in getattr(self, "_install_batch_post_outputs", ())
        ]
        install_outputs += [
            self._n_rel(o) for o in getattr(self, "_pp_install_outputs", ())
        ]
        install_outputs += [
            self._n_rel(s) for s in getattr(self, "_jar_maker_stamps", ())
        ]

        all_groups = []
        if binary_outputs:
            writer.newline()
            writer.build("binaries", "phony", inputs=binary_outputs)
            all_groups.append("binaries")
        if install_outputs:
            writer.newline()
            writer.build("install", "phony", inputs=install_outputs)
            all_groups.append("install")
        # Per-category phonies for non-default targets (gtest, etc.).
        # Not added to `all` — only built when explicitly requested.
        for cat, outs in sorted(category_outputs.items()):
            writer.newline()
            writer.build(cat, "phony", inputs=outs)
        if all_groups:
            writer.newline()
            writer.build("all", "phony", inputs=all_groups)
            writer.default(["all"])

    def _emit_compile_statements(self, writer):
        """Emit build statements for every compiled source file.

        Sources come from two places:
          * Sources objects (one .cpp -> one .obj, non-unified)
          * UnifiedSources that already wrote Unified_cpp_*.cpp files to
            disk at configure time. Those are compiled as regular .cpp.

        We associate sources with a specific Linkable using the Linkable's
        own `sources` dict (populated by the emitter), so the set of files
        we emit exactly matches what the make backend would compile.
        """
        writer.newline()
        writer.comment("------ compile rules ------")
        writer.newline()

        # Aggregate pre-compile generated-file outputs under a phony target
        # so every compile statement can depend on it as an order-only
        # prerequisite. Include only files the emitter marked as required
        # before or during compile (e.g. js-confdefs.h, selfhosted.out.h);
        # post-link generators (like spidermonkey_checks) would create a
        # dep cycle through the static library if aggregated here.
        #
        # Two phonies are emitted:
        #   * `.ninja-generated`     — every generated file; target
        #     compiles depend on this.
        #   * `.ninja-generated-host` — the subset whose producers don't
        #     transitively need a host program. Host compiles depend on
        #     this. Splitting avoids the host_compile → host_link →
        #     wasm2c output → .ninja-generated → host_compile cycle while
        #     still letting host compiles wait on plain generated headers
        #     (e.g. `wabt/config.h`).
        all_generated = []
        host_safe_generated = []
        host_program_outputs = {
            mozpath.normsep(p.output_path.full_path) for p in self._host_programs
        }

        def _depends_on_host_program(g):
            for inp in g.inputs:
                if mozpath.normsep(inp.full_path) in host_program_outputs:
                    return True
            return False

        for g in self._generated_files:
            if not g.script:
                continue
            if not (g.required_before_compile or g.required_during_compile):
                continue
            declared = []
            for o in g.outputs:
                if isinstance(o, str):
                    if o.startswith("/"):
                        declared.append(mozpath.join(self._topobjdir, o[1:]))
                    else:
                        declared.append(mozpath.join(g.objdir, o))
                else:
                    declared.append(mozpath.normsep(o.full_path))
            if not declared:
                continue
            # Apply the same `--num-outputs` expansion that
            # `_emit_generated_file_statements` does, so the actual
            # produced files (e.g. wasm2c's `<base>_0.c` ... `<base>_{N-1}.c`)
            # are what consumers wait on, not the unwritten primary.
            outs = self._expand_num_outputs_outputs(
                declared[0], declared, g.flags or ()
            )
            all_generated.extend(outs)
            if not _depends_on_host_program(g):
                host_safe_generated.extend(outs)

        # dist/include must be fully populated before compiles can resolve
        # `-I dist/include` header references. Three sources of files end
        # up under dist/include:
        #   1. Source-tree headers via the install manifest (track file
        #      emitted as output of the install_manifest edge).
        #   2. Generated files (ObjDirPath EXPORTS) — each has its own
        #      install_file edge; dst is under dist/include.
        #   3. Preprocessed OBJDIR_PP_FILES whose dest is under dist/include.
        # We fold all three into .ninja-generated so compiles wait on them.
        # The install-manifest track is also safe for host compiles (it
        # only stages source-tree EXPORTS, no host-program outputs).
        # ObjDirPath EXPORTS and OBJDIR_PP_FILES might transit through
        # host programs, so they stay in `.ninja-generated` only.
        track = getattr(self, "_install_tracks", {}).get("dist_include")
        if track:
            all_generated.append(track)
            host_safe_generated.append(track)
        dist_include_prefix = mozpath.join(self._topobjdir, "dist/include") + "/"
        for _, dst in self._installs:
            if dst.startswith(dist_include_prefix):
                all_generated.append(dst)
                host_safe_generated.append(dst)
        for _, dst, _ in self._pp_installs:
            if dst.startswith(dist_include_prefix):
                all_generated.append(dst)
                host_safe_generated.append(dst)
        # IPDL, WebIDL, and XPIDL codegen are pure-Python (no
        # host-program transit); safe for host compiles to wait on too.
        all_generated.extend(self._ipdl_outputs)
        host_safe_generated.extend(self._ipdl_outputs)
        all_generated.extend(self._webidl_outputs)
        host_safe_generated.extend(self._webidl_outputs)
        all_generated.extend(self._xpidl_outputs)
        host_safe_generated.extend(self._xpidl_outputs)
        writer.build(
            ".ninja-generated",
            "phony",
            inputs=[self._n_rel(o) for o in all_generated] if all_generated else None,
        )
        writer.build(
            ".ninja-generated-host",
            "phony",
            inputs=(
                [self._n_rel(o) for o in host_safe_generated]
                if host_safe_generated
                else None
            ),
        )
        writer.newline()

        # Flags are per-directory (ComputedFlags objects are emitted by
        # context), but a source's declaring directory may differ from the
        # Linkable's directory (e.g. js_static lives in js/src/build while
        # js/src/vm contributes its own compiled sources). We therefore
        # iterate every Sources/UnifiedSources/HostSources object — each
        # carries its own `relobjdir` and `objdir`, so we can resolve flags
        # and output paths from the source's own context.
        emitted_objs = set()

        def iter_all_sources():
            for bucket in self._sources_by_dir.values():
                for s in bucket:
                    yield s, False, False
            for bucket in self._unified_by_dir.values():
                for s in bucket:
                    yield s, True, False
            for bucket in self._host_sources_by_dir.values():
                for s in bucket:
                    yield s, False, True

        for sobj, is_unified, is_host in iter_all_sources():
            relobjdir = sobj.relobjdir
            objdir = sobj.objdir
            cxxflags = self._computed_flag_list(
                relobjdir,
                "HOST_CXXFLAGS" if is_host else "CXXFLAGS",
            )
            cflags = self._computed_flag_list(
                relobjdir,
                "HOST_CFLAGS" if is_host else "CFLAGS",
            )
            asflags = self._computed_flag_list(
                relobjdir, "SFLAGS"
            ) + self._computed_flag_list(relobjdir, "ASFLAGS")

            # For UnifiedSources, the compile inputs are the generated
            # Unified_cpp_*.cpp files (keys of unified_source_mapping),
            # living in objdir. For non-unified Sources/HostSources, inputs
            # are the static_files + generated_files from `files`.
            if is_unified and sobj.have_unified_mapping:
                inputs = [
                    mozpath.join(objdir, u) for u, _ in sobj.unified_source_mapping
                ]
            elif is_unified and not sobj.have_unified_mapping:
                inputs = list(sobj.files)
            else:
                inputs = list(sobj.files)

            for src in inputs:
                ext = mozpath.splitext(src)[1].lower()
                basename = mozpath.basename(src)
                basename_noext = mozpath.splitext(basename)[0]
                obj_prefix = "host_" if is_host else ""
                obj = mozpath.join(objdir, f"{obj_prefix}{basename_noext}.obj")
                if obj in emitted_objs:
                    continue
                emitted_objs.add(obj)
                src_norm = mozpath.normsep(src)
                extra = self._per_source_flags_for(relobjdir, src_norm)

                if ext in (".cpp", ".cc", ".cxx"):
                    rule_name = "host_cxx" if is_host else "cxx"
                    flag_var = "host_cxxflags" if is_host else "cxxflags"
                    flag_value = " ".join(response_arg(f) for f in cxxflags + extra)
                elif ext == ".c":
                    rule_name = "host_cc" if is_host else "cc"
                    flag_var = "host_cflags" if is_host else "cflags"
                    flag_value = " ".join(response_arg(f) for f in cflags + extra)
                elif ext in (".S", ".s"):
                    rule_name = "asm"
                    flag_var = "asflags"
                    flag_value = " ".join(response_arg(f) for f in asflags + extra)
                elif ext == ".asm":
                    # Native assembler path: `.asm` files use $(AS) +
                    # $(ASFLAGS), with the assembler binary set per-context
                    # by the emitter via VariablePassthru (USE_NASM picks
                    # nasm; USE_INTEGRATED_CLANGCL_AS picks $CC). Without
                    # either, fall back to the global $(AS) from substs
                    # (e.g. ml64 on Windows for libffi). Use ASFLAGS only
                    # (not SFLAGS — those are for `.S/.s`).
                    asflags_only = self._computed_flag_list(relobjdir, "ASFLAGS")
                    passthru = self._variable_passthru.get(relobjdir)
                    pv = passthru.variables if passthru else {}
                    substs = self.environment.substs
                    asm_program = pv.get("AS") or substs.get("AS", "")
                    as_dash_c_flag = pv.get(
                        "AS_DASH_C_FLAG", substs.get("AS_DASH_C_FLAG", "-c")
                    )
                    asoutoption = pv.get(
                        "ASOUTOPTION", substs.get("ASOUTOPTION", "-o ")
                    )
                    flag_value = " ".join(response_arg(f) for f in asflags_only + extra)
                    # Host compiles excluded by the conditional below; the
                    # `.asm` dispatch is target-side, never host.
                    order_only = ".ninja-generated"
                    writer.build(
                        self._n_rel(obj),
                        "asm_native",
                        inputs=self._n_rel(src_norm),
                        order_only=order_only,
                        variables={
                            "as": n_value(asm_program),
                            "asflags": flag_value,
                            "as_dash_c_flag": n_value(as_dash_c_flag),
                            "asoutoption": n_value(asoutoption),
                        },
                    )
                    continue
                else:
                    writer.comment(f"unknown source extension for {src}")
                    continue
                # Host compiles depend on `.ninja-generated-host` (the
                # subset of generated files whose producers don't
                # transitively need a host program), avoiding the cycle
                # host_obj → host_link → wasm2c output → .ninja-generated
                # → host_obj while still letting host compiles wait on
                # plain generated headers like `wabt/config.h`.
                order_only = ".ninja-generated-host" if is_host else ".ninja-generated"
                writer.build(
                    self._n_rel(obj),
                    rule_name,
                    inputs=self._n_rel(src_norm),
                    order_only=order_only,
                    variables={flag_var: flag_value},
                )

    def _emit_archive_statements(self, writer):
        """Emit archive rules for StaticLibrary (excluding rust libs which
        are built via cargo). Only emit a real archive for libraries with
        `no_expand_lib=True`; others are virtual groupings whose objs get
        pulled into their parents via CommonBackend._expand_libs."""
        writer.newline()
        writer.comment("------ static libraries ------")
        writer.newline()
        for lib in self._static_libs:
            if isinstance(lib, RustLibrary):
                continue
            if not getattr(lib, "no_expand_lib", False):
                continue
            out = self._lib_output_path(lib)
            objs, shared_libs, os_libs, static_libs = self._expand_libs(lib)
            all_archive_inputs = list(objs)
            # Static libs from expand that are themselves real archives
            # should still be inputs to this archive? For no_expand_lib
            # libraries, _expand_libs returns them in `static_libs` — we
            # include them so llvm-lib merges.
            for static_lib in static_libs:
                all_archive_inputs.append(self._lib_output_path(static_lib))
            if not all_archive_inputs:
                writer.comment(f"skip empty archive {lib.lib_name} ({lib.relobjdir})")
                continue
            writer.build(
                self._n_rel(out),
                "archive",
                inputs=[self._n_rel(o) for o in all_archive_inputs],
            )

    def _emit_shared_link_statements(self, writer):
        writer.newline()
        writer.comment("------ shared libraries ------")
        writer.newline()
        for lib in self._shared_libs:
            # For DIST_INSTALL'd shared libs, output_path puts the DLL at
            # its final dist/bin/ location so downstream js.exe finds it
            # without a separate install step.
            out = mozpath.normsep(lib.output_path.full_path)
            implib = mozpath.join(lib.objdir, getattr(lib, "import_name", lib.lib_name))
            objs, shared_libs, os_libs, static_libs = self._expand_libs(lib)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))

            # DEFFILE / SYMBOLS_FILE: DEFFILE puts `-DEF:<relpath>` (for
            # clang-cl) into LDFLAGS, while SYMBOLS_FILE drives the same
            # `-DEF:<relpath>` via SharedLibrary.symbols_link_arg (which the
            # recursive make backend folds into EXTRA_DSO_LDOPTS). We
            # cover both by scanning LDFLAGS and also appending the
            # symbols_link_arg if set. Resolve the path against `lib.objdir`
            # when it came in relative, then rewrite the ldflag using a
            # plain relpath: the ldflag is joined via `response_arg`,
            # which escapes `$` to `$$` and would defeat the
            # `$topsrcdir` reference `_rel` produces for topsrcdir-rooted
            # def files (e.g. `mozglue/build/mozglue.def`). The
            # implicit-dep list still uses `_n_rel` since that slot is
            # not response_arg-encoded.
            ldflags_raw = list(self._computed_flag_list(lib.relobjdir, "LDFLAGS"))
            symbols_link_arg = getattr(lib, "symbols_link_arg", None)
            if symbols_link_arg:
                ldflags_raw.append(symbols_link_arg)
            def_abs = None
            for i, f in enumerate(ldflags_raw):
                if f.startswith("-DEF:"):
                    rel = f[len("-DEF:") :]
                    if not os.path.isabs(rel):
                        def_abs = mozpath.normpath(mozpath.join(lib.objdir, rel))
                    else:
                        def_abs = rel
                    ldflags_raw[i] = "-DEF:" + mozpath.relpath(
                        def_abs, self._topobjdir
                    )
                    break

            implicit_deps = [def_abs] if def_abs else None

            # Only pass linker-native flags (LDFLAGS) when invoking lld-link
            # directly. CXX_LDFLAGS/C_LDFLAGS are compiler-driver flags the
            # make backend passes through `$(CXX) -o` at link time; calling
            # the linker ourselves means they're not applicable. The DEF
            # path rewrite happened in the ldflags_raw loop above.
            writer.build(
                self._n_rel(out),
                "link_shared",
                inputs=[self._n_rel(o) for o in link_inputs],
                implicit_outputs=(
                    self._n_rel(implib) if implib and implib != out else None
                ),
                implicit=(
                    [self._n_rel(d) for d in implicit_deps] if implicit_deps else None
                ),
                variables={
                    "implib": self._n_rel(implib),
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(response_arg(f) for f in ldflags_raw),
                },
            )

    def _emit_program_statements(self, writer):
        writer.newline()
        writer.comment("------ programs ------")
        writer.newline()
        for p in self._programs:
            out = p.output_path.full_path
            objs, shared_libs, os_libs, static_libs = self._expand_libs(p)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            ldflags = list(
                self.environment.substs.get("WIN32_EXE_DEFAULT_LDFLAGS") or []
            )
            passthru = self._variable_passthru.get(p.relobjdir)
            if passthru:
                ldflags.extend(passthru.variables.get("WIN32_EXE_LDFLAGS", []))
            ldflags.extend(self._computed_flag_list(p.relobjdir, "LDFLAGS"))
            writer.build(
                self._n_rel(out),
                "link_exe",
                inputs=[self._n_rel(o) for o in link_inputs],
                variables={
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(response_arg(f) for f in ldflags),
                },
            )

    def _emit_host_archive_statements(self, writer):
        """Emit archive rules for HostLibrary. Mirrors the StaticLibrary
        path: skip rust host libs (cargo handles them) and virtual host
        libs (objects flow into consumers via `_expand_libs`, no real
        archive on disk). Reuses the `archive` rule because the
        archiver tool is shared between host and target on supported
        Windows toolchains."""
        real_libs = [
            lib
            for lib in self._host_libraries
            if not hasattr(lib, "cargo_file") and getattr(lib, "no_expand_lib", False)
        ]
        if not real_libs:
            return
        writer.newline()
        writer.comment("------ host static libraries ------")
        writer.newline()
        for lib in real_libs:
            out = self._lib_output_path(lib)
            objs, shared_libs, os_libs, static_libs = self._expand_libs(lib)
            all_archive_inputs = list(objs)
            for static_lib in static_libs:
                all_archive_inputs.append(self._lib_output_path(static_lib))
            if not all_archive_inputs:
                writer.comment(
                    f"skip empty host archive {lib.lib_name} ({lib.relobjdir})"
                )
                continue
            writer.build(
                self._n_rel(out),
                "archive",
                inputs=[self._n_rel(o) for o in all_archive_inputs],
            )

    def _emit_host_program_statements(self, writer):
        if not self._host_programs:
            return
        writer.newline()
        writer.comment("------ host programs ------")
        writer.newline()
        for p in self._host_programs:
            out = p.output_path.full_path
            objs, shared_libs, os_libs, static_libs = self._expand_libs(p)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            ldflags = list(self._computed_flag_list(p.relobjdir, "HOST_LDFLAGS"))
            writer.build(
                self._n_rel(out),
                "host_link_exe",
                inputs=[self._n_rel(o) for o in link_inputs],
                variables={
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(response_arg(f) for f in ldflags),
                },
            )

    def _emit_wasm_compile_statements(self, writer):
        """Emit per-source compile rules for `WASM_SOURCES`.

        Each `WasmSources` object has `.c` or `.cpp` files (plus
        generated equivalents) that compile through `WASM_CC`/`WASM_CXX`
        — clang targeting `wasm32-wasi`. Output objects use
        `WASM_OBJ_SUFFIX` (typically `.wasm`) and live alongside the
        source's relobjdir.
        """
        if not self._wasm_sources_by_dir:
            return
        writer.newline()
        writer.comment("------ wasm compile rules ------")
        writer.newline()

        wasm_obj_suffix = self.environment.substs.get("WASM_OBJ_SUFFIX", "wasm")
        emitted_objs = set()
        for relobjdir, bucket in self._wasm_sources_by_dir.items():
            for sobj in bucket:
                wasm_cflags = self._computed_flag_list(relobjdir, "WASM_CFLAGS")
                wasm_cxxflags = self._computed_flag_list(relobjdir, "WASM_CXXFLAGS")
                for src in list(sobj.files):
                    src_norm = mozpath.normsep(src)
                    ext = mozpath.splitext(src_norm)[1].lower()
                    basename_noext = mozpath.splitext(mozpath.basename(src_norm))[0]
                    obj = mozpath.join(
                        sobj.objdir, f"{basename_noext}.{wasm_obj_suffix}"
                    )
                    if obj in emitted_objs:
                        continue
                    emitted_objs.add(obj)
                    extra = self._per_source_flags_for(relobjdir, src_norm)
                    if ext == ".c":
                        rule_name = "wasm_cc"
                        flag_var = "wasm_cflags"
                        flag_value = " ".join(
                            response_arg(f) for f in wasm_cflags + extra
                        )
                    elif ext in (".cpp", ".cc", ".cxx"):
                        rule_name = "wasm_cxx"
                        flag_var = "wasm_cxxflags"
                        flag_value = " ".join(
                            response_arg(f) for f in wasm_cxxflags + extra
                        )
                    else:
                        writer.comment(f"unknown wasm source extension for {src_norm}")
                        continue
                    # Wasm compiles must not depend on `.ninja-generated`:
                    # wasm objects link into `<name>.wasm`, which feeds the
                    # `<name>.wasm.c` GeneratedFile that's itself in
                    # `.ninja-generated`. An order_only edge here would
                    # close the cycle wasm_obj → wasm_link → wasm.c →
                    # .ninja-generated → wasm_obj. Wasm code is sandboxed
                    # and produces inputs to codegen, not consumers of it
                    # — same shape as the host-compile exclusion.
                    writer.build(
                        self._n_rel(obj),
                        rule_name,
                        inputs=self._n_rel(src_norm),
                        variables={flag_var: flag_value},
                    )

    def _emit_wasm_link_statements(self, writer):
        """Emit `wasm_link` rules for each `SandboxedWasmLibrary`.

        The output filename is the library's basename verbatim (e.g.
        `rlboxsoundtouch.wasm`) — the `SANDBOXED_WASM_LIBRARY_NAME`
        declaration includes the `.wasm` extension. Linker flags match
        `config/rules.mk`'s wasm-archive recipe: `--export-all`,
        `--stack-first`, the optimize-conditional stack size,
        `--no-entry`, `--import-memory`, `--import-table`.
        """
        if not self._wasm_libraries:
            return
        writer.newline()
        writer.comment("------ wasm libraries ------")
        writer.newline()

        # Stack size matches `config/rules.mk` line 497: 256 KB optimized,
        # 1 MB otherwise. Read MOZ_OPTIMIZE from substs to match the make
        # backend's choice for this build configuration.
        moz_optimize = bool(self.environment.substs.get("MOZ_OPTIMIZE"))
        stack_size = 262144 if moz_optimize else 1048576
        wasm_ldflags = [
            "-Wl,--export-all",
            "-Wl,--stack-first",
            f"-Wl,-z,stack-size={stack_size}",
            "-Wl,--no-entry",
            "-Wl,--import-memory",
            "-Wl,--import-table",
        ]
        for lib in self._wasm_libraries:
            # Output filename: the basename declared by
            # `SANDBOXED_WASM_LIBRARY_NAME` already includes `.wasm`; the
            # make rule uses `libdef.basename` directly. Match that.
            out = mozpath.join(lib.objdir, lib.basename)

            # Inputs: the wasm objects from the library's own context's
            # WasmSources, plus any SOURCES from this same library that
            # are wasm-compiled. SandboxedWasmLibrary's `objs` contain
            # full paths.
            objs = list(lib.objs)
            link_inputs = [mozpath.normsep(o) for o in objs]
            # WASM_LIBS is a per-context VariablePassthru entry (e.g.
            # `wasi-emulated-process-clocks` for sandboxes that need
            # wasi clock emulation). Mirror make's
            # `$(addprefix -l,$(WASM_LIBS))`.
            passthru = self._variable_passthru.get(lib.relobjdir)
            wasm_libs = []
            if passthru:
                wasm_libs = list(passthru.variables.get("WASM_LIBS", []))
            libs_flag = " ".join(f"-l{n_value(s)}" for s in wasm_libs)
            writer.build(
                self._n_rel(out),
                "wasm_link",
                inputs=[self._n_rel(o) for o in link_inputs],
                variables={
                    "libs": libs_flag,
                    "wasm_ldflags": " ".join(response_arg(f) for f in wasm_ldflags),
                },
            )

    def _emit_generated_file_statements(self, writer):
        """Emit a ninja rule for each GeneratedFile.

        Mirrors the recursive-make backend's py_action(file_generate, ...)
        invocation. Each GeneratedFile has one script, one method, one or
        more outputs, zero or more inputs, and optional flags. The
        file_generate driver produces a Makefile-style depfile which ninja
        consumes via `deps = gcc`."""
        writer.newline()
        writer.comment("------ generated files ------")
        writer.newline()
        for g in self._generated_files:
            if not g.script:
                # No script: outputs are declared but produced some other way
                # (e.g. preprocessed files tracked elsewhere). Skip.
                continue
            base_outputs = []
            for o in g.outputs:
                if isinstance(o, str):
                    # outputs can be relative to g.objdir (by mozbuild
                    # convention) or ObjDirPath-rooted (leading "!").
                    if o.startswith("/"):
                        full = mozpath.join(self._topobjdir, o[1:])
                    else:
                        full = mozpath.join(g.objdir, o)
                else:
                    full = mozpath.normsep(o.full_path)
                base_outputs.append(full)
            if not base_outputs:
                continue

            base_inputs = [mozpath.normsep(inp.full_path) for inp in g.inputs]

            # Localized GeneratedFile: en-US only in the build graph.
            # Outputs may contain `{AB_CD}`/`{AB_rCD}` placeholders (per
            # context.py:1723), both of which expand to "" for en-US.
            # Non-en-US locales are staged at command time via
            # `mach langpack` / `mach repackage-zip`.
            if g.localized:
                outs = [
                    o.replace("{AB_CD}", "").replace("{AB_rCD}", "")
                    for o in base_outputs
                ]
                ins = [
                    p.replace("{AB_CD}", "").replace("{AB_rCD}", "")
                    for p in base_inputs
                ]
                locale_arg = "--locale=en-US "
            else:
                outs = list(base_outputs)
                ins = list(base_inputs)
                locale_arg = ""

            primary = outs[0]
            # The wasm2c codegen step (driven through `config/wasm2c.py`)
            # supports a `--num-outputs N` flag that splits its output
            # into N files named `<base>_0.<ext>` ... `<base>_{N-1}.<ext>`.
            # The GeneratedFile declaration only lists the primary output
            # name (recursive-make tolerates this; ninja must declare
            # every produced file). Synthesize the split outputs so
            # downstream SOURCES references them resolve to a real edge.
            outs = list(
                self._expand_num_outputs_outputs(primary, outs, g.flags or ())
            )
            depfile = mozpath.join(
                mozpath.dirname(primary), ".deps", mozpath.basename(primary) + ".pp"
            )

            rel_inputs = [self._n_rel(i) for i in ins]
            # Input paths in `extra` stay absolute. Some pygen scripts
            # (e.g. `toolkit/components/gecko-trace/scripts/schema_parser.py`)
            # do `Path(input).relative_to(topsrcdir)` and that only
            # works on absolute paths. The `inputs` build-edge slot
            # still uses `_n_rel` form so the build graph stays
            # relative; only the positional argv to the script is
            # absolute.
            extra_parts = list(ins)
            if g.flags:
                # Expand make-style `$(DEFINES)` / `$(LOCAL_INCLUDES)`
                # references that some scripts (e.g.
                # config/external/ffi/preprocess_libffi_asm.py) embed in
                # their flag list. Make would expand them at recipe
                # time; ninja invokes commands directly, so substitute
                # here using the GeneratedFile's context.
                for f in g.flags:
                    extra_parts.extend(
                        self._expand_make_flag_refs(g.relobjdir, str(f))
                    )

            script_path = g.script
            writer.build(
                [self._n_rel(o) for o in outs],
                "pygen",
                inputs=rel_inputs if rel_inputs else None,
                # Script is an implicit dep so ninja rebuilds when the
                # script changes.
                implicit=self._n_rel(script_path),
                variables={
                    "script": self._n_rel(script_path),
                    "method": n_value(g.method or "main"),
                    "primary": self._n_rel(primary),
                    "depfile": self._n_rel(depfile),
                    "locale": locale_arg,
                    # `response_arg` (not `n_value`) so joined-string
                    # entries from `_expand_make_flag_refs` (e.g. the
                    # `-D... -D...` collapsed `$(DEFINES)`) survive
                    # `CreateProcess` argv parsing as one argv entry,
                    # matching make's `'$(DEFINES)'` shell quoting.
                    "extra": " ".join(response_arg(p) for p in extra_parts),
                },
            )

    def _emit_ipdl_statements(self, writer):
        """Emit the IPDL codegen rule.

        The emitter produces a single IPDLCollection for the whole tree.
        Mirrors `RecursiveMakeBackend._handle_ipdl_sources` plus the
        ipdl.track rule from `ipc/ipdl/Makefile.in`: each PREPROCESSED
        IPDL source is preprocessed into the IPDL_ROOT directory under
        its basename, then a single ipdl.py invocation consumes that set
        plus the static IPDL sources via `--file-list ipdlsrcs.txt`.

        Declared outputs are the .cpp files UnifiedSources references
        (suffix_map from the emitter: `.ipdl` → `.cpp`/`Child.cpp`/
        `Parent.cpp`; `.ipdlh` → `.cpp`) plus the global
        `IPCMessageTypeName.cpp`. The .h files generated alongside have
        protocol-namespace-dependent paths that ninja cannot predict
        without parsing the .ipdl; those propagate to consumers via the
        compile depfile chain.
        """
        col = self._ipdl_collection
        if not col:
            return

        writer.newline()
        writer.comment("------ IPDL codegen ------")
        writer.newline()

        ipdl_root = mozpath.normsep(col.objdir)
        topsrc_ipdl = mozpath.join(self._topsrcdir, "ipc/ipdl")
        headers_dir = mozpath.join(ipdl_root, "_ipdlheaders")
        sync_msg_list = mozpath.join(topsrc_ipdl, "sync-messages.ini")
        msg_metadata = mozpath.join(topsrc_ipdl, "message-metadata.ini")
        ipdlsrcs_txt = mozpath.join(ipdl_root, "ipdlsrcs.txt")
        ipdl_script = mozpath.join(topsrc_ipdl, "ipdl.py")

        sorted_static = sorted(col.all_regular_sources())
        sorted_preprocessed = sorted(col.all_preprocessed_sources())

        # Per-preprocessed-IPDL preprocessor edges. The output basename
        # lands in IPDL_ROOT so ipdl.py finds it via the file list. We
        # reuse `pp_install` (same preprocessor invocation shape).
        # Defines: ACDEFINES from substs (already shell-quoted -DKEY=VAL
        # tokens) plus any DEFINES from the ipc/ipdl/ context.
        acdefines = self.environment.substs.get("ACDEFINES", "")
        per_dir_defines = self._computed_flag_list(col.relobjdir, "DEFINES")
        defines_str = " ".join(per_dir_defines)
        if acdefines:
            defines_str = f"{defines_str} {acdefines}" if defines_str else acdefines

        preprocessed_outputs = []
        seen_pp = set()
        for raw_src in sorted_preprocessed:
            src = mozpath.normsep(raw_src)
            basename = mozpath.basename(src)
            out = mozpath.join(ipdl_root, basename)
            if out in seen_pp:
                continue
            seen_pp.add(out)
            preprocessed_outputs.append(out)
            writer.build(
                self._n_rel(out),
                "pp_install",
                inputs=self._n_rel(src),
                variables={"defines": defines_str},
            )

        # ipdlsrcs.txt: absolute paths so ipdl.py's `open(f)` works
        # regardless of cwd (ninja runs commands from $topobjdir, not
        # ipc/ipdl/). Bug 1885948 in the recursive-make backend uses a
        # file list to dodge Windows command-line length limits; the
        # ninja path inherits that benefit.
        all_sources_for_list = preprocessed_outputs + [
            mozpath.normsep(s) for s in sorted_static
        ]
        with self._write_file(ipdlsrcs_txt) as fh:
            for p in all_sources_for_list:
                fh.write(f"{p}\n")

        # Include search path: IPDL_ROOT (for preprocessed outputs) plus
        # the source directory of every static source. Matches
        # IPDLDIRS in the make backend.
        include_dirs = [ipdl_root]
        include_dirs.extend(
            sorted(set(mozpath.dirname(mozpath.normsep(p)) for p in sorted_static))
        )

        # Declared outputs: per-source .cpp files + global IPCMessageTypeName.cpp.
        outputs = []
        for src in sorted_preprocessed + sorted_static:
            root, ext = mozpath.splitext(mozpath.basename(src))
            if ext == ".ipdl":
                for suffix in ("", "Child", "Parent"):
                    outputs.append(mozpath.join(ipdl_root, f"{root}{suffix}.cpp"))
            elif ext == ".ipdlh":
                outputs.append(mozpath.join(ipdl_root, f"{root}.cpp"))
        outputs.append(mozpath.join(ipdl_root, "IPCMessageTypeName.cpp"))

        # Stash for `_emit_compile_statements` to fold into `.ninja-generated`,
        # so consumer compiles get an order_only edge on IPDL codegen.
        self._ipdl_outputs = list(outputs)

        # Inputs: preprocessed outputs + static sources + ipdlsrcs.txt +
        # the two ini files. ipdl python modules go in `implicit` since
        # they're rule-side deps (any .py change should re-run codegen).
        inputs = list(preprocessed_outputs)
        inputs.extend(mozpath.normsep(s) for s in sorted_static)
        inputs.append(ipdlsrcs_txt)
        inputs.append(sync_msg_list)
        inputs.append(msg_metadata)

        # ipdl_py_deps from ipc/ipdl/Makefile.in: every .py module the
        # codegen driver imports. Replace the parse-time $(wildcard ...)
        # with an explicit glob at backend-write time so deps are
        # represented in the static graph.
        ipdl_py_deps = []
        for sub in (
            mozpath.join(topsrc_ipdl),
            mozpath.join(topsrc_ipdl, "ipdl"),
            mozpath.join(topsrc_ipdl, "ipdl/cxx"),
            mozpath.join(self._topsrcdir, "other-licenses/ply/ply"),
        ):
            if os.path.isdir(sub):
                for fn in sorted(os.listdir(sub)):
                    if fn.endswith(".py"):
                        ipdl_py_deps.append(mozpath.join(sub, fn))

        include_args = " ".join(f"-I{self._n_rel(d)}" for d in include_dirs)

        writer.build(
            [self._n_rel(o) for o in outputs],
            "ipdl",
            inputs=[self._n_rel(i) for i in inputs],
            implicit=[self._n_rel(p) for p in ipdl_py_deps],
            variables={
                "script": self._n_rel(ipdl_script),
                "sync_msg_list": self._n_rel(sync_msg_list),
                "msg_metadata": self._n_rel(msg_metadata),
                "headers_dir": self._n_rel(headers_dir),
                "cpp_dir": self._n_rel(ipdl_root),
                "file_list": self._n_rel(ipdlsrcs_txt),
                "include_args": include_args,
            },
        )

    def _emit_webidl_statements(self, writer):
        """Emit the WebIDL codegen rule.

        Mirrors `RecursiveMakeBackend._handle_webidl_build` plus the
        `webidl.stub` rule from `dom/bindings/Makefile.in`: each
        preprocessed `.webidl` is preprocessed into the bindings
        directory under its basename, then a single
        `mozbuild.action.webidl` invocation reads `file-lists.json`
        (already written by `CommonBackend._handle_webidl_collection`)
        and produces every binding `.h`/`.cpp` plus the global-define
        files.

        `expected_build_output_files` is the authoritative output set
        returned by `WebIDLCodegenManager.expected_build_output_files`,
        passed in by the CommonBackend hook.

        Cross-rule dependencies (Bindings.conf, the bindings parser, the
        codegen modules) are tracked dynamically through the depfile
        (`codegen.pp`) the manager writes; ninja consumes it via
        `deps = gcc`.
        """
        if not self._webidl:
            return
        (
            bindings_dir,
            unified_source_mapping,
            webidls,
            expected_build_output_files,
            global_define_files,
        ) = self._webidl

        writer.newline()
        writer.comment("------ WebIDL codegen ------")
        writer.newline()

        file_lists_json = mozpath.join(bindings_dir, "file-lists.json")
        depfile = mozpath.join(bindings_dir, "codegen.pp")

        # Per-preprocessed-WebIDL preprocessor edges. The preprocessed
        # output basename lands in bindings_dir; the codegen reads from
        # there (and from static .webidl source paths) per file-lists.json.
        # ACDEFINES + per-dir DEFINES match make's
        # `$(DEFINES) $(ACDEFINES)` substitution.
        acdefines = self.environment.substs.get("ACDEFINES", "")
        webidl_relobjdir = mozpath.relpath(bindings_dir, self._topobjdir)
        per_dir_defines = self._computed_flag_list(webidl_relobjdir, "DEFINES")
        defines_str = " ".join(per_dir_defines)
        if acdefines:
            defines_str = f"{defines_str} {acdefines}" if defines_str else acdefines

        sorted_pp = sorted(webidls.all_preprocessed_sources())
        preprocessed_outputs = []
        seen_pp = set()
        for raw_src in sorted_pp:
            src = mozpath.normsep(raw_src)
            basename = mozpath.basename(src)
            out = mozpath.join(bindings_dir, basename)
            if out in seen_pp:
                continue
            seen_pp.add(out)
            preprocessed_outputs.append(out)
            writer.build(
                self._n_rel(out),
                "pp_install",
                inputs=self._n_rel(src),
                variables={"defines": defines_str},
            )

        # Stash outputs for `_emit_compile_statements`.
        self._webidl_outputs = list(expected_build_output_files)

        # Inputs: file-lists.json (the canonical first-run trigger),
        # preprocessed outputs (now landed in bindings_dir), and every
        # static webidl path. Static-source list comes from
        # all_static_sources() — sources, generated_events_sources,
        # test_sources.
        inputs = [file_lists_json]
        inputs.extend(preprocessed_outputs)
        inputs.extend(mozpath.normsep(s) for s in sorted(webidls.all_static_sources()))

        # Implicit Python deps: `mozwebidlcodegen` machinery + the
        # `dom/bindings/` Python modules (Codegen.py, Configuration.py,
        # parser/WebIDL.py, etc.) plus the action wrapper. Glob at
        # backend-write time. Subsequent reruns also pick up dep changes
        # via codegen.pp's gcc-format depfile.
        topsrc_bindings = mozpath.join(self._topsrcdir, "dom/bindings")
        webidl_py_deps = []
        for sub in (
            topsrc_bindings,
            mozpath.join(topsrc_bindings, "mozwebidlcodegen"),
            mozpath.join(topsrc_bindings, "parser"),
            mozpath.join(self._topsrcdir, "python/mozbuild/mozbuild/action"),
        ):
            if os.path.isdir(sub):
                for fn in sorted(os.listdir(sub)):
                    if fn.endswith(".py"):
                        webidl_py_deps.append(mozpath.join(sub, fn))
        # Bindings.conf is a config file, not a .py — pull it in explicitly.
        bindings_conf = mozpath.join(topsrc_bindings, "Bindings.conf")
        webidl_py_deps.append(bindings_conf)

        writer.build(
            [self._n_rel(o) for o in expected_build_output_files],
            "webidl",
            inputs=[self._n_rel(i) for i in inputs],
            implicit=[self._n_rel(p) for p in webidl_py_deps],
            variables={
                "depfile": self._n_rel(depfile),
            },
        )

    def _emit_xpidl_statements(self, writer):
        """Emit XPIDL per-module rules and the aggregate link rule.

        Mirrors `RecursiveMakeBackend._handle_idl_manager` plus the
        `%.xpt:` and `xptdata.cpp` rules from
        `config/makefiles/xpidl/Makefile.in`. One ninja edge per
        `XPIDLModule` invokes `xpidl-process.py` over the module's
        `.idl` files; outputs are the per-stem `.h` / `.rs` files
        landing in `dist/include` / `dist/xpcrs`, plus `<module>.xpt`
        and `<module>.d.json`. A single `xpidl_link` edge then merges
        every `<module>.xpt` into `xptdata.cpp` + `dist/include/xptdata.h`.
        """
        manager = self._xpidl_manager
        if not manager or not manager.modules:
            return

        writer.newline()
        writer.comment("------ XPIDL codegen ------")
        writer.newline()

        topobjdir = self._topobjdir
        topsrcdir = self._topsrcdir
        dist_include = mozpath.join(topobjdir, "dist/include")
        dist_xpcrs = mozpath.join(topobjdir, "dist/xpcrs")
        # Per recursive-make: the .xpt files and per-module deps live
        # alongside the make-driven xpidl Makefile, under
        # `config/makefiles/xpidl/`. Match that exactly so xptdata.cpp
        # consumers and any external tooling find them in the same place.
        xpt_dir = mozpath.join(topobjdir, "config/makefiles/xpidl")
        deps_dir = mozpath.join(xpt_dir, ".deps")

        process_py = mozpath.join(
            topsrcdir, "python/mozbuild/mozbuild/action/xpidl-process.py"
        )
        xptcodegen_py = mozpath.join(topsrcdir, "xpcom/reflect/xptinfo/xptcodegen.py")
        bindings_conf = mozpath.join(topsrcdir, "dom/bindings/Bindings.conf")
        perfecthash_py = mozpath.join(topsrcdir, "xpcom/ds/tools/perfecthash.py")

        # `all_idl_dirs` is the union of every directory containing an .idl
        # in any module — every per-module invocation gets the same -I list
        # so cross-module includes resolve.
        # Kept absolute (not routed through `_n_rel`) because
        # `xpidl_batch.py` does `os.path.join(topsrcdir, p)` on each -I
        # arg, which only behaves correctly when `p` is already absolute.
        all_idl_dirs = sorted({
            mozpath.dirname(idl)
            for m in manager.modules.values()
            for idl in m.idl_files
        })
        include_args = " ".join(f"-I{n_path(d)}" for d in all_idl_dirs)

        # Track every header output for `.ninja-generated` (consumer
        # compiles need them before they can resolve `#include`s).
        header_outputs = []
        xpt_files = []
        all_idl_inputs = set()
        manifest_entries = []
        all_outputs = []

        for module_name in sorted(manager.modules.keys()):
            module = manager.modules[module_name]
            idl_files = sorted(module.idl_files)
            stems = sorted(
                set(mozpath.splitext(mozpath.basename(p))[0] for p in idl_files)
            )

            for stem in stems:
                all_outputs.append(mozpath.join(dist_include, f"{stem}.h"))
                all_outputs.append(mozpath.join(dist_xpcrs, "rt", f"{stem}.rs"))
                all_outputs.append(mozpath.join(dist_xpcrs, "bt", f"{stem}.rs"))
                header_outputs.append(mozpath.join(dist_include, f"{stem}.h"))

            xpt_path = mozpath.join(xpt_dir, f"{module_name}.xpt")
            ts_path = mozpath.join(xpt_dir, f"{module_name}.d.json")
            all_outputs.append(xpt_path)
            all_outputs.append(ts_path)
            xpt_files.append(xpt_path)

            all_idl_inputs.update(idl_files)
            manifest_entries.append({
                "module": module_name,
                "idls": list(idl_files),
            })

        # One ninja edge for ALL modules; manifest passed to xpidl_batch
        # so the per-module process() call happens in a loop inside one
        # Python process. Combined depfile is keyed on the first .xpt
        # (the edge's primary output) so ninja's `deps = gcc` parser
        # matches it.
        manifest_path = mozpath.join(topobjdir, ".ninja-xpidl-batch.manifest")
        with self._write_file(manifest_path) as mh:
            json.dump(manifest_entries, mh, indent=2)
        primary_output = all_outputs[0] if all_outputs else None
        combined_depfile = mozpath.join(deps_dir, "xpidl-batch.pp")
        if primary_output:
            writer.build(
                [self._n_rel(o) for o in all_outputs],
                "xpidl_batch",
                inputs=[self._n_rel(p) for p in sorted(all_idl_inputs)],
                implicit=[
                    self._n_rel(process_py),
                    self._n_rel(bindings_conf),
                    self._n_rel(manifest_path),
                ],
                variables={
                    "deps_dir": self._n_rel(deps_dir),
                    "bindings_conf": self._n_rel(bindings_conf),
                    "include_args": include_args,
                    "header_dir": self._n_rel(dist_include),
                    "xpcrs_dir": self._n_rel(dist_xpcrs),
                    "xpt_dir": self._n_rel(xpt_dir),
                    "manifest": self._n_rel(manifest_path),
                    "primary": self._n_rel(primary_output),
                    "depfile": self._n_rel(combined_depfile),
                },
            )

        # Aggregate link: one xptdata.cpp + dist/include/xptdata.h from
        # every module's .xpt. The C++ output's location matches what
        # the make backend writes (xpcom/reflect/xptinfo/xptdata.cpp).
        xptdata_cpp = mozpath.join(topobjdir, "xpcom/reflect/xptinfo/xptdata.cpp")
        xptdata_h = mozpath.join(dist_include, "xptdata.h")
        writer.build(
            [self._n_rel(xptdata_cpp), self._n_rel(xptdata_h)],
            "xpidl_link",
            inputs=[self._n_rel(p) for p in sorted(xpt_files)],
            implicit=[self._n_rel(xptcodegen_py), self._n_rel(perfecthash_py)],
            variables={
                "script": self._n_rel(xptcodegen_py),
                "outfile": self._n_rel(xptdata_cpp),
                "outheader": self._n_rel(xptdata_h),
                "xpts": " ".join(self._n_rel(p) for p in sorted(xpt_files)),
            },
        )
        header_outputs.append(xptdata_h)

        # Stash for `_emit_compile_statements` to fold into `.ninja-generated`,
        # so consumer compiles get an order_only edge on XPIDL codegen.
        self._xpidl_outputs = list(header_outputs)

    def _emit_jar_statements(self, writer):
        """Emit `jar_maker` edges per `JARManifest`.

        Mirrors `config/rules.mk`'s `JAR_MANIFEST` recipe: invokes
        `mozbuild.action.jar_maker` with `-d $(FINAL_TARGET) -t $(topsrcdir)
        -f $(MOZ_JAR_MAKER_FILE_FORMAT) --relativesrcdir=<relsrcdir>`. Only
        en-US is emitted in the build graph; non-en-US locales are staged
        at command time via `mach langpack` / `mach repackage-zip`
        consuming `staging-spec.json`.
        """
        self._jar_maker_stamps = []
        if not self._jar_manifests:
            return

        writer.newline()
        writer.comment("------ jar.mn packaging ------")
        writer.newline()

        jar_format = self.environment.substs.get("MOZ_JAR_MAKER_FILE_FORMAT", "jar")
        acdefines = self.environment.substs.get("ACDEFINES", "")

        # Conservative implicit deps: jar.mn entries can reference any
        # `!path` GeneratedFile output (e.g. `aiwindow/manifest.json`
        # from a process_tokens.py pygen rule). Only the
        # `required_before_compile`/`required_during_compile` subset
        # gets folded into `.ninja-generated`, so make every jar_maker
        # edge wait on all generated outputs. Mozmake gets this via
        # tier ordering (jar_maker runs in `misc` after `pre-compile`).
        all_gen_outputs = []
        for g in self._generated_files:
            if not g.script:
                continue
            declared = []
            for o in g.outputs:
                if isinstance(o, str):
                    if o.startswith("/"):
                        declared.append(mozpath.join(self._topobjdir, o[1:]))
                    else:
                        declared.append(mozpath.join(g.objdir, o))
                else:
                    declared.append(mozpath.normsep(o.full_path))
            if not declared:
                continue
            # Apply `--num-outputs N` expansion (wasm2c) so we depend on
            # the actually-produced `<base>_0.c` ... `<base>_{N-1}.c`,
            # not the unwritten primary.
            all_gen_outputs.extend(
                self._expand_num_outputs_outputs(declared[0], declared, g.flags or ())
            )

        for jar in self._jar_manifests:
            jar_path = mozpath.normsep(jar.path.full_path)
            final_target = mozpath.join(self._topobjdir, jar.install_target)
            # Per-dir DEFINES come from moz.build's `DEFINES["KEY"] = ...`
            # (e.g. toolkit/content/moz.build sets TOPOBJDIR which
            # buildconfig.html references). The emitter delivers those as
            # `Defines` objects, not via ComputedFlags.
            per_dir_defines = []
            for d in self._defines_by_dir.get(jar.relobjdir, ()):
                per_dir_defines.extend(d.get_defines())

            locale_srcdir = mozpath.join(jar.srcdir, "en-US")

            # Per-dir DEFINES + ACDEFINES + AB_CD=en-US. Per-dir defines
            # can contain spaces in their values (e.g.
            # `-DCC=clang-cl.exe -fms-compatibility-version=19.50`);
            # quote each so CreateProcess preserves them as single
            # args. ACDEFINES is already shell-quoted by configure;
            # pass through as-is.
            defines_parts = [response_arg(d) for d in per_dir_defines]
            if acdefines:
                defines_parts.append(acdefines)
            defines_parts.append("-DAB_CD=en-US")
            defines_str = " ".join(defines_parts)

            stamp = mozpath.join(jar.objdir, ".jar-maker.stamp")
            # `-s <jar.objdir>` lets jar_maker find generated source
            # files (e.g. `!aiwindow/manifest.json`) — mozmake gets
            # this for free because each Makefile runs in its own
            # objdir (jar.py auto-adds os.getcwd() to its search path),
            # but our ninja invocation runs from $topobjdir.
            jar_args = [
                f"--relativesrcdir={mozpath.normsep(jar.relsrcdir)}",
                f"-s {self._n_rel(jar.objdir)}",
                f"-c {self._n_rel(locale_srcdir)}",
            ]
            writer.build(
                self._n_rel(stamp),
                "jar_maker",
                inputs=self._n_rel(jar_path),
                implicit=(
                    [self._n_rel(o) for o in all_gen_outputs]
                    if all_gen_outputs
                    else None
                ),
                variables={
                    "final_target": self._n_rel(final_target),
                    "topsrcdir": self._n_rel(self._topsrcdir),
                    "jar_format": n_value(jar_format),
                    "jar_args": " ".join(jar_args),
                    "defines": defines_str,
                    "jar_manifest": self._n_rel(jar_path),
                    "stamp": self._n_rel(stamp),
                },
            )
            self._jar_maker_stamps.append(stamp)

    def _emit_install_statements(self, writer):
        """Delegate to mozmake's `process_install_manifest` for each
        install target. The manifests under _build_manifests/install/
        are written by CommonBackend-derived backends at configure time
        and already handle wildcards, pattern-links, preprocessed
        content, etc. One ninja edge per install target, each one a
        single Python invocation — matches mozmake's own install cost."""
        writer.newline()
        writer.comment("------ install manifests ------")
        writer.newline()
        manifests_dir = mozpath.join(self._topobjdir, "_build_manifests/install")
        self._install_tracks = {}
        if not os.path.isdir(manifests_dir):
            return

        manifest_to_target = {
            "dist_include": "dist/include",
            "dist_public": "dist/public",
            "dist_private": "dist/private",
            "dist_bin": "dist/bin",
            "_tests": "_tests",
            "_test_files": "_tests/modules",
        }
        for manifest_name, target_rel in manifest_to_target.items():
            manifest_path = mozpath.join(manifests_dir, manifest_name)
            if not os.path.exists(manifest_path):
                continue
            install_dir = mozpath.join(self._topobjdir, target_rel)
            track_path = mozpath.join(
                self._topobjdir,
                f"install_{manifest_name}.track",
            )
            self._install_tracks[manifest_name] = track_path
            writer.build(
                self._n_rel(track_path),
                "run_install_manifest",
                inputs=self._n_rel(manifest_path),
                variables={
                    "install_dir": self._n_rel(install_dir),
                    "track": self._n_rel(track_path),
                },
            )

        # Generated-file installs (ObjDirPath EXPORTS etc.): batched into
        # two groups. dist/include-bound go in one batch (feeds compiles
        # via .ninja-generated); others (post-build OBJDIR_FILES that
        # mirror built binaries) go in a separate batch so binary inputs
        # don't create a dep cycle with compiles.
        dist_include_prefix = mozpath.join(self._topobjdir, "dist/include") + "/"
        batch_dist_include = []
        batch_post = []
        seen_inst = set()
        for src, dst in self._installs:
            if dst in seen_inst:
                continue
            seen_inst.add(dst)
            if dst.startswith(dist_include_prefix):
                batch_dist_include.append((src, dst))
            else:
                batch_post.append((src, dst))

        def _emit_install_batch(tag, pairs):
            if not pairs:
                return
            manifest_path = mozpath.join(
                self._topobjdir,
                f".ninja-gen-install-{tag}.manifest",
            )
            with self._write_file(manifest_path) as mh:
                for src, dst in pairs:
                    mh.write(f"{src}\t{dst}\n")
            outputs = [dst for _, dst in pairs]
            inputs = [src for src, _ in pairs]
            writer.build(
                [self._n_rel(o) for o in outputs],
                "install_batch",
                inputs=[self._n_rel(i) for i in inputs],
                implicit=self._n_rel(manifest_path),
                variables={"manifest": self._n_rel(manifest_path)},
            )
            return outputs

        _emit_install_batch("distinc", batch_dist_include)
        # Post batch outputs (dist/bin/dependentlibs.list etc.) feed the
        # `install` phony so they're reachable from the default target.
        # Without this, firefox.exe fails with "Couldn't load XPCOM"
        # because dependentlibs.list isn't staged into dist/bin.
        self._install_batch_post_outputs = _emit_install_batch("post", batch_post) or []

        # TestManifest staging: one `run_install_manifest` edge against the
        # ninja-private `_ninja_test_files` manifest written in
        # `consume_finished`. Tracks file feeds the `install` phony.
        ninja_test_manifest = getattr(self, "_ninja_test_manifest_path", None)
        if ninja_test_manifest:
            track_path = mozpath.join(
                self._topobjdir, "install__ninja_test_files.track"
            )
            install_dir = mozpath.join(self._topobjdir, "_tests")
            writer.build(
                self._n_rel(track_path),
                "run_install_manifest",
                inputs=self._n_rel(ninja_test_manifest),
                variables={
                    "install_dir": self._n_rel(install_dir),
                    "track": self._n_rel(track_path),
                },
            )
            self._install_tracks["_ninja_test_files"] = track_path

        # OBJDIR_PP_FILES: preprocess the .in and install. One edge per
        # output (no batching — each has unique defines). Mirror mozmake's
        # `$(DEFINES) $(ACDEFINES)` order: per-dir defines first, then
        # the global ACDEFINES from configure (which carries MOZ_BUILD_APP
        # and similar that AppConstants.sys.mjs references).
        acdefines = self.environment.substs.get("ACDEFINES", "")
        seen_pp = set()
        pp_install_outputs = []
        for src, dst, defines in self._pp_installs:
            if dst in seen_pp:
                continue
            seen_pp.add(dst)
            def_args = []
            for k, v in sorted(defines.items()):
                if v is True:
                    def_args.append(f"-D{k}")
                elif v is False:
                    pass
                else:
                    def_args.append(f"-D{k}={v}")
            defines_str = " ".join(response_arg(a) for a in def_args)
            if acdefines:
                defines_str = f"{defines_str} {acdefines}" if defines_str else acdefines
            writer.build(
                self._n_rel(dst),
                "pp_install",
                inputs=self._n_rel(src),
                variables={"defines": defines_str},
            )
            pp_install_outputs.append(dst)
        # Feed pp_install outputs into the `install` phony so they're
        # reachable from the default target (e.g. dist/bin/modules/
        # AppConstants.sys.mjs from EXTRA_PP_JS_MODULES). The dist/include
        # subset is already covered transitively by .ninja-generated, but
        # listing them again is harmless.
        self._pp_install_outputs = pp_install_outputs

    def _emit_rust_statements(self, writer):
        """Delegate Rust library builds to mozmake in the rust subdir.

        The recursive-make backend's config/makefiles/rust.mk wraps a cargo
        invocation that sets CARGO_TARGET_DIR, RUSTFLAGS, etc. We delegate
        to mozmake for the rust subdir — same opaque-sub-build pattern
        used for ICU."""
        writer.newline()
        writer.comment("------ rust libraries (opaque mozmake sub-build) ------")
        writer.newline()
        if not self._rust_libs:
            return
        for lib in self._rust_libs:
            out = self._lib_output_path(lib)
            depfile = mozpath.splitext(out)[0] + ".d"
            # Match recursive-make's `_build_target_for_obj`: when a
            # `RustLibrary` has `output_category` set (e.g. gkrust-gtest
            # with `output_category="gtest"`), its make target is named
            # by the category instead of `target-objects`.
            output_category = getattr(lib, "output_category", None)
            target_name = output_category if output_category else "target-objects"
            writer.build(
                self._n_rel(out),
                "cargo_build",
                order_only=[".ninja-generated"],
                variables={
                    "cargo_target": f"{n_path(lib.relobjdir)}/{target_name}",
                    "depfile": self._n_rel(depfile),
                },
            )

    def _lib_output_path(self, lib):
        """Return the on-disk path of a library's output .lib / .dll.

        RustLibrary's output lives under the cargo target directory
        (`$objdir/$triple/release/jsrust.lib`), not in its `objdir`, so
        we go through `import_path` for those."""
        if isinstance(lib, RustLibrary):
            return mozpath.normsep(lib.import_path.full_path)
        if isinstance(lib, SharedLibrary):
            return mozpath.join(lib.objdir, getattr(lib, "import_name", lib.lib_name))
        return mozpath.join(lib.objdir, lib.lib_name or lib.basename)
