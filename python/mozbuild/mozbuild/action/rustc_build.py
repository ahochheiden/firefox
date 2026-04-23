# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run rustc for one explicit per-crate edge described by a per-unit spec.

Usage:
    python -m mozbuild.action.rustc_build --spec <path> [--print-only]

The spec JSON is written by NinjaBackend from `cargo --unit-graph`. Any
build-script outputs this crate depends on (referenced by
`build_script_outputs`) are read at run time and folded into the rustc argv
(cfgs, link libs/search, rustc-env), since a build script's directives apply
to the crate whose script emitted them and are only known after it runs.

`--print-only` writes the composed argv to stdout and exits without running
rustc. Used by the validation harness to diff against `cargo build -v`.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mozfile import json

from mozbuild.action._rust_env import compose_rustc


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rewrite_depfile(dpath, target, topsrcdir):
    """Rewrite rustc's raw dep-info into a depfile ninja's `deps = gcc` accepts:
    one target -- the edge's ninja (topobjdir-relative) output -- with the source
    prerequisites. rustc emits multiple output targets and records prereqs
    relative to its cwd (topsrcdir); collapse to the single target ninja expects
    and make the prereqs absolute so ninja resolves them from its own build root."""
    from mozbuild.makeutil import Rule, read_dep_makefile

    if not Path(dpath).exists():
        return
    deps = set()
    with Path(dpath).open(encoding="utf-8") as fh:
        for rule in read_dep_makefile(fh):
            for dep in rule.dependencies():
                if not os.path.isabs(dep):
                    dep = os.path.join(topsrcdir, dep)
                deps.add(dep.replace("\\", "/"))
    deps.discard(target)
    with Path(dpath).open("w", encoding="utf-8", newline="\n") as fh:
        Rule([target]).add_dependencies(sorted(deps)).dump(fh)


def _merge_build_script(paths):
    merged = {
        "cfgs": [],
        "check_cfg": [],
        "env": {},
        "link_libs": [],
        "link_search": [],
    }
    for p in paths:
        if not Path(p).exists():
            continue
        data = _load(p)
        merged["cfgs"].extend(data.get("cfgs", []))
        merged["check_cfg"].extend(data.get("check_cfg", []))
        merged["link_libs"].extend(data.get("link_libs", []))
        merged["link_search"].extend(data.get("link_search", []))
        merged["env"].update(data.get("env", {}))
    return merged


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--print-only", action="store_true")
    args = ap.parse_args(argv)

    spec = _load(args.spec)

    import buildconfig

    substs = buildconfig.substs
    topsrcdir = buildconfig.topsrcdir
    topobjdir = buildconfig.topobjdir

    bs = _merge_build_script(spec.get("build_script_outputs", []))
    # A final-link unit also needs the link-search paths every build script in
    # its closure emitted (e.g. nss-rs's <objdir>/security for nss3.lib). Only
    # link_search is folded -- link_libs propagate via rlib metadata, and
    # cfgs/env apply only to each script's own crate.
    seen = set(bs["link_search"])
    for path in spec.get("link_search_outputs", []):
        if not Path(path).exists():
            continue
        for ls in _load(path).get("link_search", []):
            if ls not in seen:
                seen.add(ls)
                bs["link_search"].append(ls)
    rustc_argv, env = compose_rustc(
        spec, substs, os.environ, topsrcdir, topobjdir, bs=bs
    )

    if args.print_only:
        for a in rustc_argv:
            print(a)
        return 0

    out_dir = spec.get("out_dir")
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    # BUILDSTATUS markers let mach's build monitor capture and color these
    # edges as Rust work, matching the cargo path.
    label = spec.get("crate_name", "rust")
    print(f"BUILDSTATUS START_Rust {label}", flush=True)
    rc = subprocess.call(rustc_argv, env=env, cwd=topsrcdir)
    print(f"BUILDSTATUS END_Rust {label}", flush=True)
    if rc == 0 and spec.get("depfile") and spec.get("depfile_target"):
        _rewrite_depfile(spec["depfile"], spec["depfile_target"], topsrcdir)
    return rc


if __name__ == "__main__":
    sys.exit(main())
