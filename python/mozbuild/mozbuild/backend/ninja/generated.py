# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import mozpack.path as mozpath

from mozbuild.backend.ninja_syntax import value as n_value


class GeneratedMixin:
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
            # extra_deps are build-graph prereqs only (not passed to the
            # script as positional args). Used for things the script
            # discovers at runtime, like a preprocessor's
            # `#include @TOPOBJDIR@/foo.h`.
            extra_deps = [
                mozpath.normsep(d.full_path) for d in getattr(g, "extra_deps", ()) or ()
            ]

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
            outs = list(self._expand_num_outputs_outputs(primary, outs, g.flags or ()))
            depfile = mozpath.join(
                mozpath.dirname(primary), ".deps", mozpath.basename(primary) + ".pp"
            )

            extra_parts = list(ins)
            if g.flags:
                # Expand make-style `$(DEFINES)` / `$(LOCAL_INCLUDES)`
                # references that some scripts (e.g.
                # config/external/ffi/preprocess_libffi_asm.py) embed in
                # their flag list. Make would expand them at recipe
                # time; ninja invokes commands directly, so substitute
                # here using the GeneratedFile's context.
                for f in g.flags:
                    extra_parts.extend(self._expand_make_flag_refs(g.relobjdir, str(f)))

            script_path = g.script
            implicit = [self._rel_n_path(script_path)]
            implicit.extend(self._rel_n_path(d) for d in extra_deps)
            writer.build(
                [self._rel_n_path(o) for o in outs],
                "pygen",
                inputs=[self._rel_n_path(i) for i in ins] if ins else None,
                # Script is an implicit dep so ninja rebuilds when the
                # script changes. extra_deps are GeneratedFile-declared
                # runtime deps (e.g. `#include @TOPOBJDIR@/foo.h`).
                implicit=implicit,
                variables={
                    "script": self._rel_n_path(script_path),
                    "method": n_value(g.method or "main"),
                    "primary": self._rel_n_path(primary),
                    "depfile": self._rel_n_path(depfile),
                    "locale": locale_arg,
                    # `response_arg` (not `n_value`) so joined-string
                    # entries from `_expand_make_flag_refs` (e.g. the
                    # `-D... -D...` collapsed `$(DEFINES)`) survive
                    # `CreateProcess` argv parsing as one argv entry,
                    # matching make's `'$(DEFINES)'` shell quoting.
                    "extra": " ".join(self._rarg(p) for p in extra_parts),
                },
            )
