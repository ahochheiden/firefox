# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja backend.

Emits a single `build.ninja` at `$topobjdir`.
"""

import glob
import os
import sys
from collections import defaultdict

import mozpack.path as mozpath
from mozfile import json
from mozpack.manifests import InstallManifest

from mozbuild.backend.common import CommonBackend
from mozbuild.backend.ninja_syntax import (
    NinjaWriter,
)
from mozbuild.backend.ninja_syntax import (
    path as n_path,
)
from mozbuild.backend.ninja_syntax import (
    response_arg as _response_arg_raw,
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
    HostRustProgram,
    HostSharedLibrary,
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
    RustProgram,
    RustTests,
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
from mozbuild.util import cpu_count

from .compile import CompileMixin
from .generated import GeneratedMixin
from .helpers import strip_tests
from .host import HostMixin
from .idl import IdlMixin
from .install import InstallMixin
from .jar import JarMixin
from .l10n import L10nMixin
from .link import LinkMixin
from .runtime import RuntimeMixin
from .rust import RustMixin
from .wasm import WasmMixin


class NinjaBackend(
    CompileMixin,
    LinkMixin,
    HostMixin,
    WasmMixin,
    IdlMixin,
    GeneratedMixin,
    JarMixin,
    L10nMixin,
    InstallMixin,
    RustMixin,
    RuntimeMixin,
    CommonBackend,
):
    """Emits `build.ninja`."""

    @staticmethod
    def build_output_handler():
        from .logging import NinjaOutputHandler

        return NinjaOutputHandler()

    def _init(self):
        super()._init()

        env = self.environment

        # The Ninja backend has only been exercised with clang/clang-cl. Its
        # compile/asm rules assume clang-style flags (-Xclang -dependency-file,
        # etc.), so any other toolchain will likely produce a broken build.
        # Refuse to run unless the user opts out via MOZ_NINJA_ALLOW_NON_CLANG.
        clang_types = ("clang", "clang-cl")
        unsupported_compilers = {
            var: env.substs.get(var)
            for var in ("CC_TYPE", "HOST_CC_TYPE")
            if env.substs.get(var) and env.substs.get(var) not in clang_types
        }
        if unsupported_compilers:
            detail = ", ".join(
                f"{k}={v}" for k, v in sorted(unsupported_compilers.items())
            )
            if os.environ.get("MOZ_NINJA_ALLOW_NON_CLANG"):
                print(
                    "warning: the Ninja backend has only been tested with "
                    f"clang/clang-cl; proceeding with {detail} because "
                    "MOZ_NINJA_ALLOW_NON_CLANG is set.",
                    file=sys.stderr,
                )
            else:
                raise Exception(
                    "The Ninja backend has only been tested with clang/clang-cl, "
                    f"but configure detected {detail}. Reconfigure with clang, or "
                    "set MOZ_NINJA_ALLOW_NON_CLANG=1 to build anyway (untested)."
                )

        self._topsrcdir = mozpath.normsep(env.topsrcdir)
        self._topobjdir = mozpath.normsep(env.topobjdir)

        # Ninja `pool` depths are baked into `build.ninja` at write time
        # (no runtime substitution), so we capture the jobs count here.
        # Matches the default `runtime.py:_run_ninja` uses when `-j 0`.
        self._jobs = (cpu_count() or 1) + 2
        self._cargo_pool_depth = 2
        self._compile_pool_depth = max(1, self._jobs * 7 // 8)

        # Collected objects, keyed by the directory they belong to.
        self._sources_by_dir = defaultdict(list)  # relobjdir -> [Sources, ...]
        self._unified_by_dir = defaultdict(list)  # relobjdir -> [UnifiedSources, ...]
        self._host_sources_by_dir = defaultdict(list)  # relobjdir -> [HostSources, ...]
        self._static_libs = []
        self._shared_libs = []
        self._rust_libs = []
        self._rust_programs = []
        self._host_rust_programs = []
        self._rust_tests = []
        # output_path -> short library name (e.g. "gkrust"). Populated
        # during emit; persisted to `.ninja-rust-libs.json` so it
        # survives `./mach build-backend && ./mach build`.
        self._rust_lib_outputs = {}
        self._programs = []
        self._host_libraries = []
        self._host_programs = []
        self._host_shared_libs = []
        self._generated_files = []
        # output_path -> [post-link stamp paths]. Populated by the
        # program/shared-link emitters via `_emit_post_link_stamps`.
        # Binary stamps run on default build (folded into the
        # `binaries` phony); syms stamps run on `ninja syms` only,
        # mirroring make's `syms::` target (rules.mk:654).
        self._post_link_stamps = {}
        self._syms_stamps = []
        # Networking-check stamps for rust staticlibs (proxy-bypass guard),
        # folded into the `binaries` phony so they run on a default build.
        self._rust_netcheck_stamps = []
        # Per-directory ComputedFlags. The emitter yields TWO ComputedFlags
        # objects per context — one from COMPILE_FLAGS (keys like CXXFLAGS,
        # CFLAGS, CXX_LDFLAGS, C_LDFLAGS) and one from LINK_FLAGS (key
        # LDFLAGS only, including `-DEF:<deffile>`). Keep a list so neither
        # overwrites the other on lookup.
        self._computed_flags = defaultdict(list)  # relobjdir -> [ComputedFlags, ...]
        self._per_source_flags = defaultdict(list)  # relobjdir -> [PerSourceFlag, ...]
        self._variable_passthru = {}  # relobjdir -> VariablePassthru
        self._xpi_packages = []  # [(xpi_output_path, source_dir)]

        # File install targets: Exports (→ dist/include), ObjdirFiles (→
        # objdir root), ObjdirPreprocessedFiles (→ objdir root, run through
        # preprocessor).
        self._installs = []  # [(src, dest)] — simple copies
        self._pp_installs = []  # [(src, dst, defines_dict, extra_deps)] — preprocess + copy
        self._install_manifests = defaultdict(InstallManifest)

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
        # Per-stem `.rs` files xpidl produces in `dist/xpcrs/{rt,bt}/`,
        # `include!()`'d by xpcom/rust/xpcom. Folded into the
        # rust-prereqs phony so cargo waits for them on cold builds.
        self._xpidl_rust_outputs = []

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
        elif isinstance(obj, RustProgram):
            self._rust_programs.append(obj)
        elif isinstance(obj, HostRustProgram):
            self._host_rust_programs.append(obj)
        elif isinstance(obj, RustTests):
            self._rust_tests.append(obj)
        elif isinstance(obj, SandboxedWasmLibrary):
            self._wasm_libraries.append(obj)
        elif isinstance(obj, StaticLibrary):
            self._static_libs.append(obj)
        elif isinstance(obj, SharedLibrary):
            self._shared_libs.append(obj)
        elif isinstance(obj, HostSharedLibrary):
            self._host_shared_libs.append(obj)
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
            if obj.installed:
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
            if "XPI_PKGNAME" in obj.variables:
                self._collect_xpi_package(obj)
        elif isinstance(obj, LocalizedPreprocessedFiles):
            # LOCALIZED_PP_FILES: en-US preprocessed install. en-US source
            # is `<srcdir>/en-US/<rest>` (already what `f.full_path`
            # resolves to). Mozmake adds `-DAB_CD=en-US` at preprocess
            # time (config/config.mk:317); mirror that in the per-edge
            # defines so AB_CD-conditional `#filter` blocks resolve.
            # Non-en-US locales are staged at command time via
            # `mach langpack` / `mach repackage-zip`.
            if not obj.installed:
                return True
            install_target = obj.install_target
            defines = {"AB_CD": "en-US"}
            extra_deps = [mozpath.normsep(d.full_path) for d in obj.extra_deps]
            for subpath, files in obj.files.walk():
                for f in files:
                    if "*" in f:
                        for matched in sorted(glob.glob(f.full_path)):
                            src = mozpath.normsep(matched)
                            dst = mozpath.join(
                                self._topobjdir,
                                install_target,
                                subpath,
                                mozpath.basename(matched),
                            )
                            self._pp_installs.append((src, dst, defines, extra_deps))
                        continue
                    src = mozpath.normsep(f.full_path)
                    basename = FinalTargetPreprocessedFiles.get_obj_basename(f)
                    dst = mozpath.join(
                        self._topobjdir, install_target, subpath, basename
                    )
                    self._pp_installs.append((src, dst, defines, extra_deps))
        elif isinstance(obj, LocalizedFiles):
            # LOCALIZED_FILES: en-US install. Non-en-US locales are
            # staged at command time via `mach langpack`.
            if not obj.installed:
                return True
            install_target = obj.install_target
            for subpath, files in obj.files.walk():
                for f in files:
                    if "*" in f:
                        for matched in sorted(glob.glob(f.full_path)):
                            src = mozpath.normsep(matched)
                            dst = mozpath.join(
                                self._topobjdir,
                                install_target,
                                subpath,
                                mozpath.basename(matched),
                            )
                            self._installs.append((src, dst))
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
            if not obj.installed:
                return True
            install_target = obj.install_target
            defines = {}
            if getattr(obj, "defines", None):
                defines = obj.defines.defines
            extra_deps = [mozpath.normsep(d.full_path) for d in obj.extra_deps]
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
                    self._pp_installs.append((src, dst, defines, extra_deps))
        elif isinstance(obj, FinalTargetFiles):
            install_target = obj.install_target
            is_test = install_target.startswith("_tests")
            if not install_target:
                # OBJDIR_FILES: copies land at topobjdir root, no manifest.
                for subpath, files in obj.files.walk():
                    for f in files:
                        src = mozpath.normsep(f.full_path)
                        dst = mozpath.join(self._topobjdir, subpath, f.target_basename)
                        self._installs.append((src, dst))
                CommonBackend.consume_object(self, obj)
                return True
            # `DIST_INSTALL = False` clears `installed`, but the emitter only
            # permits it alongside TEST_HARNESS_FILES (every other file
            # variable raises), and those still install to `_tests/`. Skip
            # only non-test files that opt out of installation.
            if not obj.installed and not is_test:
                return True
            base = mozpath.basedir(
                install_target,
                ("dist/bin", "dist/xpi-stage", "_tests", "dist/include"),
            )
            if not base:
                raise Exception("Cannot install to " + install_target)
            # Source-tree EXPORTS aren't interesting to artifact builds.
            skip_manifest = (
                base == "dist/include" and self.environment.is_artifact_build
            )
            manifest_key = base.replace("/", "_")
            install_manifest = self._install_manifests[manifest_key]
            reltarget = mozpath.relpath(install_target, base)
            for subpath, files in obj.files.walk():
                full_dest_dir = mozpath.join(install_target, subpath)
                manifest_dest_dir = mozpath.join(reltarget, subpath)
                for f in files:
                    full_dest_file = mozpath.join(full_dest_dir, f.target_basename)
                    manifest_dest_file = mozpath.join(
                        manifest_dest_dir, f.target_basename
                    )
                    if isinstance(f, ObjDirPath):
                        src = mozpath.normsep(f.full_path)
                        dst = mozpath.join(self._topobjdir, full_dest_file)
                        self._installs.append((src, dst))
                        if is_test:
                            self._test_harness_entries.append((
                                "optional",
                                full_dest_file,
                            ))
                        if not skip_manifest:
                            install_manifest.add_optional_exists(manifest_dest_file)
                    elif isinstance(f, AbsolutePath):
                        if not f.full_path.lower().endswith((
                            ".dll",
                            ".pdb",
                            ".so",
                            ".dylib",
                        )):
                            raise Exception(
                                "Absolute paths installed to FINAL_TARGET_FILES"
                                " must only be shared libraries or associated"
                                " debug information."
                            )
                        src = mozpath.normsep(f.full_path)
                        dst = mozpath.join(self._topobjdir, full_dest_file)
                        self._installs.append((src, dst))
                        if is_test:
                            self._test_harness_entries.append((
                                "optional",
                                full_dest_file,
                            ))
                        if not skip_manifest:
                            install_manifest.add_optional_exists(manifest_dest_file)
                    elif "*" in f:
                        # Wildcard SourcePath. Mozmake distinguishes
                        # topsrcdir-absolute (leading `/`) from srcdir-
                        # relative for the pattern base; mirror that.
                        if f.startswith("/"):
                            basepath, pattern = os.path.split(f.full_path)
                            if "*" in basepath:
                                raise Exception(
                                    "Wildcards are only supported in the"
                                    " filename part of srcdir-relative or"
                                    " absolute paths."
                                )
                            if is_test:
                                self._test_harness_entries.append((
                                    "pattern",
                                    basepath,
                                    pattern,
                                    full_dest_dir,
                                ))
                            if not skip_manifest:
                                install_manifest.add_pattern_link(
                                    basepath, pattern, manifest_dest_dir
                                )
                        else:
                            if is_test:
                                self._test_harness_entries.append((
                                    "pattern",
                                    f.srcdir,
                                    str(f),
                                    full_dest_dir,
                                ))
                            if not skip_manifest:
                                install_manifest.add_pattern_link(
                                    f.srcdir, f, manifest_dest_dir
                                )
                    else:
                        # Plain SourcePath → LINK from full path to dest.
                        if is_test:
                            self._test_harness_entries.append((
                                "link",
                                mozpath.normsep(f.full_path),
                                full_dest_file,
                            ))
                        if not skip_manifest:
                            install_manifest.add_link(f.full_path, manifest_dest_file)
        elif isinstance(obj, ChromeManifestEntry):
            self._chrome_manifest_entries[obj.path].add(str(obj.entry))
        elif isinstance(obj, TestManifest):
            self._test_manifests.append(obj)

        # Side effects from CommonBackend (writes Unified_cpp_*.cpp files,
        # tracks generated sources, etc).
        CommonBackend.consume_object(self, obj)

        return True

    def _collect_xpi_package(self, obj):
        """Stash an (xpi_path, source_dir) pair for a moz.build that
        sets ``XPI_PKGNAME``. Mirrors ``xpi_package_rule`` from
        ``config/rules.mk``: ``XPI_TESTDIR`` zips ``srcdir`` into
        ``$XPI_TESTDIR/<XPI_PKGNAME>.xpi``; otherwise zips the
        ``FINAL_TARGET`` stage dir into ``<parent>/<XPI_PKGNAME>.xpi``
        alongside it.
        """
        xpi_pkgname = obj.variables["XPI_PKGNAME"]
        xpi_testdir = obj.variables.get("XPI_TESTDIR")
        if xpi_testdir is not None:
            source_dir = mozpath.normsep(obj.srcdir)
            output_dir = mozpath.normsep(xpi_testdir.full_path)
        else:
            ctx = obj._context
            xpi_name = ctx.get("XPI_NAME")
            if not xpi_name:
                return
            source_dir = mozpath.join(self._topobjdir, "dist/xpi-stage", xpi_name)
            dist_subdir = ctx.get("DIST_SUBDIR")
            if dist_subdir:
                source_dir = mozpath.join(source_dir, dist_subdir)
            output_dir = mozpath.dirname(source_dir)
        self._xpi_packages.append((
            mozpath.join(output_dir, f"{xpi_pkgname}.xpi"),
            source_dir,
        ))

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
        include_dir = mozpath.join(self.environment.topobjdir, "dist", "include")
        for f in expected_build_output_files:
            if f.startswith(include_dir):
                self._install_manifests["dist_include"].add_optional_exists(
                    mozpath.relpath(f, include_dir)
                )

    def _handle_idl_manager(self, manager):
        # Required by CommonBackend.consume_finished. Stash the XPIDLManager
        # so `_emit_xpidl_statements` can iterate `manager.modules`.
        self._xpidl_manager = manager
        for stem in manager.idl_stems():
            self._install_manifests["dist_include"].add_optional_exists("%s.h" % stem)

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

        for p in self._rust_programs + self._host_rust_programs:
            if not p.installed:
                continue
            src = mozpath.normsep(mozpath.join(self._topobjdir, p.location))
            dst = self._rust_program_install_dest(p)
            if src != dst:
                self._installs.append((src, dst))

        for p in self._programs:
            if not getattr(p, "is_unit_test", False):
                continue
            src = mozpath.normsep(p.output_path.full_path)
            dst = mozpath.normsep(
                mozpath.join(self._topobjdir, "dist/cppunittests", p.program)
            )
            self._installs.append((src, dst))

        if self.environment.substs.get("MOZ_COPY_PDBS"):
            for p in self._programs:
                stem = mozpath.splitext(p.program)[0]
                src = mozpath.normsep(mozpath.join(p.objdir, stem + ".pdb"))
                dst = mozpath.normsep(
                    mozpath.join(self._topobjdir, p.install_target, stem + ".pdb")
                )
                self._installs.append((src, dst))
                if getattr(p, "is_unit_test", False):
                    dst_tests = mozpath.normsep(
                        mozpath.join(
                            self._topobjdir, "dist/cppunittests", stem + ".pdb"
                        )
                    )
                    self._installs.append((src, dst_tests))
            for lib in self._shared_libs:
                src = mozpath.normsep(mozpath.join(lib.objdir, lib.lib_name + ".pdb"))
                dst = mozpath.normsep(
                    mozpath.join(
                        self._topobjdir, lib.install_target, lib.lib_name + ".pdb"
                    )
                )
                self._installs.append((src, dst))

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
                    ninja_test_manifest.add_link(src, strip_tests(dest))
                elif kind == "pattern":
                    _, base, pattern, dest_dir = entry
                    ninja_test_manifest.add_pattern_link(
                        base, pattern, strip_tests(dest_dir)
                    )
                elif kind == "optional":
                    _, dest = entry
                    ninja_test_manifest.add_optional_exists(strip_tests(dest))
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
            with self._write_file(ninja_test_manifest_path) as fh:
                ninja_test_manifest.write(fileobj=fh)
            self._ninja_test_manifest_path = ninja_test_manifest_path
        else:
            self._ninja_test_manifest_path = None

        manifests_dir = mozpath.join(self._topobjdir, "_build_manifests/install")
        for name, manifest in self._install_manifests.items():
            manifest_path = mozpath.join(manifests_dir, name)
            ensureParentDir(manifest_path)
            with self._write_file(manifest_path) as fh:
                manifest.write(fileobj=fh)

        ninja_path = mozpath.join(self._topobjdir, "build.ninja")
        with self._write_file(ninja_path) as fh:
            self._write_ninja(fh)

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

    def _rel_n_path(self, p):
        """Emit a path for build.ninja, made topobjdir-relative when
        possible. Ninja runs from `$topobjdir` (via `ninja -C`), so a
        relative path resolves against that root. Topsrcdir paths
        stay absolute; mach's build-output filter is the right place
        to strip the workspace prefix for display."""
        p = mozpath.normsep(p)
        if p == self._topobjdir:
            return n_path(".")
        if p.startswith(self._topobjdir + "/"):
            return n_path(p[len(self._topobjdir) + 1 :])
        return n_path(p)

    def _rarg(self, flag):
        """Wrap `response_arg` with the target compiler's argv-parsing
        rules. clang-cl uses MSVC parsing for response files even when
        invoked on Linux/macOS as a cross-compiler, so quoting must
        track the target, not the host."""
        return _response_arg_raw(flag, msvc=self._target_msvc)

    def _emit_run_edge(
        self,
        writer,
        output,
        steps,
        description,
        inputs=None,
        implicit=None,
        order_only=None,
        stamp=False,
        pool=None,
    ):
        """Emit a single `run` edge driven by a JSON step spec.

        `output` is the edge's declared output (an absolute path): either a
        stamp the executor touches, or the file the final step writes.
        `steps` is the spec step list. `inputs` / `implicit` / `order_only`
        are already ninja-resolved strings (paths via `_rel_n_path`, or
        phony names verbatim). When `stamp` is True the executor touches
        `output` after the steps succeed; otherwise the final step writes it.
        """
        spec_path = output + ".runspec.json"
        spec = {"description": description, "steps": steps}
        if stamp:
            spec["stamp"] = mozpath.normsep(output)
        with self._write_file(spec_path) as fh:
            json.dump(spec, fh, indent=2, sort_keys=True)
        impl = [self._rel_n_path(spec_path)]
        if implicit:
            impl.extend(implicit)
        writer.build(
            self._rel_n_path(output),
            "run",
            inputs=list(inputs) if inputs else None,
            implicit=impl,
            order_only=list(order_only) if order_only else None,
            variables={"spec": self._rel_n_path(spec_path), "desc": description},
            pool=pool,
        )

    def _emit_edge_label(self, primary_output, description):
        """Write a `<output>.runspec.json` sidecar carrying only a human
        `description` for a multi-output codegen edge (IPDL/WebIDL/XPIDL).

        Reuses the same sidecar `_classify_ninja_edge` reads for run edges
        (via `_runspec_description`), so these edges get a meaningful
        marker name in the build profile instead of falling into the
        generic "Generated" bucket. Unlike `_emit_run_edge`'s spec this
        carries no `steps` and is not wired into the build graph -- it
        only labels the edge, and is rewritten on every build-backend."""
        spec_path = primary_output + ".runspec.json"
        with self._write_file(spec_path) as fh:
            json.dump({"description": description}, fh, indent=2, sort_keys=True)

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
            # Make's `$(LOCAL_INCLUDES)` expansion produces absolute
            # paths (via `$(topsrcdir)` / `$(topobjdir)`). Scripts that
            # consume `$(LOCAL_INCLUDES)` (e.g.
            # `preprocess_libffi_asm.py`) shlex-split this string and
            # pass the `-I` flags to a C preprocessor. Relative -I
            # paths break angled-include resolution under clang's MSVC
            # mode, so emit absolute paths to match make's behavior.
            parts = [
                f"-I{mozpath.normsep(li.path.full_path)}"
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

    # ---------------------------------------------------------------------
    # build.ninja emission
    # ---------------------------------------------------------------------

    def _write_ninja(self, fh):
        env = self.environment
        substs = env.substs
        self._target_msvc = substs.get("CC_TYPE") == "clang-cl"
        writer = NinjaWriter(fh)

        writer.comment("Auto-generated by NinjaBackend. Do not edit.")
        writer.variable("ninja_required_version", "1.13")
        writer.newline()

        writer.variable("topsrcdir", n_path(self._topsrcdir))
        writer.variable("topobjdir", n_path(self._topobjdir))

        seed_path = mozpath.join(self._topsrcdir, "build", ".ninja-seed-weights")
        if os.path.exists(seed_path):
            writer.variable("seed_edge_weights", n_path(seed_path))

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
            self._rarg(f) for f in self.environment.substs.get("HOST_CPPFLAGS", [])
        )
        nspr_cflags = " ".join(
            self._rarg(f) for f in self.environment.substs.get("NSPR_CFLAGS", [])
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
            ("MACH", mach),
            ("HOST_CPPFLAGS", host_cppflags),
            ("NSPR_CFLAGS", nspr_cflags),
        ):
            writer.variable(key, n_value(val))
        writer.newline()

        # Pools cap concurrent edges per category. `cargo` caps the number
        # of simultaneous cargo invocations so each gets jobserver elbow
        # room for its rustc children; `compile` caps native compiles to
        # leave headroom for cargo's children when both phases overlap.
        writer.pool("cargo", self._cargo_pool_depth)
        writer.newline()
        writer.pool("compile", self._compile_pool_depth)
        writer.newline()

        # Compile rules. The compiler reads its own response file via
        # `@$out.rsp` using MSVC argument-parsing rules (clang-cl matches
        # cl.exe here). response_arg below produces those rules, so the
        # quoted flags pass straight through with no shell involvement.
        # clang-cl depfile via -Xclang -dependency-file; we feed those
        # into ninja via deps = gcc + depfile = $out.d so ninja manages
        # implicit deps natively.
        # Compiles cd into the source's relobjdir before invoking the
        # compiler so the Mozilla clang plugin's `inThirdPartyPath` check
        # (which calls `make_absolute` on bare filenames from `#line`
        # directives — e.g. harfbuzz's `hb-ot-shaper-use-machine.rl` —
        # via process `getcwd()`) sees the same path it would see under
        # the recursive-make backend (CWD = relobjdir). With ninja's
        # default CWD = topobjdir, the resolved path doesn't contain the
        # third-party prefix, so the plugin treats third-party code as
        # in-tree and fires diagnostics that should be suppressed.
        # Paths in the rsp content are made absolute (via $topobjdir for
        # outputs, per-edge $src_abs for inputs) so they survive the cd.
        # `-MT $out` keeps the depfile target topobjdir-relative so ninja
        # matches it to the build edge's output.
        # On POSIX hosts, ninja invokes commands via `/bin/sh -c`, so
        # `cd && cmd` works natively. On Windows, ninja uses CreateProcess
        # directly — `cd` is a cmd.exe builtin, not an .exe — so we must
        # explicitly wrap in `cmd /c`.
        writer.variable("chdir", ".")
        if os.name == "nt":
            cd_pre = 'cmd /c "cd $chdir && '
            cd_post = '"'
        else:
            cd_pre = "cd $chdir && "
            cd_post = ""
        clang_depfile_args = (
            "-Xclang -MP -Xclang -dependency-file -Xclang $topobjdir/$out.d "
            "-Xclang -MT -Xclang $out"
        )
        writer.rule(
            "cxx",
            command=f"{cd_pre}$CXX @$topobjdir/$out.rsp{cd_post}",
            description="CXX $out",
            rspfile="$out.rsp",
            rspfile_content=f"$cxxflags {clang_depfile_args} -o $topobjdir/$out -c $src_abs",
            deps="gcc",
            depfile="$out.d",
            pool="compile",
        )
        writer.newline()
        writer.rule(
            "cc",
            command=f"{cd_pre}$CC @$topobjdir/$out.rsp{cd_post}",
            description="CC $out",
            rspfile="$out.rsp",
            rspfile_content=f"$cflags {clang_depfile_args} -o $topobjdir/$out -c $src_abs",
            deps="gcc",
            depfile="$out.d",
            pool="compile",
        )
        writer.newline()
        writer.rule(
            "host_cxx",
            command=f"{cd_pre}$HOST_CXX @$topobjdir/$out.rsp{cd_post}",
            description="HOST_CXX $out",
            rspfile="$out.rsp",
            rspfile_content=(
                f"$HOST_CPPFLAGS $host_cxxflags $NSPR_CFLAGS {clang_depfile_args} "
                "-o $topobjdir/$out -c $src_abs"
            ),
            deps="gcc",
            depfile="$out.d",
            pool="compile",
        )
        writer.newline()
        writer.rule(
            "host_cc",
            command=f"{cd_pre}$HOST_CC @$topobjdir/$out.rsp{cd_post}",
            description="HOST_CC $out",
            rspfile="$out.rsp",
            rspfile_content=(
                f"$HOST_CPPFLAGS $host_cflags $NSPR_CFLAGS {clang_depfile_args} "
                "-o $topobjdir/$out -c $src_abs"
            ),
            deps="gcc",
            depfile="$out.d",
            pool="compile",
        )
        writer.newline()
        # Assembly via clang-cl's integrated assembler (USE_INTEGRATED_CLANGCL_AS).
        writer.rule(
            "asm",
            command=f"{cd_pre}$CC @$topobjdir/$out.rsp{cd_post}",
            description="AS $out",
            rspfile="$out.rsp",
            rspfile_content=f"$asflags {clang_depfile_args} -o $topobjdir/$out -c $src_abs",
            deps="gcc",
            depfile="$out.d",
            pool="compile",
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
            pool="compile",
        )
        writer.newline()

        # Archive / link rules: the rspfile holds the linker input list so
        # it can be arbitrarily long (js_static.lib archives ~700 objs).
        is_clang_cl = self.environment.substs.get("CC_TYPE") == "clang-cl"
        if is_clang_cl:
            archive_cmd = "$AR -nologo -out:$out @$out.rsp"
            link_shared_cmd = (
                "$LINKER -NOLOGO -DLL -OUT:$out $ldflags "
                "-PDB:$pdbfile -IMPLIB:$implib @$out.rsp"
            )
            link_exe_cmd = (
                "$LINKER -NOLOGO -OUT:$out $ldflags "
                "-PDB:$pdbfile -IMPLIB:$implib @$out.rsp"
            )
        else:
            archive_cmd = "$AR crs $out @$out.rsp"
            link_shared_cmd = "$CXX -shared -o $out $ldflags @$out.rsp"
            link_exe_cmd = "$CXX -o $out $ldflags @$out.rsp"
        writer.rule(
            "archive",
            command=archive_cmd,
            description="AR $out",
            rspfile="$out.rsp",
            rspfile_content="$in",
        )
        writer.newline()
        writer.rule(
            "link_shared",
            command=link_shared_cmd,
            description="LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()
        writer.rule(
            "link_exe",
            command=link_exe_cmd,
            description="LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()

        if self.environment.substs.get("HOST_CC_TYPE") == "clang-cl":
            host_link_exe_cmd = (
                "$HOST_LINKER -NOLOGO -OUT:$out $ldflags "
                "-PDB:$pdbfile -IMPLIB:$implib @$out.rsp"
            )
        else:
            host_link_exe_cmd = "$HOST_CXX -o $out $ldflags @$out.rsp"
        writer.rule(
            "host_link_exe",
            command=host_link_exe_cmd,
            description="HOST_LINK $out",
            rspfile="$out.rsp",
            rspfile_content="$in $libs",
        )
        writer.newline()

        # Resource file generation/compilation. `gen_rc` invokes
        # `config/create_rc.py` to synthesize `<binary>.rc` from
        # version info + optional RCINCLUDE; `compile_rc` runs
        # `config/create_res.py` to produce a `.res` (calls
        # llvm-rc/windres). `--srcdir` and `-o` are passed explicitly
        # so the script doesn't need to chdir (ninja on Windows
        # invokes commands via CreateProcess, not cmd.exe, so shell
        # `cd` chains aren't usable here).
        writer.rule(
            "gen_rc",
            command="$PYTHON $topsrcdir/config/create_rc.py --srcdir $srcdir -o $out $binary $rcinclude_arg",
            description="GEN_RC $out",
            restat=True,
        )
        writer.newline()
        writer.rule(
            "compile_rc",
            command="$PYTHON $topsrcdir/config/create_res.py $defines $includes -o $out $in",
            description="RC $out",
            restat=True,
        )
        writer.newline()

        # Generic step executor. Edges that must run a sequence of actions
        # (and optionally touch a stamp) emit a `run` edge whose `$spec`
        # JSON describes the steps; `mozbuild.action.ninja_run` runs them as
        # subprocesses in order. One direct command, so no shell is required
        # (ninja provides none on Windows). Used for jar.mn, l10n, cargo
        # tests, and the post-link validators (check_binary / strip /
        # autowinchecksec), each of which folds its `toolchain_stamp` step in.
        writer.rule(
            "run",
            command="$PYTHON -m mozbuild.action.ninja_run --spec $spec",
            description="$desc",
            restat=True,
        )
        writer.newline()
        # dumpsymbols runs the crashreporter symbol extraction per
        # rules.mk:617-655 (action writes its own tracking_file stamp).
        writer.rule(
            "dumpsymbols",
            command="$PYTHON -m mozbuild.action.dumpsymbols $in $out $dump_symbols_flags",
            description="DUMP_SYMS $in",
            restat=True,
        )
        writer.newline()
        if self.environment.substs.get("HOST_CC_TYPE") == "clang-cl":
            host_link_shared_cmd = (
                "$HOST_LINKER -NOLOGO -DLL -OUT:$out $ldflags @$out.rsp"
            )
        else:
            host_link_shared_cmd = "$HOST_CXX -o $out $ldflags @$out.rsp"
        writer.rule(
            "host_link_shared",
            command=host_link_shared_cmd,
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
            pool="compile",
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
            pool="compile",
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

        # toolchain_stamp: invalidate compile outputs when toolchain
        # binaries change (e.g. bootstrap installs a new clang at the
        # same path). Without this, ninja's command-hash check sees
        # identical command lines and keeps stale .obj files compiled
        # against the old binary. Cargo handles this for rust crates
        # via its fingerprint; this is the C/C++/asm equivalent.
        # `restat=True`: the stamp's mtime only updates when its
        # content differs, so identical-toolchain rebuilds don't
        # invalidate downstream.
        writer.rule(
            "toolchain_stamp",
            command="$PYTHON -m mozbuild.action.toolchain_stamp $out $in",
            description="STAMP $out",
            restat=True,
        )
        writer.newline()

        # process_install_manifest: delegates to mozmake's install_manifest
        # driver so ninja gets identical pattern/wildcard handling as the
        # recursive-make backend. One invocation covers a whole install
        # target (e.g. dist/include ~600 entries) in one Python process.
        writer.rule(
            "run_install_manifest",
            command="$PYTHON -m mozbuild.action.process_install_manifest --no-symlinks --track $track $install_dir $manifest",
            description="INSTALL $install_dir",
            restat=True,
        )
        writer.newline()
        # Single-file install via hardlink (fallback to copy). Kept for
        # any edge that doesn't batch well.
        writer.rule(
            "install_file",
            command="$PYTHON -m mozbuild.action.install_objdir_file --hardlink $in $out",
            description="INSTALL $out",
            restat=True,
        )
        writer.newline()
        # Batch install (tab-separated src->dst manifest). Used for
        # generated-file EXPORTS installs to amortize the Python startup
        # cost across many files.
        writer.rule(
            "install_batch",
            command="$PYTHON -m mozbuild.action.install_objdir_file --hardlink $manifest",
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

        # XPI_PKGNAME packaging: mirror `xpi_package_rule` from
        # `config/rules.mk`. `mozbuild.action.zip` writes a gcc-style
        # depfile so incremental rebuilds track the actual source files
        # (the implicit install-track dep handles the cold case where
        # the depfile doesn't exist yet). The output path is passed
        # absolute via ``$xpi_out`` because zip.py joins ``-C`` with
        # its positional arg; ``$out`` (the topobjdir-relative form)
        # stays as the ``--dep-target`` so ninja's gcc-deps parser
        # matches what the build edge declares.
        writer.rule(
            "zip_xpi",
            command=(
                "$PYTHON -m mozbuild.action.zip --dep-target $out "
                "--dep-file $depfile -C $stage_dir $xpi_out '*'"
            ),
            description="ZIP $out",
            deps="gcc",
            depfile="$depfile",
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
                "$PYTHON -m mozbuild.action.pygen_runner $topobjdir $depfile "
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
            "xpidl_module",
            command=(
                "$PYTHON $script "
                "--depsdir $deps_dir "
                "--bindings-conf $bindings_conf "
                "$include_args "
                "$header_dir $xpcrs_dir $xpt_dir "
                "$module $idl_files"
            ),
            description="XPIDL $module",
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

        writer.rule(
            "package_langpack",
            command=(
                "$PYTHON -m mozbuild.action.package_langpack "
                "--locale=$locale --xpi-stage=$xpi_stage "
                "--metadata=$metadata --output=$out "
                "--eid=$eid --app-version=$app_version "
                "--max-app-ver=$max_app_ver --app-name=$app_name "
                "--l10n-basedir=$l10n_basedir $include_args"
            ),
            description="LANGPACK $locale",
            restat=True,
        )
        writer.newline()
        writer.rule(
            "l10n_repackage",
            command=(
                "$PYTHON -m mozbuild.action.l10n_repackage "
                "--locale=$locale --mach=$mach --make=$MAKE "
                "--l10n-stage=$l10n_stage --unpack-distdir=$unpack_distdir "
                "--stagedist=$stagedist --xpi-stage=$xpi_stage "
                "--pkg-dir=$pkg_dir --pkg-format=$pkg_format "
                "--pkg-filename=$pkg_filename --tar=$TAR --output=$out "
                "--moz-widget-toolkit=$moz_widget_toolkit "
                "--os-arch=$os_arch $winnt_args "
                "$non_resource_args $minify_arg $package_extra_args"
            ),
            description="REPACKAGE $locale",
            restat=True,
        )
        writer.newline()

        # Cargo build via mozbuild.action.cargo_build. The action loads
        # `$spec` (a per-RustLibrary JSON file emitted alongside this
        # rule), composes the cargo argv + env, and execs cargo. Output
        # cargo-timing-*.html files are post-processed by
        # `_record_ninja_log_markers` to emit per-crate markers against
        # the edge's start time from `.ninja_log`.
        writer.rule(
            "cargo_build",
            command="$PYTHON -m mozbuild.action.cargo_build --spec $spec",
            description="CARGO $out",
            depfile="$depfile",
            deps="gcc",
            restat=True,
            pool="cargo",
        )
        writer.newline()

        # Explicit per-crate rustc edges (experimental, gated by
        # MOZ_NINJA_EXPERIMENTAL_EXPLICIT_RUSTC_EDGES). rustc_build composes the
        # rustc argv from a per-unit $spec (folding in build-script output) and
        # execs rustc; run_build_script runs a compiled build script and
        # captures its `cargo:` directives for the dependent crate's edge. No
        # `cargo` pool: these are meant to parallelize like ordinary compiles.
        writer.rule(
            "rustc_build",
            command="$PYTHON -m mozbuild.action.rustc_build --spec $spec",
            description="RUSTC $out",
            depfile="$depfile",
            deps="gcc",
            restat=True,
        )
        writer.newline()
        writer.rule(
            "run_rust_build_script",
            command="$PYTHON -m mozbuild.action.run_rust_build_script --spec $spec",
            description="BUILD-SCRIPT $out",
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

        # Toolchain stamp first — its path is folded into the base
        # codegen category so every compile order-only's on it.
        self._emit_toolchain_stamp(writer)

        # Install manifests first — compile rules read self._install_tracks
        # to know which install targets must land before compile can start.
        self._emit_install_statements(writer)

        # XPI_PKGNAME packaging. Runs after install so the
        # dist/xpi-stage track is available to depend on.
        self._emit_xpi_package_statements(writer)

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

        # Per-locale l10n phonies (merge-X, l10n-X, chrome-X,
        # package-langpack-X, repackage-zip-X, installers-X).
        self._emit_l10n_statements(writer)

        # Emit compile build statements.
        self._emit_compile_statements(writer)

        # Rust libraries are built by delegating to mozmake, which already
        # knows how to invoke cargo with the right env.
        self._emit_rust_statements(writer)
        self._emit_rust_program_statements(writer)
        self._emit_rust_tests_statements(writer)
        self._persist_rust_outputs_manifest()

        # Resource (.rc -> .res) edges for Windows linkables. Returns a
        # dict {linkable.output_path -> .res path} consumed by the
        # program/shared link emitters to add .res as a link input.
        self._res_files = self._emit_resource_statements(writer)

        # nsinstall (config/Makefile.in special case): non-Windows hosts
        # copy `dist/host/bin/nsinstall_real` to `config/nsinstall` and
        # install it to `dist/bin/nsinstall`. Returned paths get added
        # to the `binaries` phony alongside other build outputs.
        nsinstall_outputs = self._emit_nsinstall_edges(writer)

        # Emit link build statements for static libs, shared libs, and programs.
        self._emit_archive_statements(writer)
        self._emit_shared_link_statements(writer)
        self._emit_program_statements(writer)
        self._emit_host_archive_statements(writer)
        self._emit_host_program_statements(writer)
        self._emit_host_shared_link_statements(writer)

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
            "$topobjdir/build.ninja",
            "regenerator",
            inputs=[self._rel_n_path(f) for f in regen_inputs],
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
        binary_outputs = [self._rel_n_path(o) for o in nsinstall_outputs]
        category_outputs = defaultdict(list)
        for p in self._programs + self._host_programs + self._shared_libs:
            out_norm = mozpath.normsep(p.output_path.full_path)
            targets = [self._rel_n_path(out_norm)]
            targets.extend(
                self._rel_n_path(s) for s in self._post_link_stamps.get(out_norm, ())
            )
            cat = getattr(p, "output_category", None)
            if cat:
                category_outputs[cat].extend(targets)
            else:
                binary_outputs.extend(targets)
        for p in self._rust_programs + self._host_rust_programs:
            if p.installed:
                out_norm = self._rust_program_install_dest(p)
            else:
                out_norm = mozpath.normsep(p.location)
            targets = [self._rel_n_path(out_norm)]
            cat = getattr(p, "output_category", None)
            if cat:
                category_outputs[cat].extend(targets)
            else:
                binary_outputs.extend(targets)

        binary_outputs += [self._rel_n_path(s) for s in self._rust_netcheck_stamps]

        install_outputs = [
            self._rel_n_path(track)
            for track in getattr(self, "_install_tracks", {}).values()
        ]
        install_outputs += [
            self._rel_n_path(o)
            for o in getattr(self, "_install_batch_post_outputs", ())
        ]
        install_outputs += [
            self._rel_n_path(o) for o in getattr(self, "_pp_install_outputs", ())
        ]
        install_outputs += [
            self._rel_n_path(s) for s in getattr(self, "_jar_maker_stamps", ())
        ]
        install_outputs += [
            self._rel_n_path(o) for o in getattr(self, "_xpi_outputs", ())
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
        # `syms` phony: dumpsymbols + autowinchecksec stamps. Mirrors
        # make's `syms::` target (rules.mk:654) — only built on
        # explicit `ninja syms`, NOT folded into `all` (default build).
        if self._syms_stamps:
            writer.newline()
            writer.build(
                "syms",
                "phony",
                inputs=[self._rel_n_path(s) for s in self._syms_stamps],
            )
        # Per-category phonies for non-default targets (gtest, etc.).
        # Not added to `all` — only built when explicitly requested.
        for cat, outs in sorted(category_outputs.items()):
            writer.newline()
            writer.build(cat, "phony", inputs=outs)
        if all_groups:
            writer.newline()
            writer.build("all", "phony", inputs=all_groups)
            writer.default(["all"])

    def _lib_output_path(self, lib):
        """Return the on-disk path of a library's output .lib / .dll.

        RustLibrary's output lives under the cargo target directory
        (`$objdir/$triple/release/jsrust.lib`), not in its `objdir`, so
        we go through `import_path` for those."""
        if isinstance(lib, RustLibrary):
            return mozpath.normsep(lib.import_path.full_path)
        if isinstance(lib, SharedLibrary):
            if self.environment.substs.get("CC_TYPE") == "clang-cl":
                return mozpath.join(
                    lib.objdir, getattr(lib, "import_name", lib.lib_name)
                )
            return mozpath.normsep(lib.output_path.full_path)
        return mozpath.join(lib.objdir, lib.lib_name or lib.basename)
