# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run cargo for a Rust build edge described by a per-target spec.

Usage:
    python -m mozbuild.action.cargo_build --spec <path> [--print-only]

The spec JSON is written by NinjaBackend at backend-write time and contains
the per-target inputs (manifest path, features, target triple, kind, etc).
Global flags and env come from buildconfig.substs at action runtime.

`--print-only` writes the composed argv + env to stdout in a structured
format and exits 0 without running cargo. Used by the validation harness to
diff against `make -n` output.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mozfile import json

from mozbuild.action._rust_env import (
    _bool,
    _compute_rustc_flags,
    _compute_sancov_flags,
    compose,
)


def _load_spec(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_buildstatus(relsrcdir, msg):
    if relsrcdir:
        print(f"BUILDSTATUS@{relsrcdir} {msg}", flush=True)
    else:
        print(f"BUILDSTATUS {msg}", flush=True)


def _print_only(argv, env, current_env):
    print("CARGO_ARGV")
    norm_argv = list(argv)
    if norm_argv:
        norm_argv[0] = "cargo"
    for a in norm_argv:
        print(a)
    print("END_CARGO_ARGV")
    print("ENV")
    diff_keys = sorted(k for k, v in env.items() if current_env.get(k) != v)
    for k in diff_keys:
        print(f"{k}={env[k]}")
    print("END_ENV")


def _should_run_network_check(spec, substs):
    if spec["kind"] != "library":
        return False
    if _bool(substs.get("MOZ_PROFILE_GENERATE")):
        return False
    if substs.get("OS_ARCH") != "Linux":
        return False
    if (
        _compute_sancov_flags(substs)
        or _bool(substs.get("MOZ_ASAN"))
        or _bool(substs.get("MOZ_TSAN"))
        or _bool(substs.get("MOZ_UBSAN"))
    ):
        return False
    if substs.get("MOZ_LTO_RUST_CROSS"):
        return False
    rustc_flags = _compute_rustc_flags(spec, substs)
    return "-Clto" in rustc_flags or "-Clto=fat" in rustc_flags


def _run_network_check(spec, substs):
    output = spec["output_path"]
    cmd = [
        sys.executable,
        "-m",
        "mozbuild.action.check_binary",
        "--networking",
        output,
    ]
    return subprocess.call(cmd)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--print-only", action="store_true")
    args = ap.parse_args(argv)

    spec = _load_spec(args.spec)

    import buildconfig

    substs = buildconfig.substs
    topsrcdir = buildconfig.topsrcdir
    topobjdir = buildconfig.topobjdir

    cargo_argv, env = compose(spec, substs, os.environ, topsrcdir, topobjdir)

    if args.print_only:
        _print_only(cargo_argv, env, dict(os.environ))
        return 0

    relsrcdir = spec.get("relsrcdir", "")
    label = Path(spec["output_path"]).name

    # Snapshot cargo-timings before invoking cargo so we can identify the
    # `cargo-timing-<ts>.html` this invocation writes and rename it to embed
    # the edge label. `runtime.py:_record_cargo_timings` pairs HTMLs to
    # edges by label-in-filename — deterministic instead of relying on
    # mtime ordering with concurrent cargo edges sharing CARGO_TARGET_DIR.
    timings_dir = Path(topobjdir) / "cargo-timings"

    def _timing_html_files():
        # Cargo writes per-invocation files as `cargo-timing-<ts>.html`
        # and a non-timestamped alias `cargo-timing.html`. Only the
        # timestamped form is per-invocation, so ignore the alias.
        if not timings_dir.is_dir():
            return set()
        return {
            p.name
            for p in timings_dir.iterdir()
            if p.name.startswith("cargo-timing-") and p.name.endswith(".html")
        }

    before = _timing_html_files()
    _print_buildstatus(relsrcdir, f"START_Rust {label}")
    rc = subprocess.call(cargo_argv, env=env)
    new = sorted(
        _timing_html_files() - before,
        key=lambda f: (timings_dir / f).stat().st_mtime,
    )
    if new:
        src = timings_dir / new[-1]
        # Match `ninja/runtime.py:_classify_ninja_edge`'s edge label
        # (basename minus extension, e.g. "gkrust" not "gkrust.lib").
        edge_label = Path(label).stem
        rest = new[-1][len("cargo-timing-") :]
        dst = timings_dir / f"cargo-timing-{edge_label}-{rest}"
        try:
            src.replace(dst)
        except OSError:
            pass
    _print_buildstatus(relsrcdir, f"END_Rust {label}")

    if rc != 0:
        return rc

    if _should_run_network_check(spec, substs):
        return _run_network_check(spec, substs)

    return 0


if __name__ == "__main__":
    sys.exit(main())
