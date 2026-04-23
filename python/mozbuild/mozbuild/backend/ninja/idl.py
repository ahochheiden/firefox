# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import mozpack.path as mozpath


class IdlMixin:
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
        ipdlsrcs_txt = mozpath.join(ipdl_root, "ipdlsrcs.ninja.txt")
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
        defines_str = " ".join(self._rarg(d) for d in per_dir_defines)
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
                self._rel_n_path(out),
                "pp_install",
                inputs=self._rel_n_path(src),
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

        include_args = " ".join(f"-I{self._rel_n_path(d)}" for d in include_dirs)

        writer.build(
            [self._rel_n_path(o) for o in outputs],
            "ipdl",
            inputs=[self._rel_n_path(i) for i in inputs],
            implicit=[self._rel_n_path(p) for p in ipdl_py_deps],
            variables={
                "script": self._rel_n_path(ipdl_script),
                "sync_msg_list": self._rel_n_path(sync_msg_list),
                "msg_metadata": self._rel_n_path(msg_metadata),
                "headers_dir": self._rel_n_path(headers_dir),
                "cpp_dir": self._rel_n_path(ipdl_root),
                "file_list": self._rel_n_path(ipdlsrcs_txt),
                "include_args": include_args,
            },
        )
        self._emit_edge_label(outputs[0], "IPDL codegen")

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
        defines_str = " ".join(self._rarg(d) for d in per_dir_defines)
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
                self._rel_n_path(out),
                "pp_install",
                inputs=self._rel_n_path(src),
                variables={"defines": defines_str},
            )

        # Stash outputs for `_emit_compile_statements`.
        self._webidl_outputs = list(expected_build_output_files)

        # Inputs: file-lists.json (the canonical first-run trigger),
        # preprocessed outputs (now landed in bindings_dir), every
        # static webidl path, and the objdir paths for generated webidl
        # sources (GENERATED_WEBIDL_FILES like CSSCounterStyleRule.webidl,
        # which are GeneratedFile outputs landing in bindings_dir). The
        # static-source list comes from all_static_sources() — sources,
        # generated_events_sources, test_sources — but excludes
        # generated_sources, which we wire explicitly here so the webidl
        # edge waits for the GeneratedFile producers. file-lists.json
        # references all of these by their bindings_dir path.
        inputs = [file_lists_json]
        inputs.extend(preprocessed_outputs)
        inputs.extend(mozpath.normsep(s) for s in sorted(webidls.all_static_sources()))
        for src in sorted(webidls.generated_sources):
            inputs.append(mozpath.join(bindings_dir, mozpath.basename(src)))

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
            [self._rel_n_path(o) for o in self._webidl_outputs],
            "webidl",
            inputs=[self._rel_n_path(i) for i in inputs],
            implicit=[self._rel_n_path(p) for p in webidl_py_deps],
            variables={
                "depfile": self._rel_n_path(depfile),
            },
        )
        if self._webidl_outputs:
            self._emit_edge_label(self._webidl_outputs[0], "WebIDL bindings")

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
        all_idl_dirs = sorted({
            mozpath.dirname(idl)
            for m in manager.modules.values()
            for idl in m.idl_files
        })
        # `xpidl-process.py` joins each `-I` arg with topsrcdir before
        # use, so emit topsrcdir-relative paths (not topobjdir-relative)
        # to match what the script expects.
        include_args = " ".join(
            f"-I{mozpath.relpath(d, self._topsrcdir)}" for d in all_idl_dirs
        )

        # Track every header output for `.ninja-generated` (consumer
        # compiles need them before they can resolve `#include`s).
        header_outputs = []
        # Per-stem .rs outputs are `include!()`'d by xpcom/rust/xpcom via
        # the `dist/xpcrs/{rt,bt}/all.rs` summary files written by
        # CommonBackend._write_rust_xpidl_summary. Cargo must wait for
        # these on cold builds; tracked separately so the rust-prereqs
        # phony in `_emit_compile_statements` can include them.
        rust_outputs = []
        xpt_files = []

        for module_name in sorted(manager.modules.keys()):
            module = manager.modules[module_name]
            idl_files = sorted(module.idl_files)
            stems = sorted(
                set(mozpath.splitext(mozpath.basename(p))[0] for p in idl_files)
            )

            outputs = []
            for stem in stems:
                outputs.append(mozpath.join(dist_include, f"{stem}.h"))
                outputs.append(mozpath.join(dist_xpcrs, "rt", f"{stem}.rs"))
                outputs.append(mozpath.join(dist_xpcrs, "bt", f"{stem}.rs"))
                header_outputs.append(mozpath.join(dist_include, f"{stem}.h"))
                rust_outputs.append(mozpath.join(dist_xpcrs, "rt", f"{stem}.rs"))
                rust_outputs.append(mozpath.join(dist_xpcrs, "bt", f"{stem}.rs"))

            xpt_path = mozpath.join(xpt_dir, f"{module_name}.xpt")
            ts_path = mozpath.join(xpt_dir, f"{module_name}.d.json")
            outputs.append(xpt_path)
            outputs.append(ts_path)
            xpt_files.append(xpt_path)

            depfile = mozpath.join(deps_dir, f"{module_name}.pp")

            writer.build(
                [self._rel_n_path(o) for o in outputs],
                "xpidl_module",
                inputs=[self._rel_n_path(p) for p in idl_files],
                implicit=[
                    self._rel_n_path(process_py),
                    self._rel_n_path(bindings_conf),
                ],
                variables={
                    "script": self._rel_n_path(process_py),
                    "deps_dir": self._rel_n_path(deps_dir),
                    "bindings_conf": self._rel_n_path(bindings_conf),
                    "include_args": include_args,
                    "header_dir": self._rel_n_path(dist_include),
                    "xpcrs_dir": self._rel_n_path(dist_xpcrs),
                    "xpt_dir": self._rel_n_path(xpt_dir),
                    "module": module_name,
                    "idl_files": " ".join(self._rel_n_path(p) for p in idl_files),
                    "depfile": self._rel_n_path(depfile),
                },
            )
            self._emit_edge_label(outputs[0], f"XPIDL {module_name}")

        # Aggregate link: one xptdata.cpp + dist/include/xptdata.h from
        # every module's .xpt. The C++ output's location matches what
        # the make backend writes (xpcom/reflect/xptinfo/xptdata.cpp).
        xptdata_cpp = mozpath.join(topobjdir, "xpcom/reflect/xptinfo/xptdata.cpp")
        xptdata_h = mozpath.join(dist_include, "xptdata.h")
        writer.build(
            [self._rel_n_path(xptdata_cpp), self._rel_n_path(xptdata_h)],
            "xpidl_link",
            inputs=[self._rel_n_path(p) for p in sorted(xpt_files)],
            implicit=[
                self._rel_n_path(xptcodegen_py),
                self._rel_n_path(perfecthash_py),
            ],
            variables={
                "script": self._rel_n_path(xptcodegen_py),
                "outfile": self._rel_n_path(xptdata_cpp),
                "outheader": self._rel_n_path(xptdata_h),
                "xpts": " ".join(self._rel_n_path(p) for p in sorted(xpt_files)),
            },
        )
        self._emit_edge_label(xptdata_cpp, "XPIDL link")
        header_outputs.append(xptdata_h)

        # Stash for `_emit_compile_statements` to fold into `.ninja-generated`,
        # so consumer compiles get an order_only edge on XPIDL codegen.
        self._xpidl_outputs = list(header_outputs)
        self._xpidl_rust_outputs = list(rust_outputs)
