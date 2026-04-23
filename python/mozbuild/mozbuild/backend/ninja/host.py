# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mozpack.path as mozpath

from mozbuild.backend.ninja_syntax import value as n_value


class HostMixin:
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
        real_libs.sort(key=lambda lib: len(self._lib_output_path(lib)))
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
                self._rel_n_path(out),
                "archive",
                inputs=[self._rel_n_path(o) for o in all_archive_inputs],
            )
            self._emit_linkable_alias(writer, lib.basename, out)

    def _emit_host_program_statements(self, writer):
        if not self._host_programs:
            return
        writer.newline()
        writer.comment("------ host programs ------")
        writer.newline()
        sorted_progs = sorted(
            self._host_programs, key=lambda p: len(p.output_path.full_path)
        )
        for p in sorted_progs:
            out = p.output_path.full_path
            objs, shared_libs, os_libs, static_libs = self._expand_libs(p)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            ldflags = list(self._computed_flag_list(p.relobjdir, "HOST_LDFLAGS"))
            stem = mozpath.splitext(p.program)[0]
            pdbfile = mozpath.join(p.objdir, stem + ".pdb")
            implib = mozpath.join(p.objdir, stem + ".lib")
            writer.build(
                self._rel_n_path(out),
                "host_link_exe",
                inputs=[self._rel_n_path(o) for o in link_inputs],
                variables={
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(self._rarg(f) for f in ldflags),
                    "pdbfile": self._rel_n_path(pdbfile),
                    "implib": self._rel_n_path(implib),
                },
            )
            # HostProgram has `.program` ("wasm2c.exe"), not `.basename`;
            # strip the extension for the alias.
            self._emit_linkable_alias(writer, stem, out)

    def _emit_host_shared_link_statements(self, writer):
        if not self._host_shared_libs:
            return
        writer.newline()
        writer.comment("------ host shared libraries ------")
        writer.newline()
        sorted_libs = sorted(
            self._host_shared_libs, key=lambda lib: len(self._lib_output_path(lib))
        )
        for lib in sorted_libs:
            out = self._lib_output_path(lib)
            objs, shared_libs, os_libs, static_libs = self._expand_libs(lib)
            link_inputs = list(objs)
            for static_lib in static_libs:
                link_inputs.append(self._lib_output_path(static_lib))
            for shared_lib in shared_libs:
                link_inputs.append(self._lib_output_path(shared_lib))
            ldflags = list(self._computed_flag_list(lib.relobjdir, "HOST_LDFLAGS"))
            writer.build(
                self._rel_n_path(out),
                "host_link_shared",
                inputs=[self._rel_n_path(o) for o in link_inputs],
                variables={
                    "libs": " ".join(n_value(s) for s in os_libs),
                    "ldflags": " ".join(self._rarg(f) for f in ldflags),
                },
            )
            self._emit_linkable_alias(writer, lib.basename, out)
