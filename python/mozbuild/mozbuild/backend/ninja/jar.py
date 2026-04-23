# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


import mozpack.path as mozpath


class JarMixin:
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
            locale_srcdir = mozpath.join(jar.srcdir, "en-US")

            # Defines reach jar_maker as discrete argv tokens (raw values),
            # so nothing re-parses them and the values match what the make
            # backend's `sh` would have delivered. Per-dir DEFINES come from
            # moz.build's `DEFINES["KEY"] = ...`. The global defines are
            # rebuilt from the structured config dict rather than forwarding
            # the pre-POSIX-shell-quoted `ACDEFINES` string (which neither
            # `cmd` nor `CreateProcess` would unquote correctly).
            defines = []
            for d in self._defines_by_dir.get(jar.relobjdir, ()):
                defines.extend(d.get_defines())
            for name in sorted(self.environment.defines):
                defines.append(f"-D{name}={self.environment.defines[name]}")
            defines.append("-DAB_CD=en-US")

            stamp = mozpath.join(jar.objdir, ".jar-maker.stamp")
            # `-s <jar.objdir>` lets jar_maker find generated source
            # files (e.g. `!aiwindow/manifest.json`) — mozmake gets
            # this for free because each Makefile runs in its own
            # objdir (jar.py auto-adds os.getcwd() to its search path),
            # but our ninja invocation runs from $topobjdir.
            jar_args = [
                "-d",
                final_target,
                "-t",
                self._topsrcdir,
                "-f",
                jar_format,
                f"--relativesrcdir={mozpath.normsep(jar.relsrcdir)}",
                "-s",
                mozpath.normsep(jar.objdir),
                "-c",
                mozpath.normsep(locale_srcdir),
                *defines,
                jar_path,
            ]
            self._emit_run_edge(
                writer,
                stamp,
                [{"module": "mozbuild.action.jar_maker", "args": jar_args}],
                f"JAR {jar_path}",
                inputs=[self._rel_n_path(jar_path)],
                implicit=(
                    [self._rel_n_path(o) for o in all_gen_outputs]
                    if all_gen_outputs
                    else None
                ),
                stamp=True,
            )
            self._jar_maker_stamps.append(stamp)

    # Per-app l10n routing. `locale_list` is read relative to topsrcdir
    # to produce the set of phony locales; `l10n_toml` and `relativedir`
    # are passed to l10n_merge / used to locate langpack-metadata.ftl.
    # `dist_subdir` becomes a `--include=` entry for package_langpack
    # (matches the makefile `PKG_ZIP_DIRS = chrome localization
    # $(DIST_SUBDIR)`).
    _L10N_APPS = {
        "browser": {
            "locale_list": "browser/locales/shipped-locales",
            "l10n_toml": "browser/locales/l10n.toml",
            "relativedir": "browser/locales",
            "dist_subdir": "browser",
        },
        "mobile/android": {
            "locale_list": "mobile/android/locales/all-locales",
            "l10n_toml": "mobile/android/locales/l10n.toml",
            "relativedir": "mobile/android/locales",
            "dist_subdir": "",
        },
    }
