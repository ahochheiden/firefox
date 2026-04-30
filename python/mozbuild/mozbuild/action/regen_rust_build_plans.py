# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Regenerate per-RustLibrary `cargo build --build-plan` JSON files.

Reads the manifest NinjaBackend writes at backend time
(`<topobjdir>/.ninja-rust-libs-manifest.json`), invokes mozmake's
`force-cargo-library-build-plan` target for each RustLibrary so
rust.mk's full env setup applies, and dumps the per-lib plan JSON
in-tree at `python/mozbuild/mozbuild/rust_build_plans/<basename>.json`.

This is a one-off / on-demand tool; users run it after updating
Cargo.lock. The committed plans are the source of truth — the
NinjaBackend reads them at backend time without invoking cargo
itself.

Usage:
    ./mach python -m mozbuild.action.regen_rust_build_plans \\
        --topobjdir <topobjdir>
"""

import argparse
import json
import os
import subprocess
import sys


_DEFAULT_OUTPUT_REL = os.path.join(
    "python", "mozbuild", "mozbuild", "rust_build_plans"
)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--topobjdir", default=None)
    parser.add_argument("--topsrcdir", default=None)
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override committed plan dir (default: "
        "<topsrcdir>/python/mozbuild/mozbuild/rust_build_plans).",
    )
    parser.add_argument(
        "--make",
        default=None,
        help="Path to mozmake binary (defaults to substs.GMAKE).",
    )
    args = parser.parse_args(argv)

    topobjdir = args.topobjdir or os.environ.get("MOZ_TOPOBJDIR")
    if not topobjdir:
        sys.stderr.write("--topobjdir required (or MOZ_TOPOBJDIR set).\n")
        return 2
    # mozmake runs each per-RustLibrary recipe from the cargo_dir cwd,
    # not from where the user invoked us. Relative output / topobjdir
    # paths get reinterpreted there. Force absolute now.
    topobjdir = os.path.abspath(topobjdir)

    manifest_path = os.path.join(topobjdir, ".ninja-rust-libs-manifest.json")
    if not os.path.exists(manifest_path):
        sys.stderr.write(
            f"manifest not found at {manifest_path}.\n"
            f"Run `./mach build-backend -b Ninja` first.\n"
        )
        return 1
    with open(manifest_path, encoding="utf-8") as f:
        libs = json.load(f)

    cs_path = os.path.join(topobjdir, "config.status")
    g = {"__name__": "config_status"}
    with open(cs_path, encoding="utf-8") as f:
        exec(compile(f.read(), cs_path, "exec"), g)
    substs = g.get("substs", {})
    make = args.make or substs.get("GMAKE") or substs.get("MAKE") or "make"

    topsrcdir = args.topsrcdir or substs.get("top_srcdir") or substs.get("TOP_SRCDIR")
    if not topsrcdir:
        # Fallback: derive from config.status's `topsrcdir` global if set.
        topsrcdir = g.get("topsrcdir")
    if not topsrcdir:
        sys.stderr.write(
            "could not resolve topsrcdir; pass --topsrcdir explicitly.\n"
        )
        return 2
    topsrcdir = os.path.abspath(topsrcdir)

    output_dir = os.path.abspath(
        args.output_dir or os.path.join(topsrcdir, _DEFAULT_OUTPUT_REL)
    )

    os.makedirs(output_dir, exist_ok=True)

    failures = []
    for entry in libs:
        basename = entry["basename"]
        cargo_dir = entry["cargo_dir"]
        out_path = os.path.join(output_dir, f"{basename}.json").replace(
            os.sep, "/"
        )
        sys.stderr.write(f"==> {basename} ({cargo_dir})\n")
        # Per-RustLibrary cargo target dir so output paths don't collide
        # across libs (each plan references its own debug/release/deps).
        per_lib_target_dir = os.path.abspath(
            os.path.join(topobjdir, ".ninja-rust", basename)
        ).replace(os.sep, "/")
        cmd = [
            make,
            "-C",
            cargo_dir,
            "force-cargo-library-build-plan",
            f"RUST_BUILD_PLAN_OUT={out_path}",
            f"CARGO_TARGET_DIR={per_lib_target_dir}",
        ]
        rc = subprocess.call(cmd)
        if rc != 0:
            failures.append(basename)
            sys.stderr.write(f"  FAILED ({rc})\n")
            continue
        with open(out_path, encoding="utf-8") as f:
            plan = json.load(f)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, sort_keys=True)

    if failures:
        sys.stderr.write(f"\n{len(failures)} RustLibrary plan(s) failed:\n")
        for n in failures:
            sys.stderr.write(f"  {n}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
