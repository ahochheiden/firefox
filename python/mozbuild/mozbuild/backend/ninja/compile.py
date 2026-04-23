# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
from collections import defaultdict

import mozpack.path as mozpath
from mozfile import json

from mozbuild.backend.ninja_syntax import value as n_value


class CompileMixin:
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

        # Pre-compile generated-file fence. Every compile order-only's on
        # the union of these category phonies. Splitting by category is
        # structural — a follow-up commit narrows per-relobjdir to only
        # the categories actually consumed.
        #
        # Two variants per category: the full set, and a host-safe subset
        # that excludes entries whose producers transitively depend on a
        # host program. The host-safe split avoids the host_compile →
        # host_link → host-program output → category → host_compile cycle
        # (canonical case: wasm2c, when wasm support lands on top of this
        # commit).
        #
        # Only files marked `required_before_compile` / `required_during_compile`
        # join the codegen category — post-link generators (like
        # spidermonkey_checks) would otherwise cycle through the static
        # library.
        categories = {
            "base-exports": {"all": [], "host": []},
            "base-core": {"all": [], "host": []},
            "base-generated": {"all": [], "host": []},
            "xpidl": {"all": [], "host": []},
            "ipdl": {"all": [], "host": []},
            "webidl": {"all": [], "host": []},
            "codegen": {"all": [], "host": []},
        }

        host_program_outputs = {
            mozpath.normsep(p.output_path.full_path) for p in self._host_programs
        }

        def _depends_on_host_program(g):
            for inp in g.inputs:
                if mozpath.normsep(inp.full_path) in host_program_outputs:
                    return True
            return False

        # headers-base: dist/include must be fully populated before
        # compiles can resolve `-I dist/include` references. Three sources
        # land there: the source-tree EXPORTS install-manifest track,
        # ObjDirPath EXPORTS (`_installs`, generated files copied to
        # dist/include), and preprocessed OBJDIR_PP_FILES (`_pp_installs`)
        # whose dst is under dist/include.
        track = getattr(self, "_install_tracks", {}).get("dist_include")
        if track:
            categories["base-exports"]["all"].append(track)
            categories["base-exports"]["host"].append(track)
        clang_plugin = self.environment.substs.get("CLANG_PLUGIN")
        if clang_plugin:
            categories["base-core"]["all"].append(mozpath.normsep(clang_plugin))

        topobjdir_prefix = self._topobjdir + "/"
        force_include_paths = set()
        for relobjdir in self._computed_flags:
            for var in ("CFLAGS", "CXXFLAGS", "HOST_CFLAGS", "HOST_CXXFLAGS"):
                flags = self._computed_flag_list(relobjdir, var)
                i = 0
                while i < len(flags):
                    f = flags[i]
                    if f in ("-FI", "-include") and i + 1 < len(flags):
                        force_include_paths.add(mozpath.normsep(flags[i + 1]))
                        i += 2
                    elif f.startswith("-FI"):
                        force_include_paths.add(mozpath.normsep(f[3:]))
                        i += 1
                    else:
                        i += 1
        for path in sorted(force_include_paths):
            if not path.startswith(topobjdir_prefix):
                continue
            categories["base-core"]["all"].append(path)
            categories["base-core"]["host"].append(path)
        dist_include_prefix = mozpath.join(self._topobjdir, "dist/include") + "/"
        # `rust_prereqs` is a narrower subset for the `cargo_build`
        # edge to fence behind. Cargo needs the cargo config and early
        # configure-define files, and it should not consume job slots
        # before the cbindgen export path has completed.
        rust_prereqs = []
        rust_prereqs_seen = set()

        def add_rust_prereq(path):
            if path and path not in rust_prereqs_seen:
                rust_prereqs_seen.add(path)
                rust_prereqs.append(path)

        if track:
            add_rust_prereq(track)
        for _, dst in self._installs:
            if dst.startswith(dist_include_prefix):
                categories["base-generated"]["all"].append(dst)
                categories["base-generated"]["host"].append(dst)
                add_rust_prereq(dst)
        cargo_config = mozpath.join(self._topobjdir, ".cargo/config.toml")
        for _, dst, _, _ in self._pp_installs:
            if dst.startswith(dist_include_prefix):
                categories["base-core"]["all"].append(dst)
                categories["base-core"]["host"].append(dst)
                add_rust_prereq(dst)
            if dst == cargo_config:
                add_rust_prereq(dst)
        # `required_before_export` GeneratedFiles (e.g. mozilla-config.h,
        # source-repo.h, buildid.h) are read directly from the objdir
        # by some build.rs scripts before they're installed. `.rs`
        # outputs from any GeneratedFile are added too: cargo
        # `include!()`s them at compile time (e.g. mozbuild's
        # buildconfig.rs, fog's metrics.rs/pings.rs/factory.rs).
        for g in self._generated_files:
            if not g.script:
                continue
            is_define_file = g.method == "process_define_file"
            include_all = g.required_before_export or is_define_file
            has_rs = any(
                (o if isinstance(o, str) else o.full_path).endswith(".rs")
                for o in g.outputs
            )
            if not include_all and not has_rs:
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
            if declared:
                for output in self._expand_num_outputs_outputs(
                    declared[0], declared, g.flags or ()
                ):
                    if include_all or output.endswith(".rs"):
                        add_rust_prereq(output)
                    categories["base-core"]["all"].append(output)
                    categories["base-core"]["host"].append(output)

        # xpcom/rust/xpcom `include!()`s every xpidl-produced .rs via
        # `dist/xpcrs/{rt,bt}/all.rs`. Without this gate, a cold cargo
        # run would race xpidl and fail to find the per-stem includes.
        for output in self._xpidl_rust_outputs:
            add_rust_prereq(output)

        # IPDL, WebIDL, XPIDL codegen is pure-Python; outputs are
        # uniformly host-safe.
        categories["xpidl"]["all"].extend(self._xpidl_outputs)
        categories["xpidl"]["host"].extend(self._xpidl_outputs)
        categories["ipdl"]["all"].extend(self._ipdl_outputs)
        categories["ipdl"]["host"].extend(self._ipdl_outputs)
        categories["webidl"]["all"].extend(self._webidl_outputs)
        categories["webidl"]["host"].extend(self._webidl_outputs)
        # WebIDL writes directly to dist/include/mozilla/dom/*Binding.h.
        # Anything reaching dist/include is in the global compile fence,
        # so every compile must wait, not just dom/bindings consumers.
        for o in self._webidl_outputs:
            if o.startswith(dist_include_prefix):
                categories["base-generated"]["all"].append(o)
                categories["base-generated"]["host"].append(o)

        # headers-codegen split by output location:
        #
        #   * global   — outputs in dist/include. Reachable via
        #     `-I dist/include` from anywhere; gates every compile.
        #   * local    — outputs that stay in the generator's relobjdir
        #     tree. Only attributed to that relobjdir + relobjdirs whose
        #     LOCAL_INCLUDES reach the generator's objdir.
        #
        # Source-style outputs (.c/.cpp/.cc/.m/.mm/.s/.S/.asm) are
        # excluded entirely from both buckets: those land in some
        # compile edge's `inputs=` (as a regular dep, via SOURCES with a
        # `!` prefix), so ninja already gates the consuming compile on
        # the file's existence. Including them in any codegen aggregate
        # would force unrelated relobjdirs to wait for the entire
        # generator chain (canonical case: wasm2c — rlbox.wasm.c only
        # matters to lgpllibs but folding it in would block every
        # compile in the tree behind host_wabt → wasm2c).
        #
        # Cross-relobjdir consumers reached via relative `#include
        # "../foo/X.h"` (no configured include path) won't be detected
        # by the LOCAL_INCLUDES scan. Such a consumer needs an explicit
        # `LOCAL_INCLUDES = ["!/path/to/generator/dir"]` entry in its
        # moz.build to opt back into the prereq, or the GeneratedFile
        # should publish its output via `EXPORTS` so it lands in
        # dist/include and joins the global aggregate.
        #
        # Per-GF host-safety filter (no-op until wasm2c lands on top).
        SOURCE_EXTS = (
            ".c",
            ".cpp",
            ".cc",
            ".cxx",
            ".C",
            ".m",
            ".mm",
            ".s",
            ".S",
            ".asm",
        )
        codegen_local_all = defaultdict(list)
        codegen_local_host = defaultdict(list)
        gen_relobjdir_to_objdir = {}
        global_include_prefixes = (
            dist_include_prefix,
            mozpath.join(self._topobjdir, "dist/stl_wrappers") + "/",
            mozpath.join(self._topobjdir, "dist/system_wrappers") + "/",
        )
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
            header_outs = [o for o in outs if not o.endswith(SOURCE_EXTS)]
            host_safe = not _depends_on_host_program(g)
            g_relobjdir = mozpath.relpath(g.objdir, self._topobjdir)
            gen_relobjdir_to_objdir[g_relobjdir] = mozpath.normsep(g.objdir)

            def _out_dir(o):
                return mozpath.relpath(mozpath.dirname(o), self._topobjdir)

            for o in header_outs:
                if o.startswith(global_include_prefixes):
                    categories["codegen"]["all"].append(o)
                    if host_safe:
                        categories["codegen"]["host"].append(o)
                else:
                    out_relobjdir = _out_dir(o)
                    codegen_local_all[out_relobjdir].append(o)
                    if host_safe:
                        codegen_local_host[out_relobjdir].append(o)
            # Source-style outputs are excluded from the global codegen
            # aggregate (their consuming compile lists them in `inputs=`
            # already, so the global fence would just over-couple), but
            # we still record them in codegen_local so a consumer that
            # LOCAL_INCLUDES the generator's directory waits for the
            # generator edge to complete. Some GeneratedFile actions
            # write side-effect outputs that aren't formally declared
            # (canonical case: `wasm2c` emits `<base>.wasm.h` alongside
            # the declared `<base>.wasm.c`); a consumer that #includes
            # the side-effect header has no input-dep path to that
            # edge, so it must reach it through codegen_local.
            source_outs = [o for o in outs if o.endswith(SOURCE_EXTS)]
            for o in source_outs:
                if o.startswith(dist_include_prefix):
                    continue
                out_relobjdir = _out_dir(o)
                codegen_local_all[out_relobjdir].append(o)
                if host_safe:
                    codegen_local_host[out_relobjdir].append(o)

        # Persist for the per-relobjdir attribution pass (next commit).
        self._prereq_categories = categories

        for name, buckets in categories.items():
            writer.build(
                f".ninja-headers-{name}",
                "phony",
                inputs=[self._rel_n_path(o) for o in buckets["all"]]
                if buckets["all"]
                else None,
            )
            writer.build(
                f".ninja-headers-{name}-host",
                "phony",
                inputs=(
                    [self._rel_n_path(o) for o in buckets["host"]]
                    if buckets["host"]
                    else None
                ),
            )

        # Per-generator local codegen phonies. One phony per relobjdir
        # that contains any local-codegen output. Consumers attribute
        # to these in the per-relobjdir prereq pass below.
        for gen_relobjdir, outs in sorted(codegen_local_all.items()):
            writer.build(
                f".ninja-headers-codegen-local/{gen_relobjdir}",
                "phony",
                inputs=[self._rel_n_path(o) for o in outs],
            )
        for gen_relobjdir, outs in sorted(codegen_local_host.items()):
            writer.build(
                f".ninja-headers-codegen-local-host/{gen_relobjdir}",
                "phony",
                inputs=[self._rel_n_path(o) for o in outs],
            )

        # Union aliases. Compile edges use per-relobjdir phonies emitted
        # below; cargo edges use `.ninja-rust-prereqs` (see
        # `_emit_rust_statements`). The aliases stay as a coarse-grained
        # entry point for external consumers.
        writer.build(
            ".ninja-generated",
            "phony",
            inputs=[f".ninja-headers-{name}" for name in categories],
        )
        writer.build(
            ".ninja-generated-host",
            "phony",
            inputs=[f".ninja-headers-{name}-host" for name in categories],
        )
        writer.build(
            ".ninja-rust-prereqs",
            "phony",
            inputs=[self._rel_n_path(o) for o in rust_prereqs]
            if rust_prereqs
            else None,
        )
        writer.newline()

        # Per-relobjdir prereq phonies. Each compile order-only's on
        # its relobjdir's phony, which aggregates only the categories
        # that relobjdir's context actually consumes.
        #
        # Attribution: always include base+xpidl+codegen; add ipdl /
        # webidl per consumer detection below. The detection is
        # producer-centric (catches contexts that compile gen .cpp's
        # or LOCAL_INCLUDE the gen dir); header-only consumers via
        # dist/include are not detected and may need explicit
        # special-casing if they break on cold build.
        ipdl_root = mozpath.join(self._topobjdir, "ipc/ipdl")
        webidl_root = mozpath.join(self._topobjdir, "dom/bindings")
        ipdl_root_prefix = ipdl_root + "/"
        webidl_root_prefix = webidl_root + "/"

        ipdl_consumer_dirs = set()
        webidl_consumer_dirs = set()
        for buckets in (self._sources_by_dir, self._unified_by_dir):
            for reldir, sources_list in buckets.items():
                for sobj in sources_list:
                    for f in sobj.files:
                        norm = mozpath.normsep(f)
                        if norm.startswith(ipdl_root_prefix):
                            ipdl_consumer_dirs.add(reldir)
                        if norm.startswith(webidl_root_prefix):
                            webidl_consumer_dirs.add(reldir)
        for reldir, includes in self._local_includes_by_dir.items():
            for li in includes:
                full = mozpath.normsep(li.path.full_path)
                if full == ipdl_root or full.startswith(ipdl_root_prefix):
                    ipdl_consumer_dirs.add(reldir)
                if full == webidl_root or full.startswith(webidl_root_prefix):
                    webidl_consumer_dirs.add(reldir)

        # codegen_local consumers: which relobjdirs need each
        # generator's local codegen outputs as a prereq. Direct
        # consumer is the generator's own relobjdir. Indirect consumers
        # are anyone whose LOCAL_INCLUDES resolves to (or under) the
        # generator's directory.
        #
        # LOCAL_INCLUDES entries can be source-tree paths (`/foo/bar`)
        # or objdir paths (`!/foo/bar`). Mach auto-adds both `-I
        # srcdir/X` and `-I objdir/X` when a source-tree LOCAL_INCLUDES
        # is set, so generated headers in `objdir/X` are reachable from
        # any consumer that wrote either form. Match both: normalize
        # `li.path.full_path` against topsrcdir and topobjdir, treat the
        # relative remainder as a candidate relobjdir.
        #
        # Cross-module relative `#include "../foo/X.h"` paths that don't
        # go through LOCAL_INCLUDES will still not be detected and need
        # an explicit LOCAL_INCLUDES entry to opt back in.
        codegen_local_consumers = defaultdict(set)
        for gen_relobjdir in codegen_local_all:
            codegen_local_consumers[gen_relobjdir].add(gen_relobjdir)
        for gen_relobjdir in codegen_local_host:
            codegen_local_consumers[gen_relobjdir].add(gen_relobjdir)
        gen_relobjdirs = set(codegen_local_all) | set(codegen_local_host)
        topsrc_prefix = mozpath.normsep(self._topsrcdir) + "/"
        topobj_prefix = mozpath.normsep(self._topobjdir) + "/"
        for reldir, includes in self._local_includes_by_dir.items():
            for li in includes:
                full = mozpath.normsep(li.path.full_path)
                candidate = None
                if full.startswith(topobj_prefix):
                    candidate = full[len(topobj_prefix) :]
                elif full.startswith(topsrc_prefix):
                    candidate = full[len(topsrc_prefix) :]
                if candidate is None:
                    continue
                for gen_relobjdir in gen_relobjdirs:
                    if (
                        candidate == gen_relobjdir
                        or candidate.startswith(gen_relobjdir + "/")
                        or gen_relobjdir.startswith(candidate + "/")
                    ):
                        codegen_local_consumers[gen_relobjdir].add(reldir)

        base_cats = (
            "base-exports",
            "base-core",
            "base-generated",
            "xpidl",
            "codegen",
        )

        def _categories_for(reldir):
            cats = list(base_cats)
            if reldir in ipdl_consumer_dirs:
                cats.append("ipdl")
            if reldir in webidl_consumer_dirs:
                cats.append("webidl")
            return cats

        compile_dirs = sorted(
            set(self._sources_by_dir)
            | set(self._unified_by_dir)
            | set(self._host_sources_by_dir)
        )

        # Implicit `-I <consumer objdir>` reaches generated outputs in
        # any subdirectory under the consumer's relobjdir. Attribute
        # those gen_relobjdirs to the consumer (canonical case: wabt's
        # `wabt/config.h` at <consumer>/wabt/config.h, included by
        # binary-reader-logging.cc compiled in the same consumer dir).
        for reldir in compile_dirs:
            for gen_relobjdir in gen_relobjdirs:
                if gen_relobjdir.startswith(reldir + "/"):
                    codegen_local_consumers[gen_relobjdir].add(reldir)

        attribution = {}
        for reldir in compile_dirs:
            cats = _categories_for(reldir)
            local_codegen = sorted(
                gen_relobjdir
                for gen_relobjdir, consumers in codegen_local_consumers.items()
                if reldir in consumers
            )
            attribution[reldir] = {
                "categories": cats,
                "codegen_local": local_codegen,
            }
            inputs = [f".ninja-headers-{c}" for c in cats]
            inputs_host = [f".ninja-headers-{c}-host" for c in cats]
            for gen_relobjdir in local_codegen:
                if codegen_local_all.get(gen_relobjdir):
                    inputs.append(f".ninja-headers-codegen-local/{gen_relobjdir}")
                if codegen_local_host.get(gen_relobjdir):
                    inputs_host.append(
                        f".ninja-headers-codegen-local-host/{gen_relobjdir}"
                    )
            writer.build(
                f".ninja-prereqs/{reldir}",
                "phony",
                inputs=inputs,
            )
            writer.build(
                f".ninja-prereqs-host/{reldir}",
                "phony",
                inputs=inputs_host,
            )
        writer.newline()

        attribution_path = mozpath.join(
            self._topobjdir, ".ninja-prereq-attribution.json"
        )
        with self._write_file(attribution_path) as fh:
            json.dump(attribution, fh, indent=2, sort_keys=True)

        # Flags are per-directory (ComputedFlags objects are emitted by
        # context), but a source's declaring directory may differ from the
        # Linkable's directory (e.g. js_static lives in js/src/build while
        # js/src/vm contributes its own compiled sources). We therefore
        # iterate every Sources/UnifiedSources/HostSources object — each
        # carries its own `relobjdir` and `objdir`, so we can resolve flags
        # and output paths from the source's own context.
        toolchain_stamp = getattr(self, "_toolchain_stamp_path", None)
        toolchain_implicit = (
            [self._rel_n_path(toolchain_stamp)] if toolchain_stamp else None
        )
        emitted_objs = set()
        obj_suffix = self.environment.substs.get("OBJ_SUFFIX", "obj")

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
                obj = mozpath.join(objdir, f"{obj_prefix}{basename_noext}.{obj_suffix}")
                if obj in emitted_objs:
                    continue
                emitted_objs.add(obj)
                src_norm = mozpath.normsep(src)
                extra = self._per_source_flags_for(relobjdir, src_norm)

                if ext in (".cpp", ".cc", ".cxx"):
                    rule_name = "host_cxx" if is_host else "cxx"
                    flag_var = "host_cxxflags" if is_host else "cxxflags"
                    flag_value = " ".join(self._rarg(f) for f in cxxflags + extra)
                elif ext == ".mm":
                    # CMMFLAGS / HOST_CMMFLAGS land in
                    # `passthru.variables["MOZBUILD_(HOST_)CMMFLAGS"]`
                    # (see frontend/emitter.py:1310), not ComputedFlags.
                    # `_computed_flag_list("CMMFLAGS")` returns empty;
                    # pull the per-dir passthru entry instead. Canonical
                    # case: dom/media/systemservices `CMMFLAGS +=
                    # ["-fobjc-arc"]` for the objc_video_capture .mm
                    # files on Darwin.
                    passthru = self._variable_passthru.get(relobjdir)
                    pv = passthru.variables if passthru else {}
                    mmkey = "MOZBUILD_HOST_CMMFLAGS" if is_host else "MOZBUILD_CMMFLAGS"
                    cmmflags = list(pv.get(mmkey, []))
                    rule_name = "host_cxx" if is_host else "cxx"
                    flag_var = "host_cxxflags" if is_host else "cxxflags"
                    flag_value = " ".join(
                        self._rarg(f) for f in cxxflags + cmmflags + extra
                    )
                elif ext == ".c":
                    rule_name = "host_cc" if is_host else "cc"
                    flag_var = "host_cflags" if is_host else "cflags"
                    flag_value = " ".join(self._rarg(f) for f in cflags + extra)
                elif ext == ".m":
                    # CMFLAGS / HOST_CMFLAGS, like CMMFLAGS, ride along
                    # in `passthru.variables["MOZBUILD_(HOST_)CMFLAGS"]`.
                    passthru = self._variable_passthru.get(relobjdir)
                    pv = passthru.variables if passthru else {}
                    mkey = "MOZBUILD_HOST_CMFLAGS" if is_host else "MOZBUILD_CMFLAGS"
                    cmflags = list(pv.get(mkey, []))
                    rule_name = "host_cc" if is_host else "cc"
                    flag_var = "host_cflags" if is_host else "cflags"
                    flag_value = " ".join(
                        self._rarg(f) for f in cflags + cmflags + extra
                    )
                elif ext in (".S", ".s"):
                    rule_name = "asm"
                    flag_var = "asflags"
                    flag_value = " ".join(self._rarg(f) for f in asflags + extra)
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
                    # When cross-compiling Windows from a non-Windows
                    # host (toolkit/moz.configure:3428-3438 sets WINE
                    # exactly in that case), MASM (`ml.exe` / `ml64.exe`)
                    # is a Windows PE binary that the host's `/bin/sh`
                    # cannot exec directly — `code=126 Exec format error`.
                    # Mirror `build/midl.py:150-152`: prefix with the
                    # configured wine when the assembler is a `.exe`.
                    wine = substs.get("WINE")
                    if (
                        wine
                        and isinstance(asm_program, str)
                        and asm_program.lower().endswith(".exe")
                    ):
                        asm_program = f"{wine} {asm_program}"
                    as_dash_c_flag = pv.get(
                        "AS_DASH_C_FLAG", substs.get("AS_DASH_C_FLAG", "-c")
                    )
                    asoutoption = pv.get(
                        "ASOUTOPTION", substs.get("ASOUTOPTION", "-o ")
                    )
                    flag_value = " ".join(self._rarg(f) for f in asflags_only + extra)
                    # `.asm` dispatch is target-side, never host.
                    order_only = f".ninja-prereqs/{relobjdir}"
                    # Cross-compiling Windows MASM under wine: MSVC tools
                    # treat `/X` arguments as switches, so a Unix
                    # absolute source path like
                    # `/builds/.../file.asm` is parsed as
                    # an option and `ml64.exe` errors "missing source
                    # filename". Mirror `config/rules.mk`'s `relativize`
                    # by forcing a topobjdir-relative path when WINE is
                    # set (ninja runs from topobjdir, so it resolves
                    # correctly).
                    if substs.get("WINE"):
                        asm_input = mozpath.relpath(src_norm, self._topobjdir)
                    else:
                        asm_input = self._rel_n_path(src_norm)
                    writer.build(
                        self._rel_n_path(obj),
                        "asm_native",
                        inputs=asm_input,
                        implicit=toolchain_implicit,
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
                # Host compiles use the host-safe variant of the
                # per-relobjdir phony, which excludes categories whose
                # producers transitively depend on a host program
                # (avoids host_obj → host_link → wasm2c output →
                # category → host_obj cycle).
                order_only = (
                    f".ninja-prereqs-host/{relobjdir}"
                    if is_host
                    else f".ninja-prereqs/{relobjdir}"
                )
                writer.build(
                    self._rel_n_path(obj),
                    rule_name,
                    inputs=self._rel_n_path(src_norm),
                    implicit=toolchain_implicit,
                    order_only=order_only,
                    variables={
                        flag_var: flag_value,
                        "chdir": self._rel_n_path(objdir),
                        "src_abs": n_value(mozpath.normsep(src_norm)),
                    },
                )

        # WebIDL-injected sources have no Sources/UnifiedSources object:
        # `emitter._link_libraries` prepends `WebIDLCollection.all_source_files()`
        # (GLOBAL_DEFINE_FILES + unified binding .cpp basenames) into the
        # dom/bindings linkable's `sources[".cpp"]`, so `lib.objs` references
        # them — but the iteration above never sees them. Emit `cxx` rules
        # here so xul.lib's link inputs resolve. The global-define .cpp files
        # are produced by the webidl codegen edge; the unified binding .cpp
        # files are written at backend-write time by `_write_unified_files`.
        if self._webidl:
            (
                bindings_dir,
                unified_source_mapping,
                _webidls,
                _expected_build_output_files,
                global_define_files,
            ) = self._webidl
            webidl_relobjdir = mozpath.relpath(bindings_dir, self._topobjdir)
            webidl_cxxflags = self._computed_flag_list(webidl_relobjdir, "CXXFLAGS")
            webidl_order_only = f".ninja-prereqs/{webidl_relobjdir}"
            webidl_srcs = list(global_define_files) + [
                u for u, _ in unified_source_mapping
            ]
            for basename in webidl_srcs:
                src = mozpath.join(bindings_dir, basename)
                basename_noext = mozpath.splitext(basename)[0]
                obj = mozpath.join(bindings_dir, f"{basename_noext}.{obj_suffix}")
                if obj in emitted_objs:
                    continue
                emitted_objs.add(obj)
                src_norm = mozpath.normsep(src)
                extra = self._per_source_flags_for(webidl_relobjdir, src_norm)
                flag_value = " ".join(self._rarg(f) for f in webidl_cxxflags + extra)
                writer.build(
                    self._rel_n_path(obj),
                    "cxx",
                    inputs=self._rel_n_path(src_norm),
                    implicit=toolchain_implicit,
                    order_only=webidl_order_only,
                    variables={
                        "cxxflags": flag_value,
                        "chdir": self._rel_n_path(bindings_dir),
                        "src_abs": n_value(src_norm),
                    },
                )

    def _emit_toolchain_stamp(self, writer):
        """Emit the toolchain identity stamp.

        Inputs are kept narrow on purpose: we declare the mozbuild
        toolchains tarball directory plus a handful of well-known
        compiler binaries, so ninja's per-build stat cost is a few
        cheap calls. The action expands the directory at run time and
        hashes every artifact filename — each tarball's name is
        prefixed with the TaskCluster artifact hash, so the listing
        is a complete version manifest of every toolchain bootstrap
        installed.

        When bootstrap installs a different artifact, the toolchains
        directory's mtime updates (NTFS/POSIX both update dir mtime
        on add/remove of children), the stamp action runs, the
        listing differs, the digest differs, the stamp file is
        rewritten, and every compile invalidates. When binaries change
        at the same path without the tarball set changing (rare —
        e.g., an in-place bootstrap re-extract), the binaries' own
        mtimes catch it.

        Cargo solves the equivalent for rust crates via its per-crate
        fingerprint; this is the C/C++/asm equivalent. Recursive-make
        currently has no equivalent — it relies on manual `CLOBBER`
        bumps when toolchain manifests change in TaskCluster.
        """
        from mach.util import get_state_dir

        substs = self.environment.substs
        # The substs values for CC/CXX/etc. include base flags
        # ("clang -fms-compatibility-version=19.50 -std:c++20"). The
        # first token is the binary path.
        keys = (
            "CC",
            "CXX",
            "HOST_CC",
            "HOST_CXX",
            "WASM_CC",
            "WASM_CXX",
            "AR",
            "LINKER",
            "HOST_LINKER",
            "RUSTC",
        )
        inputs = set()
        for key in keys:
            val = substs.get(key, "")
            if isinstance(val, list):
                val = " ".join(val)
            if not val:
                continue
            binary = val.split()[0]
            if os.path.exists(binary):
                inputs.add(mozpath.normsep(binary))

        # Mozbuild's toolchain artifact tarball directory: filenames
        # are prefixed with TaskCluster artifact hashes, so listing
        # the directory captures every toolchain version bootstrap
        # has installed in one cheap stat. Adding it as an input
        # makes ninja re-run the stamp action whenever bootstrap
        # adds/removes a tarball.
        try:
            state_dir = get_state_dir()
            toolchains_dir = mozpath.join(state_dir, "toolchains")
            if os.path.isdir(toolchains_dir):
                inputs.add(mozpath.normsep(toolchains_dir))
        except Exception:
            pass

        stamp_path = mozpath.join(self._topobjdir, ".toolchain-stamp")
        if not inputs:
            self._toolchain_stamp_path = None
            return
        writer.newline()
        writer.comment("------ toolchain identity stamp ------")
        writer.newline()
        writer.build(
            self._rel_n_path(stamp_path),
            "toolchain_stamp",
            inputs=[self._rel_n_path(p) for p in sorted(inputs)],
        )
        writer.newline()
        self._toolchain_stamp_path = stamp_path
