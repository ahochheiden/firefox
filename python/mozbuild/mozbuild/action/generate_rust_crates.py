# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run `cargo metadata` for a RustLibrary's Cargo.toml and emit a
normalized JSON describing every (package, target) compile invocation
in the resolve graph.

Plan B's later phases consume the aggregated `rust_crates.json` to emit
one ninja `rustc` edge per crate. This module is the data-plumbing
foundation; nothing in the build graph depends on its output yet.

Usage as a CLI:

    python -m mozbuild.action.generate_rust_crates \\
        --root-basename gkrust \\
        --manifest-path /path/to/Cargo.toml \\
        --target x86_64-pc-windows-msvc \\
        --features feat_a,feat_b \\
        --output rust_crates_gkrust.json

Usage from Python (e.g. NinjaBackend):

    from mozbuild.action import generate_rust_crates as grc
    entry = grc.run_for_library(
        root_basename="gkrust",
        manifest_path="...",
        target="x86_64-pc-windows-msvc",
        features=["feat_a", "feat_b"],
        cargo="cargo",
    )
"""

import argparse
import json
import os
import subprocess
import sys


def _run_cargo_metadata(manifest_path, target, features, cargo):
    cmd = [
        cargo,
        "metadata",
        "--format-version",
        "1",
        "--frozen",
        "--offline",
        "--manifest-path",
        manifest_path,
    ]
    if target:
        cmd.extend(["--filter-platform", target])
    if features:
        cmd.extend(["--features", ",".join(features)])
    out = subprocess.check_output(cmd, encoding="utf-8")
    return json.loads(out)


def _normalize(meta, root_basename, manifest_path, target_triple, features):
    """Walk cargo's resolve graph and emit one entry per compile target.

    Cargo metadata gives us `packages` (every package in the graph) and
    `resolve.nodes` (the resolved active dep set per package, including
    feature unification). We cross those: each package contributes one
    entry per `target` (lib/bin/proc-macro/custom-build/...), with
    active features from the resolve node and resolved dep edges from
    the resolve node's `deps` array."""
    workspace_members = set(meta.get("workspace_members", ()))
    resolve = meta.get("resolve") or {}
    nodes_by_id = {n["id"]: n for n in resolve.get("nodes", ())}

    crates = {}
    for pkg in meta.get("packages", ()):
        pkg_id = pkg["id"]
        node = nodes_by_id.get(pkg_id, {})
        active_features = list(node.get("features") or ())
        deps = []
        for dep in node.get("deps", ()):
            for dep_kind in dep.get("dep_kinds") or ({},):
                deps.append(
                    {
                        "name": dep["name"],
                        "package_id": dep["pkg"],
                        "kind": dep_kind.get("kind"),
                        "target": dep_kind.get("target"),
                    }
                )
        for tgt in pkg.get("targets", ()):
            kinds = list(tgt.get("kind") or ())
            crate_types = list(tgt.get("crate_types") or ())
            entry_id = "{}::{}::{}".format(pkg_id, tgt["name"], ",".join(kinds))
            crates[entry_id] = {
                "name": tgt["name"],
                "package_id": pkg_id,
                "package_name": pkg["name"],
                "package_version": pkg["version"],
                "edition": tgt.get("edition") or pkg.get("edition") or "2015",
                "src_path": tgt.get("src_path"),
                "kinds": kinds,
                "crate_types": crate_types,
                "manifest_path": pkg["manifest_path"],
                "features": active_features,
                "is_proc_macro": "proc-macro" in crate_types,
                "is_build_script": "custom-build" in kinds,
                "is_workspace_member": pkg_id in workspace_members,
                "deps": deps,
            }

    return {
        "root_basename": root_basename,
        "manifest_path": manifest_path,
        "workspace_root": meta.get("workspace_root"),
        "target_triple": target_triple,
        "features": list(features),
        "root_package_id": resolve.get("root"),
        "target_directory": meta.get("target_directory"),
        "crates": crates,
    }


def run_for_library(*, root_basename, manifest_path, target, features, cargo=None):
    """Public entry point for in-process callers (e.g. NinjaBackend).

    Returns the normalized dict for one RustLibrary. Caller aggregates
    multiple calls into a single `rust_crates.json`."""
    cargo = cargo or os.environ.get("CARGO") or "cargo"
    meta = _run_cargo_metadata(manifest_path, target, features, cargo)
    return _normalize(meta, root_basename, manifest_path, target, features)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-basename", required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--target", default=None)
    parser.add_argument(
        "--features",
        default="",
        help="Comma-separated list of features to enable for the metadata query.",
    )
    parser.add_argument("--cargo", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    features = [f for f in args.features.split(",") if f]
    entry = run_for_library(
        root_basename=args.root_basename,
        manifest_path=args.manifest_path,
        target=args.target,
        features=features,
        cargo=args.cargo,
    )
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
