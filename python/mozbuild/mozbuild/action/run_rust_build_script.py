# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run a cargo build script and capture its output for explicit rustc edges.

A build script (build.rs compiled to an executable) is run with the env cargo
provides, its stdout parsed for `cargo:`/`cargo::` directives, and the result
written as JSON for the dependent crate's rustc edge to fold in (cfgs, link
libs/search, rustc-env). OUT_DIR is created first so the script can write
generated sources into it.

Usage: python -m mozbuild.action.run_rust_build_script --spec <path>
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mozfile import json


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _expand_watch(path):
    """Yield the files cargo's fingerprint would watch for `path`: a file is
    itself; a directory is itself (so adds/removes show via its mtime) plus every
    file under it (so content changes show), matching cargo's recursive mtime."""
    if os.path.isdir(path):
        yield path
        for root, _dirs, files in os.walk(path):
            for name in files:
                yield os.path.join(root, name)
    else:
        yield path


def _write_depfile(spec, parsed):
    """Wire the build script's file dependencies into ninja as the run edge's
    depfile, matching cargo's rerun fingerprint: the declared `rerun-if-changed`
    paths (files watched directly, directories expanded recursively), or -- when
    the script declares none -- the whole package directory, which is cargo's
    default. Paths are relative to the script's cwd (its manifest dir), so they
    are made absolute for ninja to resolve from its build root.

    `rerun-if-env-changed` has no ninja equivalent (ninja tracks files, not env);
    env vars sourced from the build config travel in the spec, so configure
    changing them re-runs the edge, but ambient env vars are not tracked here."""
    from mozbuild.makeutil import Rule

    dpath = spec.get("depfile")
    target = spec.get("depfile_target")
    if not dpath or not target:
        return
    base = spec.get("manifest_dir") or ""
    # cargo's default when a script declares no rerun-if-changed is to re-run on
    # any change in the package dir (the manifest dir).
    watched = parsed.get("rerun_if_changed") or ([base] if base else [])
    deps = set()
    for path in watched:
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        for f in _expand_watch(path):
            deps.add(f.replace("\\", "/"))
    deps.discard(target)
    with Path(dpath).open("w", encoding="utf-8", newline="\n") as fh:
        Rule([target]).add_dependencies(sorted(deps)).dump(fh)


def _parse(stdout):
    out = {
        "cfgs": [],
        "check_cfg": [],
        "env": {},
        "link_libs": [],
        "link_search": [],
        "rerun_if_changed": [],
        "rerun_if_env_changed": [],
        "errors": [],
    }
    for line in stdout.splitlines():
        line = line.strip()
        # cargo accepts both the old `cargo:` and new `cargo::` prefixes.
        if line.startswith("cargo::"):
            body = line[len("cargo::") :]
        elif line.startswith("cargo:"):
            body = line[len("cargo:") :]
        else:
            continue
        key, _, val = body.partition("=")
        if key == "rustc-cfg":
            out["cfgs"].append(val)
        elif key == "rustc-check-cfg":
            out["check_cfg"].append(val)
        elif key == "rustc-env":
            k, _, v = val.partition("=")
            out["env"][k] = v
        elif key == "rustc-link-lib":
            out["link_libs"].append(val)
        elif key == "rustc-link-search":
            out["link_search"].append(val)
        elif key == "rustc-flags":
            # cargo only allows -l/-L here; fold them into link libs/search.
            toks = val.split()
            j = 0
            while j < len(toks):
                t = toks[j]
                if t in ("-l", "-L"):
                    if j + 1 >= len(toks):
                        break
                    arg = toks[j + 1]
                    j += 2
                elif t.startswith(("-l", "-L")):
                    arg = t[2:]
                    t = t[:2]
                    j += 1
                else:
                    j += 1
                    continue
                if t == "-l":
                    out["link_libs"].append(arg)
                else:
                    out["link_search"].append(arg)
        elif key == "rerun-if-changed":
            out["rerun_if_changed"].append(val)
        elif key == "rerun-if-env-changed":
            out["rerun_if_env_changed"].append(val)
        elif key == "error":
            out["errors"].append(val)
    return out


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    args = ap.parse_args(argv)

    spec = _load(args.spec)
    out_dir = spec["out_dir"]
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["OUT_DIR"] = out_dir
    env.update(spec.get("env", {}))

    # BUILDSTATUS markers let mach's build monitor capture and color this as
    # Rust work, matching the cargo path.
    label = spec.get("env", {}).get("CARGO_PKG_NAME", "build-script")
    print(f"BUILDSTATUS START_Rust {label}", flush=True)
    proc = subprocess.run(
        [spec["exe"]],
        cwd=spec.get("manifest_dir") or None,
        env=env,
        capture_output=True,
        check=False,
    )
    print(f"BUILDSTATUS END_Rust {label}", flush=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace"))
        return proc.returncode

    parsed = _parse(proc.stdout.decode("utf-8", "replace"))
    if parsed["errors"]:
        for err in parsed["errors"]:
            sys.stderr.write(f"error: {err}\n")
        return 1
    with Path(spec["output"]).open("w", encoding="utf-8", newline="\n") as f:
        json.dump(parsed, f, indent=2, sort_keys=True)
    _write_depfile(spec, parsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
