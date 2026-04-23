# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import hashlib
import os
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mozpack.path as mozpath
from mozfile import json

from mozbuild.backend.cargo_build_defs import cargo_extra_outputs


class RustMixin:
    def _rust_program_install_dest(self, obj):
        target = "dist/host/bin" if obj.KIND == "host" else obj.install_target
        return mozpath.normsep(
            mozpath.join(self._topobjdir, target, mozpath.basename(obj.location))
        )

    def _clang_plugin_order_only(self):
        """Order-only deps so cargo edges wait for the Mozilla clang
        plugin to be built.

        With `ENABLE_CLANG_PLUGIN`, `libclang-plugin.so` (a
        HostSharedLibrary) is loaded via `-Xclang -load` into every
        target C/C++ compilation -- including the C that cargo build
        scripts and crates compile through the `cc` crate. sccache hashes
        the plugin as a compiler input, so it must exist before any cargo
        edge runs or the build fails (`Failed to open file for hashing`).
        The make backend enforces this in `config/recurse.mk` (every
        `%/target`/`%/target-objects` depends on `build/clang-plugin/host`);
        this mirrors it as an order-only dep on the plugin output.

        Computed once and cached."""
        dep = getattr(self, "_clang_plugin_dep", None)
        if dep is None:
            dep = []
            if self.environment.substs.get("ENABLE_CLANG_PLUGIN"):
                for lib in self._host_shared_libs:
                    if lib.basename == "clang-plugin":
                        dep = [self._rel_n_path(self._lib_output_path(lib))]
                        break
            self._clang_plugin_dep = dep
        return dep

    def _gkrust_order_only(self, exclude_output=None):
        """Order-only dep so every other cargo edge waits for gkrust.

        gkrust is emitted first and all other cargo invocations are
        gated behind it so they find its transitively-built dep crates
        already cached in the shared `CARGO_TARGET_DIR` and skip
        rebuilding them (mirrors `recursivemake.py`). Returns a
        one-element list, or empty when this build has no gkrust or the
        edge *is* gkrust (`exclude_output` matches its output)."""
        gkrust = getattr(self, "_gkrust_output", None)
        if gkrust and gkrust != exclude_output:
            return [gkrust]
        return []

    def _cargo_spec_dict(self, lib, output_path):
        return {
            "kind": "library",
            "subcommand": "build",
            "manifest_path": mozpath.normsep(lib.cargo_file),
            "output_path": mozpath.normsep(output_path),
            "features": list(lib.features) if lib.features else [],
            "target_triple": self.environment.substs.get("RUST_TARGET", ""),
            "is_megazord": "megazord" in lib.lib_name,
            "is_gkrust_gtest": "gkrust_gtest" in lib.lib_name,
            "is_ltoable": True,
            "extra_rustcflags": [],
            "cargo_extra_cli_flags": [],
            "output_category": getattr(lib, "output_category", None),
            "relsrcdir": lib.relsrcdir,
            "relobjdir": lib.relobjdir,
            "computed_cflags": " ".join(
                self._computed_flag_list(lib.relobjdir, "CFLAGS")
            ),
            "computed_cxxflags": " ".join(
                self._computed_flag_list(lib.relobjdir, "CXXFLAGS")
            ),
            "computed_host_cflags": " ".join(
                self._computed_flag_list(lib.relobjdir, "HOST_CFLAGS")
            ),
            "computed_host_cxxflags": " ".join(
                self._computed_flag_list(lib.relobjdir, "HOST_CXXFLAGS")
            ),
            "computed_ldflags": " ".join(
                self._computed_flag_list(lib.relobjdir, "LDFLAGS")
            ),
        }

    def _write_cargo_spec(self, path, lib, output_path):
        with self._write_file(path) as fh:
            json.dump(
                self._cargo_spec_dict(lib, output_path), fh, indent=2, sort_keys=True
            )

    def _emit_rust_statements(self, writer):
        """Emit cargo edges. One edge per RustLibrary, gated on
        `.ninja-rust-prereqs`. Each edge calls `mozbuild.action.cargo_build`
        with a per-target spec JSON written alongside this rule.

        gkrust is emitted first and all other cargo invocations are
        order-only gated behind it so they find its transitively-built
        dep crates already cached in the shared `CARGO_TARGET_DIR` and
        skip rebuilding them. Mirrors `recursivemake.py:807-814`."""
        writer.newline()
        writer.comment("------ rust libraries ------")
        writer.newline()
        if not self._rust_libs:
            return
        sorted_rust = sorted(
            self._rust_libs, key=lambda lib: len(self._lib_output_path(lib))
        )
        if self.environment.substs.get("MOZ_NINJA_EXPERIMENTAL_EXPLICIT_RUSTC_EDGES"):
            self._emit_explicit_rust_library_edges(writer, sorted_rust)
            return
        gkrust_lib = next(
            (lib for lib in sorted_rust if getattr(lib, "is_gkrust", False)), None
        )
        self._gkrust_output = (
            self._rel_n_path(self._lib_output_path(gkrust_lib)) if gkrust_lib else None
        )
        for lib in sorted_rust:
            out = self._lib_output_path(lib)
            depfile = mozpath.splitext(out)[0] + ".d"
            spec_path = mozpath.join(lib.objdir, ".cargo-spec.json")
            self._write_cargo_spec(spec_path, lib, out)
            order_only = [".ninja-rust-prereqs"]
            order_only.extend(self._gkrust_order_only(self._rel_n_path(out)))
            order_only.extend(self._clang_plugin_order_only())
            writer.build(
                self._rel_n_path(out),
                "cargo_build",
                order_only=order_only,
                implicit=[self._rel_n_path(spec_path)],
                variables={
                    "spec": self._rel_n_path(spec_path),
                    "depfile": self._rel_n_path(depfile),
                },
            )
            self._emit_linkable_alias(writer, lib.basename, out)
            # Key by the same form ninja records in `.ninja_log`: the
            # topobjdir-relative path that was written on the build
            # edge (via `_rel_n_path`), so `_classify_ninja_edge`
            # lookups match.
            self._rust_lib_outputs[self._rel_n_path(out)] = mozpath.splitext(
                mozpath.basename(out)
            )[0]

    # ------ explicit per-crate rustc edges (experimental) ------
    #
    # Driven by cargo --unit-graph instead of one monolithic cargo edge per
    # RustLibrary/RustProgram. Gated behind
    # MOZ_NINJA_EXPERIMENTAL_EXPLICIT_RUSTC_EDGES. Each crate unit becomes a
    # `rustc_build` edge; each build script a compile edge plus a
    # `run_build_script` edge whose captured cfgs/links feed the dependent
    # crate's rustc edge. Units shared across edges (same identity) are emitted
    # once.

    def _prefetch_rust_graphs(self):
        """Run every cargo call the explicit-edge path needs -- one
        `--unit-graph` per Rust edge (libraries and programs) plus the
        `cargo metadata` for declared features -- concurrently, each with its
        own CARGO_HOME, so none of them serialize on cargo's package-cache
        lock. Memoized; both emit passes read the results."""
        if getattr(self, "_rust_graphs", None) is not None:
            return
        from mozbuild.backend.ninja.rust_unit_graph import (
            unit_graphs_for_specs,
            vendored_cargo_config,
        )

        substs = self.environment.substs
        topsrcdir = self.environment.topsrcdir
        topobjdir = self._topobjdir
        lib_edges = list(self._rust_libs)
        prog_edges = list(self._rust_programs) + list(self._host_rust_programs)
        specs = [
            self._cargo_spec_dict(lib, mozpath.normsep(self._lib_output_path(lib)))
            for lib in lib_edges
        ]
        for obj in prog_edges:
            kind = "program" if obj.KIND == "target" else "host-program"
            specs.append(self._cargo_program_spec_dict(obj, kind))
        all_edges = lib_edges + prog_edges
        # Write the vendored config once and share it, so the concurrent
        # unit-graph batch and metadata call don't race to write it.
        config_path = vendored_cargo_config(topsrcdir, topobjdir)
        meta_home = mozpath.join(topobjdir, "rust-edges", "cargo-homes", "metadata")
        with ThreadPoolExecutor(max_workers=2) as ex:
            graphs_fut = ex.submit(
                unit_graphs_for_specs, specs, substs, topsrcdir, topobjdir, config_path
            )
            meta_fut = ex.submit(
                self._compute_declared_features, meta_home, config_path
            )
            graphs = graphs_fut.result()
            self._declared_features_cache = meta_fut.result()
        self._rust_graphs = dict(zip(all_edges, graphs))

    def _emit_explicit_rust_library_edges(self, writer, libs):
        self._prefetch_rust_graphs()
        deps_dir = mozpath.join(self._topobjdir, "rust-edges", "deps")
        for lib in libs:
            out = mozpath.normsep(self._lib_output_path(lib))
            cargo_spec = self._cargo_spec_dict(lib, out)
            self._emit_unit_graph(
                writer,
                self._rust_graphs[lib],
                out,
                deps_dir,
                ["staticlib"],
                self._edge_build_env(cargo_spec),
                cargo_spec.get("computed_ldflags"),
                edge_is_ltoable=cargo_spec["is_ltoable"],
                edge_is_gkrust_gtest=cargo_spec["is_gkrust_gtest"],
            )
            self._emit_linkable_alias(writer, lib.basename, out)
            self._rust_lib_outputs[self._rel_n_path(out)] = mozpath.splitext(
                mozpath.basename(out)
            )[0]

    def _unit_ids(self, units):
        """Stable per-unit identities for -Cmetadata, rlib filenames, and
        cross-edge dedup: everything that makes two compilations of the same
        crate distinct (features, profile, host/target, mode, crate kind) plus
        the identities of all (transitive) dependencies. The dependency identity
        is essential: a crate compiled against different feature sets of a dep --
        e.g. one edge resolving `http` with `default` and another without -- must
        not collapse to one rlib, or a dependent gets linked against the wrong
        variant and rustc reports "multiple versions of crate" errors."""
        memo = {}
        visiting = set()

        def compute(i):
            if i in memo:
                return memo[i]
            if i in visiting:
                # Unit graphs are DAGs; this only guards against looping.
                return units[i]["pkg_id"]
            visiting.add(i)
            unit = units[i]
            prof = unit.get("profile", {})
            dep_ids = sorted(compute(d["index"]) for d in unit.get("dependencies", []))
            key = "\x1f".join(
                [
                    unit["pkg_id"],
                    ",".join(sorted(unit.get("features", []))),
                    str(unit.get("platform")),
                    unit.get("mode", ""),
                    ",".join(unit["target"].get("kind", [])),
                    json.dumps(prof, sort_keys=True),
                ]
                + dep_ids
            )
            uid = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            visiting.discard(i)
            memo[i] = uid
            return uid

        return [compute(i) for i in range(len(units))]

    def _unit_output(self, units, i, ids, deps_dir, root_index, root_output):
        unit = units[i]
        kind = unit["target"].get("kind", [])
        name = unit["target"]["name"].replace("-", "_")
        if i == root_index:
            return root_output
        uid = ids[i]
        if unit["mode"] == "run-custom-build":
            return mozpath.join(deps_dir, f"run-build-{uid}.json")
        if "custom-build" in kind:
            # rustc names the build-script exe <crate_name>-<extra-filename>,
            # e.g. build_script_build-<id>.exe (no lib prefix). `name` is
            # already the underscored crate name, so match it. Build scripts
            # are host binaries, so use the host exe suffix.
            ext = self.environment.substs.get("HOST_BIN_SUFFIX", "")
            return mozpath.join(deps_dir, f"{name}-{uid}{ext}")
        if "proc-macro" in kind:
            # Proc-macros are host dynamic libraries loaded by the host rustc.
            pre = self.environment.substs.get("HOST_DLL_PREFIX", "lib")
            ext = self.environment.substs.get("HOST_DLL_SUFFIX", ".so")
            return mozpath.join(deps_dir, f"{pre}{name}-{uid}{ext}")
        return mozpath.join(deps_dir, f"lib{name}-{uid}.rlib")

    def _emit_unit_graph(
        self,
        writer,
        graph,
        root_output,
        deps_dir,
        root_crate_types=None,
        edge_env=None,
        edge_ldflags=None,
        root_link_deps=None,
        edge_is_ltoable=False,
        edge_is_gkrust_gtest=False,
    ):
        emitted = getattr(self, "_explicit_rust_emitted", None)
        if emitted is None:
            emitted = self._explicit_rust_emitted = set()
        units = graph["units"]
        ids = self._unit_ids(units)
        roots = graph.get("roots", [])
        root_index = next(
            (
                r
                for r in roots
                if units[r]["mode"] != "run-custom-build"
                and "custom-build" not in units[r]["target"].get("kind", [])
            ),
            roots[0] if roots else 0,
        )
        for i, unit in enumerate(units):
            output = self._unit_output(units, i, ids, deps_dir, root_index, root_output)
            rel_out = self._rel_n_path(output)
            if rel_out in emitted:
                continue
            emitted.add(rel_out)
            if unit["mode"] == "run-custom-build":
                self._emit_build_script_run(
                    writer, units, i, ids, deps_dir, output, edge_env
                )
            else:
                self._emit_crate_rustc(
                    writer,
                    units,
                    i,
                    ids,
                    deps_dir,
                    output,
                    i == root_index,
                    root_crate_types,
                    edge_ldflags,
                    root_link_deps,
                    edge_is_ltoable,
                    edge_is_gkrust_gtest,
                )

    def _emit_crate_rustc(
        self,
        writer,
        units,
        i,
        ids,
        deps_dir,
        output,
        is_root,
        root_crate_types=None,
        edge_ldflags=None,
        root_link_deps=None,
        edge_is_ltoable=False,
        edge_is_gkrust_gtest=False,
    ):
        unit = units[i]
        externs = []
        bs_outputs = []
        bs_out_dir = None
        dep_inputs = []
        for dep in unit.get("dependencies", []):
            du = units[dep["index"]]
            dout = self._unit_output(units, dep["index"], ids, deps_dir, -1, "")
            if du["mode"] == "run-custom-build":
                bs_outputs.append(self._rel_n_path(dout))
                dep_inputs.append(self._rel_n_path(dout))
                bs_out_dir = mozpath.normsep(
                    mozpath.join(deps_dir, f"out-{ids[dep['index']]}")
                )
            elif any(
                k in ("lib", "rlib", "proc-macro") for k in du["target"].get("kind", [])
            ):
                externs.append([dep["extern_crate_name"], mozpath.normsep(dout)])
                dep_inputs.append(self._rel_n_path(dout))
        kind = unit["target"].get("kind", [])
        if is_root and root_crate_types:
            crate_types = root_crate_types
        elif "proc-macro" in kind:
            # kind is authoritative for proc-macro-ness; force the crate-type so
            # rustc makes the `proc_macro` crate available.
            crate_types = ["proc-macro"]
        else:
            crate_types = unit["target"]["crate_types"]
        name, version = self._pkg_name_version(unit["pkg_id"])
        # Strip build metadata (+...) then split the pre-release (-...) so a
        # dotted pre-release such as "1.0.0-alpha.1" keeps its full PRE value.
        core = version.split("+", 1)[0]
        core, _, pre = core.partition("-")
        vparts = (core.split(".") + ["", "", ""])[:3]
        crate_env = {
            "CARGO_PKG_NAME": name,
            "CARGO_PKG_VERSION": version,
            "CARGO_PKG_VERSION_MAJOR": vparts[0],
            "CARGO_PKG_VERSION_MINOR": vparts[1],
            "CARGO_PKG_VERSION_PATCH": vparts[2],
            "CARGO_PKG_VERSION_PRE": pre,
        }
        # cargo sets CARGO_MANIFEST_DIR for every rustc invocation; crates and
        # build scripts read it at compile time via env!(). Sourced from the
        # cargo metadata manifest path (correct for vendored/registry crates too,
        # unlike dirname(src_path) which points into src/ for libs).
        manifest_dir = self._rust_manifest_dirs().get((name, version))
        if manifest_dir:
            crate_env["CARGO_MANIFEST_DIR"] = manifest_dir
        # CARGO_PKG_AUTHORS/DESCRIPTION/etc. -- clap's #[command(author, about)]
        # and similar derives read them at compile time.
        crate_env.update(self._rust_pkg_env().get((name, version), {}))
        # rustc writes dep-info from cwd=topsrcdir, so this must be an absolute
        # objdir path: program roots (obj.location) are objdir-relative, while
        # library/dep outputs are already absolute.
        depfile = mozpath.splitext(output)[0] + ".d"
        if not os.path.isabs(depfile):
            depfile = mozpath.join(self._topobjdir, depfile)
        depfile = mozpath.normsep(depfile)
        spec = {
            "crate_name": unit["target"]["name"].replace("-", "_"),
            "edition": unit["target"]["edition"],
            "src_path": mozpath.normsep(unit["target"]["src_path"]),
            "crate_types": crate_types,
            "env": crate_env,
            "profile": unit.get("profile", {}),
            "platform": unit.get("platform"),
            "features": unit.get("features", []),
            "externs": externs,
            "extern_dirs": [mozpath.normsep(deps_dir)],
            "metadata": ids[i],
            "is_dep": not unit["pkg_id"].startswith("path+"),
            "out_dir": mozpath.normsep(mozpath.dirname(output)),
            # rustc emits dep-info to `depfile` (compose_rustc) and rustc_build
            # rewrites it into the ninja depfile so editing any module .rs (not
            # just the crate root) re-triggers this edge. depfile_target is the
            # edge's ninja output, the sole target ninja's `deps = gcc` expects.
            "depfile": depfile,
            "depfile_target": self._rel_n_path(output),
            # Edge-level: every target unit of an ltoable edge gets the
            # -Clinker-plugin-lto/pgo overlay, not just the root.
            "is_ltoable": edge_is_ltoable,
            "build_script_outputs": bs_outputs,
            "build_script_out_dir": bs_out_dir,
            "declared_features": self._rust_declared_features().get(
                (name, version), []
            ),
            "check_cfg": self._rust_check_cfg().get((name, version), []),
        }
        if is_root:
            spec["output_file"] = mozpath.normsep(output)
            spec["is_library_root"] = edge_is_ltoable
            spec["is_gkrust_gtest"] = edge_is_gkrust_gtest
            # This edge's own LDFLAGS (the cargo path's per-edge computed_ldflags
            # rather than the global fallback).
            if edge_ldflags:
                spec["computed_ldflags"] = edge_ldflags
            # A final link (program/dylib) also needs the link-search paths every
            # build script in the closure emitted -- e.g. nss-rs points -L at
            # <objdir>/security where nss3.lib lives. cargo accumulates these for
            # the binary link; rlib compiles ignore -L so only the root needs it.
            if set(crate_types) & {"bin", "dylib", "cdylib"}:
                link_outputs = [
                    self._rel_n_path(self._unit_output(units, j, ids, deps_dir, -1, ""))
                    for j, u in enumerate(units)
                    if u["mode"] == "run-custom-build"
                ]
                spec["link_search_outputs"] = link_outputs
                dep_inputs += link_outputs
            # USE_LIBS / extra_link_deps producers (e.g. NSS), so the link is
            # sequenced behind them instead of racing on a clobber build.
            if root_link_deps:
                dep_inputs += root_link_deps
        spec_path = output + ".rustc-spec.json"
        with self._write_file(spec_path) as fh:
            json.dump(spec, fh, indent=2, sort_keys=True)
        order_only = [".ninja-rust-prereqs"]
        order_only.extend(self._clang_plugin_order_only())
        writer.build(
            self._rel_n_path(output),
            "rustc_build",
            inputs=[self._rel_n_path(unit["target"]["src_path"])],
            implicit=[self._rel_n_path(spec_path)] + dep_inputs,
            order_only=order_only,
            variables={
                "spec": self._rel_n_path(spec_path),
                "depfile": self._rel_n_path(depfile),
            },
        )

    def _edge_build_env(self, cargo_spec):
        """The Firefox compiler/tool env (compose_env) a build script for this
        edge runs under in the cargo path -- CC/CXX/AR and CFLAGS per triple,
        LIBCLANG_PATH, bindgen args, and so on -- which cc-rs and bindgen build
        scripts read. Computed per edge so the per-edge CFLAGS are right."""
        from mozbuild.action._rust_env import compose_env

        return compose_env(
            cargo_spec,
            self.environment.substs,
            dict(os.environ),
            self.environment.topsrcdir,
            self._topobjdir,
        )

    def _program_link_deps(self, obj):
        """Implicit deps mirroring the cargo-path program edge (see the cargo
        branch of _emit_explicit_rust_program_edges): a RustProgram links its
        USE_LIBS outputs and extra_link_deps, so the explicit-edge root link
        must be sequenced behind their producers (e.g. NSS for nss3.lib) or a
        clobber build races them."""
        _, shared_libs, _, static_libs = self._expand_libs(obj)
        deps = [
            self._rel_n_path(self._lib_output_path(lib))
            for lib in static_libs + shared_libs
        ]
        for dep in getattr(obj, "extra_link_deps", ()):
            deps.append(self._rel_n_path(mozpath.normsep(dep.full_path)))
        return deps

    def _emit_build_script_run(
        self, writer, units, i, ids, deps_dir, output, edge_env=None
    ):
        unit = units[i]
        exe = None
        for dep in unit.get("dependencies", []):
            du = units[dep["index"]]
            if du["mode"] == "build" and "custom-build" in du["target"].get("kind", []):
                exe = self._unit_output(units, dep["index"], ids, deps_dir, -1, "")
        out_dir = mozpath.join(deps_dir, f"out-{ids[i]}")
        substs = self.environment.substs
        # Start from the Firefox compiler/tool env this edge's cargo build would
        # use, then overlay the per-script vars cargo provides. cc-rs build
        # scripts (mozjs_sys, swgl) read CC/CXX/CFLAGS from it to pick clang-cl
        # over autodetected MSVC; bindgen users read LIBCLANG_PATH.
        env = dict(edge_env or {})
        env.update({
            "TARGET": str(unit.get("platform") or substs.get("RUST_HOST_TARGET", "")),
            "HOST": substs.get("RUST_HOST_TARGET", ""),
            "OPT_LEVEL": str(unit.get("profile", {}).get("opt_level", "0")),
            "RUSTC": substs.get("RUSTC", ""),
            "RUSTDOC": substs.get("RUSTDOC", ""),
            "CARGO": substs.get("CARGO", ""),
            "NUM_JOBS": "1",
            "PROFILE": "debug" if substs.get("MOZ_DEBUG_RUST") else "release",
            "DEBUG": (
                "true" if unit.get("profile", {}).get("debug_assertions") else "false"
            ),
            # In-tree build scripts (e.g. nss_build_common) detect a Gecko
            # build by the presence of MOZ_TOPOBJDIR.
            "MOZ_TOPOBJDIR": mozpath.normsep(self._topobjdir),
        })
        # MSVC header/library search paths the C compiler reads from the env
        # (cc-rs scripts declare them via rerun-if-env-changed). Source them from
        # config, like CC/CFLAGS, rather than the ambient shell: this puts them in
        # the spec, so an SDK/toolchain change (config -> config.status ->
        # regenerated spec) re-runs the affected scripts, matching cargo.
        for var in ("INCLUDE", "LIB"):
            val = substs.get(var)
            if val:
                env[var] = val
        for f in unit.get("features", []):
            env[f"CARGO_FEATURE_{f.upper().replace('-', '_')}"] = "1"
        triple = unit.get("platform") or substs.get("RUST_HOST_TARGET", "")
        env.update(self._rust_target_cfg(triple))
        name, version = self._pkg_name_version(unit["pkg_id"])
        vparts = (version.split(".") + ["", "", ""])[:3]
        manifest_dir = self._rust_manifest_dirs().get((
            name,
            version,
        )) or mozpath.dirname(mozpath.normsep(unit["target"]["src_path"]))
        env["CARGO_PKG_NAME"] = name
        env["CARGO_PKG_VERSION"] = version
        env["CARGO_PKG_VERSION_MAJOR"] = vparts[0]
        env["CARGO_PKG_VERSION_MINOR"] = vparts[1]
        env["CARGO_PKG_VERSION_PATCH"] = vparts[2]
        env["CARGO_MANIFEST_DIR"] = manifest_dir
        # Generated sources a build script writes into OUT_DIR, declared so
        # ninja tracks them as outputs of this edge and sequences the dependent
        # crate after them. The known set per crate lives in cargo_extra_outputs.
        generated = [
            mozpath.join(out_dir, rel) for rel in cargo_extra_outputs.get(name, [])
        ]
        spec = {
            "exe": mozpath.normsep(exe) if exe else "",
            "out_dir": mozpath.normsep(out_dir),
            "manifest_dir": manifest_dir,
            "env": env,
            "output": mozpath.normsep(output),
            # run_rust_build_script writes the script's `rerun-if-changed` files
            # here as a ninja depfile, so a tracked input change re-runs it.
            "depfile": mozpath.normsep(output + ".d"),
            "depfile_target": self._rel_n_path(output),
        }
        spec_path = output + ".run-build-spec.json"
        with self._write_file(spec_path) as fh:
            json.dump(spec, fh, indent=2, sort_keys=True)
        implicit = [self._rel_n_path(spec_path)]
        if exe:
            implicit.append(self._rel_n_path(exe))
        order_only = [".ninja-rust-prereqs"]
        order_only.extend(self._clang_plugin_order_only())
        outputs = [self._rel_n_path(output)] + [self._rel_n_path(g) for g in generated]
        writer.build(
            outputs,
            "run_rust_build_script",
            implicit=implicit,
            order_only=order_only,
            variables={
                "spec": self._rel_n_path(spec_path),
                "depfile": self._rel_n_path(output + ".d"),
            },
        )

    def _rust_declared_features(self):
        """pkg_id -> declared feature names (for --check-cfg). Populated by the
        concurrent prefetch; computed lazily here if the prefetch didn't run."""
        cache = getattr(self, "_declared_features_cache", None)
        if cache is None:
            cache = self._declared_features_cache = self._compute_declared_features()
        return cache

    def _rust_manifest_dirs(self):
        """pkg_id -> manifest directory (for CARGO_MANIFEST_DIR), populated from
        the same cargo metadata pass as the declared features."""
        if getattr(self, "_manifest_dir_cache", None) is None:
            self._rust_declared_features()
        return getattr(self, "_manifest_dir_cache", None) or {}

    def _rust_pkg_env(self):
        """pkg_id -> CARGO_PKG_* env (authors, description, license, ...), from
        the same cargo metadata pass as the declared features."""
        if getattr(self, "_pkg_env_cache", None) is None:
            self._rust_declared_features()
        return getattr(self, "_pkg_env_cache", None) or {}

    def _rust_check_cfg(self):
        """pkg_id -> --check-cfg names a crate declares in its Cargo.toml
        `[lints.rust.unexpected_cfgs] check-cfg` (cargo passes these; without
        them rustc warns `unexpected cfg`). Populated by the metadata pass."""
        if getattr(self, "_check_cfg_cache", None) is None:
            self._rust_declared_features()
        return getattr(self, "_check_cfg_cache", None) or {}

    def _lints_check_cfg(self, manifest_path):
        """The crate's `[lints.rust.unexpected_cfgs] check-cfg` list, read from
        its Cargo.toml. Firefox's workspace root declares no workspace lints, so
        `[lints] workspace = true` crates resolve to nothing (the text prefilter
        skips them) -- matching cargo for this tree."""
        try:
            text = Path(manifest_path).read_text(encoding="utf-8")
        except OSError:
            return []
        if "unexpected_cfgs" not in text:
            return []
        import toml

        try:
            data = toml.loads(text)
        except Exception:
            return []
        rust = (data.get("lints") or {}).get("rust") or {}
        unexpected = rust.get("unexpected_cfgs") or {}
        if isinstance(unexpected, dict):
            return list(unexpected.get("check-cfg", []))
        return []

    def _compute_declared_features(self, cargo_home=None, config_path=None):
        """Run one `cargo metadata` and return pkg_id -> declared feature names.
        Optional dependencies are implicit features, so they are included. Also
        populates `self._manifest_dir_cache` (pkg_id -> manifest dir) and
        `self._pkg_env_cache` (pkg_id -> the CARGO_PKG_* env cargo sets, which
        clap's `#[command(author/about)]` and others read at compile time) from
        the same pass. `cargo_home`/`config_path` mirror unit_graph_for_spec so
        this can run concurrently with the unit-graph calls without contention."""
        cache = {}
        manifest_dirs = {}
        pkg_env = {}
        check_cfg = {}
        cargo = self.environment.substs.get("CARGO")
        if not cargo:
            self._manifest_dir_cache = manifest_dirs
            self._pkg_env_cache = pkg_env
            self._check_cfg_cache = check_cfg
            return cache
        argv = [cargo, "metadata", "--format-version", "1", "--frozen"]
        if config_path:
            argv += ["--config", config_path]
        env = dict(os.environ)
        if cargo_home:
            Path(cargo_home).mkdir(parents=True, exist_ok=True)
            env["CARGO_HOME"] = cargo_home
        proc = subprocess.run(
            argv,
            cwd=self.environment.topsrcdir,
            env=env,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout.decode("utf-8"))
            for p in data.get("packages", []):
                feats = set(p.get("features", {}).keys())
                for d in p.get("dependencies", []):
                    if d.get("optional") and d.get("name"):
                        feats.add(d["name"])
                # Key by (name, version), not the raw pkg id: a path-dependency's
                # id bakes in its absolute source path, which can differ between
                # this `cargo metadata` call and the per-edge `--unit-graph` call
                # (e.g. CI's aliased srcdir), breaking a raw-id lookup. (name,
                # version) is identical across both.
                key = self._pkg_name_version(p["id"])
                cache[key] = sorted(feats)
                manifest_path = p.get("manifest_path")
                if manifest_path:
                    manifest_dirs[key] = mozpath.dirname(mozpath.normsep(manifest_path))
                    check_cfg[key] = self._lints_check_cfg(manifest_path)
                pkg_env[key] = {
                    "CARGO_PKG_AUTHORS": ":".join(p.get("authors") or []),
                    "CARGO_PKG_DESCRIPTION": p.get("description") or "",
                    "CARGO_PKG_HOMEPAGE": p.get("homepage") or "",
                    "CARGO_PKG_REPOSITORY": p.get("repository") or "",
                    "CARGO_PKG_LICENSE": p.get("license") or "",
                    "CARGO_PKG_LICENSE_FILE": p.get("license_file") or "",
                    "CARGO_PKG_RUST_VERSION": p.get("rust_version") or "",
                    "CARGO_PKG_README": p.get("readme") or "",
                }
        self._manifest_dir_cache = manifest_dirs
        self._pkg_env_cache = pkg_env
        self._check_cfg_cache = check_cfg
        return cache

    def _rust_target_cfg(self, triple):
        """CARGO_CFG_* env a build script sees for a target triple, from a
        cached `rustc --print cfg --target <triple>`."""
        cache = getattr(self, "_target_cfg_cache", None)
        if cache is None:
            cache = self._target_cfg_cache = {}
        if triple in cache:
            return cache[triple]
        env = {}
        rustc = self.environment.substs.get("RUSTC")
        if rustc and triple:
            proc = subprocess.run(
                [rustc, "--print", "cfg", "--target", triple],
                capture_output=True,
                check=False,
            )
            if proc.returncode == 0:
                kv = defaultdict(list)
                for line in proc.stdout.decode("utf-8").splitlines():
                    line = line.strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        kv[k.strip()].append(v.strip().strip('"'))
                    elif line:
                        env[f"CARGO_CFG_{line.upper()}"] = ""
                for k, vs in kv.items():
                    env[f"CARGO_CFG_{k.upper()}"] = ",".join(vs)
        cache[triple] = env
        return env

    def _pkg_name_version(self, pkg_id):
        head, _, tail = pkg_id.partition("#")
        if "@" in tail:
            pkg_name, _, version = tail.partition("@")
            return pkg_name, version
        base = head.split("+", 1)[-1].rstrip("/")
        return base.rsplit("/", 1)[-1], tail

    def _cargo_program_spec_dict(self, obj, kind):
        target_subst = "RUST_TARGET" if kind == "program" else "RUST_HOST_TARGET"
        return {
            "kind": kind,
            "subcommand": "build",
            "manifest_path": mozpath.normsep(obj.cargo_file),
            "output_path": mozpath.normsep(obj.location),
            "features": list(obj.features) if obj.features else [],
            "target_triple": self.environment.substs.get(target_subst, ""),
            "is_megazord": False,
            "is_gkrust_gtest": False,
            "is_ltoable": False,
            "extra_rustcflags": [],
            "cargo_extra_cli_flags": ["--bin", obj.name],
            "output_category": obj.output_category,
            "relsrcdir": obj.relsrcdir,
            "relobjdir": obj.relobjdir,
            "computed_cflags": " ".join(
                self._computed_flag_list(obj.relobjdir, "CFLAGS")
            ),
            "computed_cxxflags": " ".join(
                self._computed_flag_list(obj.relobjdir, "CXXFLAGS")
            ),
            "computed_host_cflags": " ".join(
                self._computed_flag_list(obj.relobjdir, "HOST_CFLAGS")
            ),
            "computed_host_cxxflags": " ".join(
                self._computed_flag_list(obj.relobjdir, "HOST_CXXFLAGS")
            ),
            "computed_ldflags": " ".join(
                self._computed_flag_list(obj.relobjdir, "LDFLAGS")
            ),
        }

    def _write_cargo_program_spec(self, path, obj, kind):
        """Write a cargo build spec for a RustProgram or HostRustProgram."""
        with self._write_file(path) as fh:
            json.dump(
                self._cargo_program_spec_dict(obj, kind), fh, indent=2, sort_keys=True
            )

    def _emit_explicit_rust_program_edges(self, writer, programs):
        self._prefetch_rust_graphs()
        deps_dir = mozpath.join(self._topobjdir, "rust-edges", "deps")
        for obj in programs:
            out = mozpath.normsep(obj.location)
            kind = "program" if obj.KIND == "target" else "host-program"
            cargo_spec = self._cargo_program_spec_dict(obj, kind)
            self._emit_unit_graph(
                writer,
                self._rust_graphs[obj],
                out,
                deps_dir,
                edge_env=self._edge_build_env(cargo_spec),
                edge_ldflags=cargo_spec.get("computed_ldflags"),
                root_link_deps=self._program_link_deps(obj),
                edge_is_ltoable=False,
            )
            if obj.installed:
                self._emit_linkable_alias(
                    writer, obj.name, self._rust_program_install_dest(obj)
                )
            else:
                self._emit_linkable_alias(writer, obj.name, out)
            self._rust_lib_outputs[self._rel_n_path(out)] = mozpath.splitext(
                mozpath.basename(out)
            )[0]

    def _emit_rust_program_statements(self, writer):
        if not self._rust_programs and not self._host_rust_programs:
            return
        writer.newline()
        writer.comment("------ rust programs ------")
        writer.newline()
        programs = sorted(
            self._rust_programs + self._host_rust_programs,
            key=lambda p: len(p.location),
        )
        if self.environment.substs.get("MOZ_NINJA_EXPERIMENTAL_EXPLICIT_RUSTC_EDGES"):
            self._emit_explicit_rust_program_edges(writer, programs)
            return
        for obj in programs:
            out = mozpath.normsep(obj.location)
            depfile = mozpath.splitext(out)[0] + ".d"
            spec_path = mozpath.join(obj.objdir, ".cargo-program-spec.json")
            kind = "program" if obj.KIND == "target" else "host-program"
            self._write_cargo_program_spec(spec_path, obj, kind)
            # cargo invokes the linker which pulls in `USE_LIBS`
            # outputs (and any `EXTRA_LINK_DEPS`). Mirror what
            # `_emit_program_statements` does for non-rust programs so
            # the cargo edge is sequenced behind those producers
            # instead of racing them. The make backend reaches the
            # same outcome via `_process_linked_libraries` populating
            # `_compile_graph` for `RustProgram` (recursivemake.py).
            _, shared_libs, _, static_libs = self._expand_libs(obj)
            implicit_deps = [self._rel_n_path(spec_path)]
            for static_lib in static_libs:
                implicit_deps.append(
                    self._rel_n_path(self._lib_output_path(static_lib))
                )
            for shared_lib in shared_libs:
                implicit_deps.append(
                    self._rel_n_path(self._lib_output_path(shared_lib))
                )
            for dep in getattr(obj, "extra_link_deps", ()):
                implicit_deps.append(self._rel_n_path(mozpath.normsep(dep.full_path)))
            order_only = [".ninja-rust-prereqs"]
            order_only.extend(self._gkrust_order_only())
            order_only.extend(self._clang_plugin_order_only())
            writer.build(
                self._rel_n_path(out),
                "cargo_build",
                order_only=order_only,
                implicit=implicit_deps,
                variables={
                    "spec": self._rel_n_path(spec_path),
                    "depfile": self._rel_n_path(depfile),
                },
            )
            if obj.installed:
                self._emit_linkable_alias(
                    writer, obj.name, self._rust_program_install_dest(obj)
                )
            else:
                self._emit_linkable_alias(writer, obj.name, out)
            self._rust_lib_outputs[self._rel_n_path(out)] = mozpath.splitext(
                mozpath.basename(out)
            )[0]

    def _persist_rust_outputs_manifest(self):
        """Persist `_rust_lib_outputs` for `_record_ninja_log_markers` so a
        separate `./mach build` invocation can still classify cargo edges
        and locate cargo-timings .html files. Called after both library
        and program emit passes so programs are included."""
        manifest_path = mozpath.join(self._topobjdir, ".ninja-rust-libs.json")
        with self._write_file(manifest_path) as fh:
            json.dump(self._rust_lib_outputs, fh, indent=2, sort_keys=True)

    def _write_cargo_test_spec(self, path, obj):
        cargo_file = mozpath.join(obj.srcdir, "Cargo.toml")
        extra = ["--no-fail-fast"]
        for name in obj.names:
            extra.extend(["-p", name])
        spec = {
            "kind": "test",
            "subcommand": "test",
            "manifest_path": mozpath.normsep(cargo_file),
            "output_path": "",
            "features": list(obj.features) if obj.features else [],
            "target_triple": self.environment.substs.get("RUST_TARGET", ""),
            "is_megazord": False,
            "is_gkrust_gtest": False,
            "is_ltoable": False,
            "extra_rustcflags": [],
            "cargo_extra_cli_flags": extra,
            "output_category": obj.output_category,
            "relsrcdir": obj.relsrcdir,
            "relobjdir": obj.relobjdir,
            "computed_cflags": "",
            "computed_cxxflags": "",
            "computed_host_cflags": "",
            "computed_host_cxxflags": "",
            "computed_ldflags": "",
        }
        with self._write_file(path) as fh:
            json.dump(spec, fh, indent=2, sort_keys=True)

    def _emit_rust_tests_statements(self, writer):
        if not self._rust_tests:
            return
        if not self.environment.substs.get("MOZ_RUST_TESTS"):
            return
        writer.newline()
        writer.comment("------ rust tests ------")
        writer.newline()
        stamps = []
        for obj in self._rust_tests:
            cargo_spec = mozpath.join(obj.objdir, ".cargo-tests-spec.json")
            self._write_cargo_test_spec(cargo_spec, obj)
            stamp = mozpath.join(obj.objdir, ".cargo-tests.stamp")
            order_only = [".ninja-rust-prereqs"]
            order_only.extend(self._gkrust_order_only())
            order_only.extend(self._clang_plugin_order_only())
            self._emit_run_edge(
                writer,
                stamp,
                [
                    {
                        "module": "mozbuild.action.cargo_build",
                        "args": ["--spec", mozpath.normsep(cargo_spec)],
                    }
                ],
                f"CARGO_TEST {self._rel_n_path(stamp)}",
                implicit=[self._rel_n_path(cargo_spec)],
                order_only=order_only,
                stamp=True,
                pool="cargo",
            )
            stamps.append(self._rel_n_path(stamp))
        writer.build("rusttests", "phony", inputs=stamps)
