# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mozpack.path as mozpath

from mozbuild.backend.ninja_syntax import value as n_value


class WasmMixin:
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
        toolchain_stamp = getattr(self, "_toolchain_stamp_path", None)
        toolchain_implicit = (
            [self._rel_n_path(toolchain_stamp)] if toolchain_stamp else None
        )
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
                            self._rarg(f) for f in wasm_cflags + extra
                        )
                    elif ext in (".cpp", ".cc", ".cxx"):
                        rule_name = "wasm_cxx"
                        flag_var = "wasm_cxxflags"
                        flag_value = " ".join(
                            self._rarg(f) for f in wasm_cxxflags + extra
                        )
                    else:
                        writer.comment(f"unknown wasm source extension for {src_norm}")
                        continue
                    # Wasm compiles need dist/include populated to find
                    # `mozilla/mozalloc.h`, stl_wrappers, etc. Fence on
                    # `.ninja-headers-base` only — that's the install-
                    # manifest track + dist/include _installs/_pp_installs,
                    # nothing that transitively depends on wasm output.
                    #
                    # Source-style GeneratedFile outputs (the `.wasm.c`
                    # produced by wasm2c) are excluded from the codegen
                    # category, so even the broader `.ninja-headers-codegen`
                    # would be cycle-free; we still keep this minimal to
                    # avoid coupling wasm compiles to unrelated codegen.
                    writer.build(
                        self._rel_n_path(obj),
                        rule_name,
                        inputs=self._rel_n_path(src_norm),
                        implicit=toolchain_implicit,
                        order_only=[
                            ".ninja-headers-base-exports",
                            ".ninja-headers-base-core",
                            ".ninja-headers-base-generated",
                        ],
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
        sorted_wasm = sorted(
            self._wasm_libraries,
            key=lambda lib: len(mozpath.join(lib.objdir, lib.basename)),
        )
        for lib in sorted_wasm:
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
                self._rel_n_path(out),
                "wasm_link",
                inputs=[self._rel_n_path(o) for o in link_inputs],
                variables={
                    "libs": libs_flag,
                    "wasm_ldflags": " ".join(self._rarg(f) for f in wasm_ldflags),
                },
            )
            # Strip the `.wasm` suffix that SANDBOXED_WASM_LIBRARY_NAME
            # bakes into the basename, so users can `./mach build rlbox`
            # rather than `./mach build rlbox.wasm`.
            alias = mozpath.splitext(lib.basename)[0]
            self._emit_linkable_alias(writer, alias, out)
