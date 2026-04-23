# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import mozpack.path as mozpath

from mozbuild.backend.ninja_syntax import (
    path as n_path,
)
from mozbuild.backend.ninja_syntax import (
    value as n_value,
)
from mozbuild.frontend.data import (
    RustLibrary,
    SimpleProgram,
)


class LinkMixin:
    def _emit_linkable_alias(self, writer, basename, output_path):
        """Emit a `phony` short-name alias for a linkable's output, so
        users can `./mach build <basename>` (e.g. `./mach build xul`)
        instead of typing the full output path.

        Collision handling: if a basename has already been claimed by
        another linkable (rare — Firefox has 2 such cases as of writing,
        both shared-lib gtest/test variants), disambiguate by prepending
        the immediate parent dir of the output path. So `xul` resolves
        to the canonical libxul, `gtest_xul` to the gtest variant.
        Iteration order of each emit site is sorted by output-path
        length so the canonical (shorter-path) linkable wins the bare
        alias; the variant (deeper path) gets the prefixed form.
        """
        if not hasattr(self, "_linkable_aliases"):
            self._linkable_aliases = set()
        if basename in self._linkable_aliases:
            parent = mozpath.basename(mozpath.dirname(output_path))
            alias = f"{parent}_{basename}" if parent else basename
            if alias in self._linkable_aliases:
                # Triple-collision; rare enough to skip rather than
                # invent a deeper prefix.
                return
        else:
            alias = basename
        self._linkable_aliases.add(alias)
        writer.build(alias, "phony", inputs=[self._rel_n_path(output_path)])

    def _emit_archive_statements(self, writer):
        """Emit archive rules for StaticLibrary (excluding rust libs which
        are built via cargo). Only emit a real archive for libraries with
        `no_expand_lib=True`; others are virtual groupings whose objs get
        pulled into their parents via CommonBackend._expand_libs."""
        writer.newline()
        writer.comment("------ static libraries ------")
        writer.newline()
        candidates = [
            lib
            for lib in self._static_libs
            if not isinstance(lib, RustLibrary) and getattr(lib, "no_expand_lib", False)
        ]
        # Sort by output-path length so the shortest-path lib wins the
        # bare-basename alias on collision (canonical primaries live
        # at less-nested paths than variants).
        candidates.sort(key=lambda lib: len(self._lib_output_path(lib)))
        for lib in candidates:
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
                self._rel_n_path(out),
                "archive",
                inputs=[self._rel_n_path(o) for o in all_archive_inputs],
            )
            self._emit_linkable_alias(writer, lib.basename, out)

    def _emit_resource_statements(self, writer):
        if self.environment.substs.get("OS_TARGET") != "WINNT":
            return {}
        linkables = list(self._programs) + list(self._shared_libs)
        if not linkables:
            return {}
        writer.newline()
        writer.comment("------ resource files (.rc/.res) ------")
        writer.newline()
        topobjdir = self._topobjdir
        dist_include = mozpath.join(topobjdir, "dist/include")
        res_files = {}
        for lk in linkables:
            out_path = mozpath.normsep(lk.output_path.full_path)
            binary_name = mozpath.basename(out_path)
            objdir = mozpath.normsep(lk.objdir)
            srcdir = mozpath.normsep(lk.srcdir)
            # Match `resfile_for_manifest` in config/rules.mk: SimplePrograms
            # only get a .res when a sibling <binary>.manifest exists in
            # srcdir. PROGRAM and SHARED_LIBRARY are unconditional.
            if isinstance(lk, SimpleProgram) and not os.path.exists(
                mozpath.join(srcdir, f"{binary_name}.manifest")
            ):
                continue
            res_path = mozpath.join(objdir, f"{binary_name}.res")
            passthru = self._variable_passthru.get(lk.relobjdir)
            rcfile = passthru.variables.get("RCFILE") if passthru else None
            rcinclude = passthru.variables.get("RCINCLUDE") if passthru else None

            defines_args = []
            for d in self._defines_by_dir.get(lk.relobjdir, ()):
                defines_args.extend(d.get_defines())
            includes_args = [f"-I{srcdir}", f"-I{objdir}"]
            includes_args.extend(
                f"-I{mozpath.normsep(li.path.full_path)}"
                for li in self._local_includes_by_dir.get(lk.relobjdir, ())
            )
            includes_args.append(f"-I{dist_include}")
            defines_str = " ".join(self._rarg(f) for f in defines_args)
            includes_str = " ".join(self._rarg(f) for f in includes_args)

            def _resolve(p):
                if p.startswith("/"):
                    return mozpath.normsep(mozpath.join(self._topsrcdir, p[1:]))
                return mozpath.normsep(mozpath.join(srcdir, p))

            rc_order_only = [
                ".ninja-headers-base-exports",
                ".ninja-headers-base-core",
                ".ninja-headers-base-generated",
            ]
            if rcfile:
                rc_input = _resolve(rcfile)
                writer.build(
                    self._rel_n_path(res_path),
                    "compile_rc",
                    inputs=[self._rel_n_path(rc_input)],
                    order_only=rc_order_only,
                    variables={
                        "defines": defines_str,
                        "includes": includes_str,
                    },
                )
            else:
                rc_path = mozpath.join(objdir, f"{binary_name}.rc")
                gen_inputs = []
                rcinclude_arg = ""
                if rcinclude:
                    rcinclude_path = _resolve(rcinclude)
                    gen_inputs.append(self._rel_n_path(rcinclude_path))
                    rcinclude_arg = f"--include {n_value(rcinclude_path)}"
                writer.build(
                    self._rel_n_path(rc_path),
                    "gen_rc",
                    inputs=gen_inputs or None,
                    variables={
                        "srcdir": n_path(srcdir),
                        "binary": n_value(binary_name),
                        "rcinclude_arg": rcinclude_arg,
                    },
                )
                writer.build(
                    self._rel_n_path(res_path),
                    "compile_rc",
                    inputs=[self._rel_n_path(rc_path)],
                    order_only=rc_order_only,
                    variables={
                        "defines": defines_str,
                        "includes": includes_str,
                    },
                )
            res_files[out_path] = res_path
        return res_files

    def _emit_post_link_stamps(self, writer, out_path, final_target):
        """Emit per-binary post-link stamps. Returns a tuple
        (binary_stamps, syms_stamps): `binary_stamps` join the
        `binaries` phony (always run on default build, mirroring
        rules-recipe-attached steps like check_binary/strip in
        rules.mk:422,425-427,464,532); `syms_stamps` join the `syms`
        phony (separate target, only on `ninja syms`, mirroring the
        `syms::` target at rules.mk:654).

        `final_target` is the linkable's install_target (e.g. "dist/bin")
        and gates dumpsymbols (rules.mk:629)."""
        substs = self.environment.substs
        stamps = []
        syms_stamps = []
        # check_binary: rules.mk only runs it on the !(WINNT && clang-cl) branch.
        is_clang_cl_winnt = (
            substs.get("OS_ARCH") == "WINNT" and substs.get("CC_TYPE") == "clang-cl"
        )
        if not is_clang_cl_winnt:
            stamp = out_path + ".check"
            self._emit_run_edge(
                writer,
                stamp,
                [
                    {"module": "mozbuild.action.check_binary", "args": [out_path]},
                    {
                        "module": "mozbuild.action.toolchain_stamp",
                        "args": [stamp, out_path],
                    },
                ],
                f"CHECK {out_path}",
                inputs=[self._rel_n_path(out_path)],
            )
            stamps.append(stamp)

        if substs.get("ENABLE_STRIP"):
            strip_cmd = substs.get("STRIP")
            strip_argv = strip_cmd if isinstance(strip_cmd, list) else [strip_cmd]
            strip_flags = list(substs.get("STRIP_FLAGS") or [])
            stamp = out_path + ".strip"
            self._emit_run_edge(
                writer,
                stamp,
                [
                    {"exec": [*strip_argv, *strip_flags, out_path]},
                    {
                        "module": "mozbuild.action.toolchain_stamp",
                        "args": [stamp, out_path],
                    },
                ],
                f"STRIP {out_path}",
                inputs=[self._rel_n_path(out_path)],
            )
            stamps.append(stamp)

        # dumpsymbols: rules.mk:617-655. Gated on:
        # - MOZ_CRASHREPORTER set
        # - MOZ_COPY_PDBS NOT set (else-if branch)
        # - FINAL_TARGET starts with $(DIST)/bin
        # - Either MOZ_AUTOMATION not set, or MOZ_AUTOMATION_BUILD_SYMBOLS=1
        if (
            substs.get("MOZ_CRASHREPORTER")
            and not substs.get("MOZ_COPY_PDBS")
            and final_target
            and final_target.startswith("dist/bin")
            and (
                not substs.get("MOZ_AUTOMATION")
                or substs.get("MOZ_AUTOMATION_BUILD_SYMBOLS") == "1"
            )
        ):
            dump_symbols_flags = " ".join(
                self._rarg(f) for f in substs.get("DUMP_SYMBOLS_FLAGS") or []
            )
            # Stamp name matches make's `<basename>_syms.track`.
            stamp = mozpath.join(
                mozpath.dirname(out_path),
                f"{mozpath.basename(out_path)}_syms.track",
            )
            writer.build(
                self._rel_n_path(stamp),
                "dumpsymbols",
                inputs=[self._rel_n_path(out_path)],
                variables={"dump_symbols_flags": dump_symbols_flags},
            )
            syms_stamps.append(stamp)
            if substs.get("OS_ARCH") == "WINNT" and substs.get("WINCHECKSEC"):
                wcs_stamp = out_path + ".wcs"
                script = mozpath.join(self._topsrcdir, "build/win32/autowinchecksec.py")
                python_bin = substs.get("PYTHON3") or "python"
                self._emit_run_edge(
                    writer,
                    wcs_stamp,
                    [
                        {"exec": [python_bin, script, out_path]},
                        {
                            "module": "mozbuild.action.toolchain_stamp",
                            "args": [wcs_stamp, out_path],
                        },
                    ],
                    f"WINCHECKSEC {out_path}",
                    inputs=[self._rel_n_path(out_path)],
                )
                syms_stamps.append(wcs_stamp)
        return stamps, syms_stamps

    def _emit_nsinstall_edges(self, writer):
        if self.environment.substs.get("HOST_OS_ARCH") == "WINNT":
            return []
        nsinstall_real = None
        for hp in self._host_programs:
            if hp.program == "nsinstall_real":
                nsinstall_real = hp
                break
        if not nsinstall_real:
            return []
        src = mozpath.normsep(nsinstall_real.output_path.full_path)
        intermediate = mozpath.join(self._topobjdir, "config/nsinstall")
        final = mozpath.join(self._topobjdir, "dist/bin/nsinstall")
        writer.newline()
        writer.comment("------ nsinstall (from config/Makefile.in) ------")
        writer.newline()
        writer.build(
            self._rel_n_path(intermediate),
            "install_file",
            inputs=[self._rel_n_path(src)],
        )
        writer.build(
            self._rel_n_path(final),
            "install_file",
            inputs=[self._rel_n_path(intermediate)],
        )
        return [intermediate, final]

    def _emit_shared_link_statements(self, writer):
        writer.newline()
        writer.comment("------ shared libraries ------")
        writer.newline()
        is_clang_cl = self.environment.substs.get("CC_TYPE") == "clang-cl"
        # Sort by output-path length: the canonical libxul lives at
        # `dist/bin/xul.dll` (shorter), the gtest variant at
        # `dist/bin/gtest/xul.dll` (longer). Sorting puts the canonical
        # one first so it wins the bare `xul` alias.
        sorted_libs = sorted(
            self._shared_libs,
            key=lambda lib: len(mozpath.normsep(lib.output_path.full_path)),
        )
        for lib in sorted_libs:
            # For DIST_INSTALL'd shared libs, output_path puts the DLL at
            # its final dist/bin/ location so downstream js.exe finds it
            # without a separate install step.
            out = mozpath.normsep(lib.output_path.full_path)
            implib = (
                mozpath.join(lib.objdir, getattr(lib, "import_name", lib.lib_name))
                if is_clang_cl
                else None
            )
            objs, shared_libs, os_libs, static_libs = self._expand_libs(lib)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            res_file = self._res_files.get(out)
            if res_file:
                link_inputs.append(res_file)

            # DEFFILE / SYMBOLS_FILE: DEFFILE puts `-DEF:<relpath>` (for
            # clang-cl) into LDFLAGS, while SYMBOLS_FILE drives the same
            # `-DEF:<relpath>` via SharedLibrary.symbols_link_arg (which the
            # recursive make backend folds into EXTRA_DSO_LDOPTS). We
            # cover both by scanning LDFLAGS and also appending the
            # symbols_link_arg if set. Since our ninja commands run from
            # $topobjdir, rewrite the relative path to an absolute path
            # and collect it for the implicit-dep list.
            ldflags_raw = list(self._computed_flag_list(lib.relobjdir, "LDFLAGS"))
            # Non-MSVC shared link goes through `$CXX -shared -o`, so
            # CXX_LDFLAGS (compiler-driver flags like `-pthread`,
            # MOZ_HARDENING_CFLAGS, OS_CPPFLAGS) must travel with LDFLAGS.
            # See _emit_program_statements for the mirror case.
            if not is_clang_cl:
                ldflags_raw.extend(
                    self._computed_flag_list(lib.relobjdir, "CXX_LDFLAGS")
                )
            # SONAME on ELF/Solaris targets so consumers' DT_NEEDED
            # records the canonical lib name instead of the link-time
            # path. We pass shared libs as full paths (e.g.
            # `dist/bin/libfoo.so`) at link time; without a SONAME the
            # linker burns that path into DT_NEEDED, and downstream
            # tools (canonical case: `toolkit/library/build/dependentlibs.py`)
            # can't resolve it via libpath lookup. Mirrors `MKSHLIB` in
            # `build/moz.configure/toolchain.configure:3424-3434`.
            # Darwin uses dylib install_names instead.
            substs = self.environment.substs
            os_target = substs.get("OS_TARGET")
            target_kernel = substs.get("TARGET_KERNEL")
            if (
                not is_clang_cl
                and lib.soname
                and target_kernel != "Darwin"
                and os_target != "Darwin"
            ):
                soname_flag = "-soname" if os_target == "NetBSD" else "-h"
                ldflags_raw.append(f"-Wl,{soname_flag},{lib.soname}")
            # Darwin counterpart of SONAME: set the dylib's LC_ID_DYLIB to
            # `@rpath/<basename>` so consumers' LC_LOAD_DYLIB records the
            # canonical name, not the link-time path. Without this, ld
            # defaults LC_ID_DYLIB to the `-o` argument (`dist/bin/foo.dylib`),
            # which propagates to every linker that consumes the dylib and
            # then breaks `toolkit/library/build/dependentlibs.py` (it strips
            # `@rpath/` / `@executable_path/` and looks the basename up under
            # `dist/bin`). Mirrors `config/rules.mk:277-278`
            # (`-install_name $(_LOADER_PATH)/$(@F) -compatibility_version 1
            # -current_version 1`, with `_LOADER_PATH := @rpath`).
            if not is_clang_cl and (target_kernel == "Darwin" or os_target == "Darwin"):
                ldflags_raw.extend([
                    "-install_name",
                    f"@rpath/{mozpath.basename(out)}",
                    "-compatibility_version",
                    "1",
                    "-current_version",
                    "1",
                ])
            symbols_link_arg = getattr(lib, "symbols_link_arg", None)
            if symbols_link_arg:
                ldflags_raw.append(symbols_link_arg)
            # Symbols-file flags hold a path relative to the binary's
            # objdir in the make recipe (which `cd`s there). Ninja runs
            # from $topobjdir, so rewrite each relative path to absolute
            # and collect for implicit-dep tracking.
            symbol_file_prefixes = (
                "-DEF:",
                "-Wl,--version-script,",
                "-Wl,-exported_symbols_list,",
            )
            symbol_file_abs = []
            for i, f in enumerate(ldflags_raw):
                for prefix in symbol_file_prefixes:
                    if f.startswith(prefix):
                        rel = f[len(prefix) :]
                        if rel and not os.path.isabs(rel):
                            abs_path = mozpath.normpath(mozpath.join(lib.objdir, rel))
                            ldflags_raw[i] = prefix + abs_path
                            symbol_file_abs.append(abs_path)
                        elif rel:
                            symbol_file_abs.append(rel)
                        break

            implicit_deps = symbol_file_abs or None

            # Only pass linker-native flags (LDFLAGS) when invoking lld-link
            # directly. CXX_LDFLAGS/C_LDFLAGS are compiler-driver flags the
            # make backend passes through `$(CXX) -o` at link time; calling
            # the linker ourselves means they're not applicable. The DEF
            # path rewrite happened in the ldflags_raw loop above.
            pdbfile = (
                mozpath.join(lib.objdir, lib.lib_name + ".pdb") if is_clang_cl else None
            )
            writer.build(
                self._rel_n_path(out),
                "link_shared",
                inputs=[self._rel_n_path(o) for o in link_inputs],
                implicit_outputs=self._rel_n_path(implib)
                if implib and implib != out
                else None,
                implicit=[self._rel_n_path(d) for d in implicit_deps]
                if implicit_deps
                else None,
                variables={
                    "implib": self._rel_n_path(implib) if implib else None,
                    "pdbfile": self._rel_n_path(pdbfile) if pdbfile else None,
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(self._rarg(f) for f in ldflags_raw),
                },
            )
            self._emit_linkable_alias(writer, lib.basename, out)
            stamps, syms = self._emit_post_link_stamps(writer, out, lib.install_target)
            if stamps:
                self._post_link_stamps[out] = stamps
            self._syms_stamps.extend(syms)

    def _emit_program_statements(self, writer):
        writer.newline()
        writer.comment("------ programs ------")
        writer.newline()
        sorted_programs = sorted(
            self._programs, key=lambda p: len(p.output_path.full_path)
        )
        for p in sorted_programs:
            out = p.output_path.full_path
            objs, shared_libs, os_libs, static_libs = self._expand_libs(p)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            res_file = self._res_files.get(mozpath.normsep(out))
            if res_file:
                link_inputs.append(res_file)
            ldflags = list(
                self.environment.substs.get("WIN32_EXE_DEFAULT_LDFLAGS") or []
            )
            passthru = self._variable_passthru.get(p.relobjdir)
            if passthru:
                ldflags.extend(passthru.variables.get("WIN32_EXE_LDFLAGS", []))
            ldflags.extend(self._computed_flag_list(p.relobjdir, "LDFLAGS"))
            # On non-MSVC the program link goes through `$CXX -o` (the
            # compiler driver), so it needs the compiler-driver-level
            # linker flags too: `-pthread` (auto-links libpthread for
            # zucchini, etc.), MOZ_HARDENING_CFLAGS, OS_CPPFLAGS, etc.
            # The make backend folds these into COMPUTED_CXX_LDFLAGS and
            # passes them alongside LDFLAGS. lld-link doesn't accept
            # them, so this is gated on non-clang-cl.
            if self.environment.substs.get("CC_TYPE") != "clang-cl":
                ldflags.extend(self._computed_flag_list(p.relobjdir, "CXX_LDFLAGS"))
            # MOZ_PROGRAM_LDFLAGS (rules.mk:144-163): Mac rpath so a
            # PROGRAM next to its dylibs in dist/bin can resolve
            # @rpath-prefixed install_names. arm-Darwin also needs
            # @executable_path/Frameworks for the Frameworks bundle.
            substs = self.environment.substs
            if substs.get("OS_ARCH") == "Darwin":
                ldflags.append("-Wl,-rpath,@executable_path")
                if substs.get("TARGET_CPU") == "arm":
                    ldflags.extend(["-Wl,-rpath", "-Wl,@executable_path/Frameworks"])
            stem = mozpath.splitext(p.program)[0]
            pdbfile = mozpath.join(p.objdir, stem + ".pdb")
            implib = mozpath.join(p.objdir, stem + ".lib")
            writer.build(
                self._rel_n_path(out),
                "link_exe",
                inputs=[self._rel_n_path(o) for o in link_inputs],
                variables={
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(self._rarg(f) for f in ldflags),
                    "pdbfile": self._rel_n_path(pdbfile),
                    "implib": self._rel_n_path(implib),
                },
            )
            # Program has `.program` ("firefox.exe"), not `.basename` like
            # libraries do; strip the extension for the alias name so users
            # type `./mach build firefox` not `./mach build firefox.exe`.
            self._emit_linkable_alias(writer, stem, out)
            stamps, syms = self._emit_post_link_stamps(
                writer, mozpath.normsep(out), p.install_target
            )
            if stamps:
                self._post_link_stamps[mozpath.normsep(out)] = stamps
            self._syms_stamps.extend(syms)
