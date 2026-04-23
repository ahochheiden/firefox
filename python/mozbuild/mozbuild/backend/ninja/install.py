# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os

import mozpack.path as mozpath
from mozpack.manifests import InstallManifest


class InstallMixin:
    def _emit_install_statements(self, writer):
        """Emit one `run_install_manifest` edge per install target.
        Every source file referenced by the manifest is declared as
        a ninja input so source mtime changes invalidate the edge.
        The manifest itself is an implicit input so manifest edits
        also trigger re-install."""
        from mozpack.files import FileFinder

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
            "dist_xpi-stage": "dist/xpi-stage",
            "_tests": "_tests",
            "_test_files": "_tests",
            "_ninja_test_files": "_tests",
        }
        # Group manifests by destination. `_tests`, `_test_files`, and
        # `_ninja_test_files` all install to `_tests/`. Emitted as separate
        # `process_install_manifest` edges, each invocation independently
        # treats files in the shared dir but outside its own manifest as
        # unaccounted-for and `os.remove`s them, racing the others. The
        # make backend serializes these via tier ordering; we merge them
        # into a single combined manifest+edge so the unaccounted-files
        # logic sees the union.
        target_groups = {}
        for manifest_name, target_rel in manifest_to_target.items():
            target_groups.setdefault(target_rel, []).append(manifest_name)

        for target_rel, manifest_names in target_groups.items():
            existing = [
                n
                for n in manifest_names
                if os.path.exists(mozpath.join(manifests_dir, n))
            ]
            if not existing:
                continue
            group_name = existing[0] if len(existing) == 1 else "+".join(existing)
            install_dir = mozpath.join(self._topobjdir, target_rel)
            track_path = mozpath.join(
                self._topobjdir,
                f"install_{group_name}.track",
            )
            # Track path is shared by every manifest_name in the group so
            # downstream code (manifest_prefixes routing, install track
            # lookups) resolves correctly regardless of which name is queried.
            for n in manifest_names:
                self._install_tracks[n] = track_path

            mf = InstallManifest()
            for n in existing:
                sub = InstallManifest(path=mozpath.join(manifests_dir, n))
                to_remove = []
                to_add_link = []
                to_add_copy = []
                for dst, entry in list(sub._dests.items()):
                    kind = entry[0]
                    if kind == sub.LINK:
                        src = mozpath.normsep(entry[1])
                        if os.path.isdir(src):
                            to_remove.append(dst)
                            for path, _ in FileFinder(src).find("**"):
                                full = mozpath.normsep(mozpath.join(src, path))
                                if os.path.isfile(full):
                                    to_add_link.append((
                                        full,
                                        mozpath.normsep(mozpath.join(dst, path)),
                                    ))
                    elif kind == sub.COPY:
                        src = mozpath.normsep(entry[1])
                        if os.path.isdir(src):
                            to_remove.append(dst)
                            for path, _ in FileFinder(src).find("**"):
                                full = mozpath.normsep(mozpath.join(src, path))
                                if os.path.isfile(full):
                                    to_add_copy.append((
                                        full,
                                        mozpath.normsep(mozpath.join(dst, path)),
                                    ))
                    elif kind in (
                        sub.REQUIRED_EXISTS,
                        sub.OPTIONAL_EXISTS,
                        sub.CONTENT,
                        sub.PREPROCESS,
                        sub.PATTERN_LINK,
                        sub.PATTERN_COPY,
                    ):
                        pass
                    else:
                        raise Exception(
                            f"Unknown install manifest entry kind {kind} "
                            f"for {dst!r} in {n}"
                        )
                for dst in to_remove:
                    del sub._dests[dst]
                for src, dst in to_add_link:
                    sub.add_link(src, dst)
                for src, dst in to_add_copy:
                    sub.add_copy(src, dst)
                # Manually merge: `_tests` and `_test_files` overlap on
                # entries like `crashtest/crashtest.toml`, so the strict
                # `__ior__` (which raises on duplicates) is wrong here.
                # First-seen wins; duplicates point at the same source.
                mf._source_files |= sub._source_files
                for dst, entry in sub._dests.items():
                    if dst not in mf._dests:
                        mf._dests[dst] = entry

            expanded_path = mozpath.join(manifests_dir, group_name + ".expanded")
            with self._write_file(expanded_path) as fh:
                mf.write(fileobj=fh)

            sources = []
            for dst, entry in mf._dests.items():
                kind = entry[0]
                if kind in (mf.LINK, mf.COPY, mf.PREPROCESS):
                    src = mozpath.normsep(entry[1])
                    if os.path.isfile(src):
                        sources.append(src)
                elif kind in (mf.PATTERN_LINK, mf.PATTERN_COPY):
                    _, base, pattern, _ = entry
                    for path, _ in FileFinder(base).find(pattern):
                        full = mozpath.normsep(mozpath.join(base, path))
                        if os.path.isfile(full):
                            sources.append(full)

            writer.build(
                self._rel_n_path(track_path),
                "run_install_manifest",
                inputs=[self._rel_n_path(s) for s in sorted(set(sources))],
                implicit=self._rel_n_path(expanded_path),
                variables={
                    "install_dir": self._rel_n_path(install_dir),
                    "track": self._rel_n_path(track_path),
                    "manifest": self._rel_n_path(expanded_path),
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

        manifest_prefixes = []
        for manifest_name, target_rel in manifest_to_target.items():
            track = self._install_tracks.get(manifest_name)
            if track:
                manifest_prefixes.append((
                    mozpath.join(self._topobjdir, target_rel) + "/",
                    track,
                ))

        edge_outputs = set()
        for p in self._programs + self._host_programs:
            edge_outputs.add(mozpath.normsep(p.output_path.full_path))
        for lib in self._shared_libs:
            edge_outputs.add(mozpath.normsep(lib.output_path.full_path))
            implib = mozpath.join(lib.objdir, getattr(lib, "import_name", lib.lib_name))
            edge_outputs.add(mozpath.normsep(implib))
        for lib in self._static_libs:
            edge_outputs.add(mozpath.normsep(self._lib_output_path(lib)))
        for lib in self._host_libraries:
            edge_outputs.add(mozpath.normsep(self._lib_output_path(lib)))
        for lib in self._rust_libs:
            edge_outputs.add(mozpath.normsep(self._lib_output_path(lib)))
        for g in self._generated_files:
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
                    edge_outputs.add(mozpath.normsep(output))
        # OBJDIR_PP_FILES preprocess outputs (e.g. dist/bin/.lldbinit from
        # build/.lldbinit.in) are produced by per-file `pp_install` edges
        # emitted later in this function. Record their dsts here so that
        # `_route_input` returns them as direct edge inputs rather than
        # routing them to an install-manifest track that doesn't actually
        # build them. Without this, OBJDIR_FILES copies from those dsts
        # race against the producer at low job counts.
        for _, dst, _, _ in self._pp_installs:
            edge_outputs.add(mozpath.normsep(dst))

        def _route_input(src):
            if src in edge_outputs:
                return src
            for prefix, track in manifest_prefixes:
                if src.startswith(prefix):
                    return track
            return src

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
            seen = set()
            inputs = []
            for src, _ in pairs:
                routed = _route_input(src)
                if routed not in seen:
                    seen.add(routed)
                    inputs.append(routed)
            writer.build(
                [self._rel_n_path(o) for o in outputs],
                "install_batch",
                inputs=[self._rel_n_path(i) for i in inputs],
                implicit=self._rel_n_path(manifest_path),
                variables={"manifest": self._rel_n_path(manifest_path)},
            )
            return outputs

        _emit_install_batch("distinc", batch_dist_include)
        # Post batch outputs (dist/bin/dependentlibs.list etc.) feed the
        # `install` phony so they're reachable from the default target.
        # Without this, firefox.exe fails with "Couldn't load XPCOM"
        # because dependentlibs.list isn't staged into dist/bin.
        self._install_batch_post_outputs = _emit_install_batch("post", batch_post) or []

        # OBJDIR_PP_FILES: preprocess the .in and install. One edge per
        # output (no batching — each has unique defines). Mirror mozmake's
        # `$(DEFINES) $(ACDEFINES)` order: per-dir defines first, then
        # the global ACDEFINES from configure (which carries MOZ_BUILD_APP
        # and similar that AppConstants.sys.mjs references).
        acdefines = self.environment.substs.get("ACDEFINES", "")
        seen_pp = set()
        pp_install_outputs = []
        for src, dst, defines, extra_deps in self._pp_installs:
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
            defines_str = " ".join(self._rarg(a) for a in def_args)
            if acdefines:
                defines_str = f"{defines_str} {acdefines}" if defines_str else acdefines
            # `extra_deps` carry the per-directory `PP_FILES_EXTRA_DEPS`
            # value: paths the preprocessor opens at runtime via
            # `#include @TOPOBJDIR@/...` but that aren't passed as
            # arguments. Wire them as implicit deps so the edge waits
            # for them to exist before running.
            writer.build(
                self._rel_n_path(dst),
                "pp_install",
                inputs=self._rel_n_path(src),
                implicit=[self._rel_n_path(d) for d in extra_deps]
                if extra_deps
                else None,
                variables={"defines": defines_str},
            )
            pp_install_outputs.append(dst)
        # Feed pp_install outputs into the `install` phony so they're
        # reachable from the default target (e.g. dist/bin/modules/
        # AppConstants.sys.mjs from EXTRA_PP_JS_MODULES). The dist/include
        # subset is already covered transitively by .ninja-generated, but
        # listing them again is harmless.
        self._pp_install_outputs = pp_install_outputs

    def _emit_xpi_package_statements(self, writer):
        """Emit one `zip_xpi` edge per directory with `XPI_PKGNAME`.

        For staged-tree xpis (the regular case, e.g. specialpowers),
        depend implicitly on the ``dist/xpi-stage`` install track so
        zipping waits for ``FinalTargetFiles`` to land. For
        ``XPI_TESTDIR`` xpis (telemetry test add-ons) the source is
        ``srcdir`` directly, so the depfile alone tracks inputs.
        Outputs feed into the ``install`` phony.
        """
        self._xpi_outputs = []
        if not self._xpi_packages:
            return
        writer.newline()
        writer.comment("------ XPI_PKGNAME packaging ------")
        writer.newline()
        xpi_stage_track = self._install_tracks.get("dist_xpi-stage")
        xpi_stage_prefix = mozpath.join(self._topobjdir, "dist/xpi-stage") + "/"
        for xpi_path, source_dir in self._xpi_packages:
            implicit = []
            if xpi_stage_track and source_dir.startswith(xpi_stage_prefix):
                implicit.append(self._rel_n_path(xpi_stage_track))
            writer.build(
                self._rel_n_path(xpi_path),
                "zip_xpi",
                implicit=implicit or None,
                variables={
                    "stage_dir": self._rel_n_path(source_dir),
                    "depfile": self._rel_n_path(xpi_path + ".d"),
                    "xpi_out": xpi_path,
                },
            )
            self._xpi_outputs.append(xpi_path)
