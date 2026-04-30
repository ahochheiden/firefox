# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run a rustc invocation described by a JSON spec, optionally splicing
flags from a build-script run.

The spec encodes one entry from `cargo build --build-plan` output:
program, args, env, cwd. We set env, set cwd, exec program. If
`--buildrs-output` is given, we read `cfg.txt` and `env.txt` from that
directory and append `--cfg <X>` to args and KEY=VAL to env before
exec'ing — this is how cargo's build-script-derived cfgs get into the
rustc invocation."""

import argparse
import json
import os
import subprocess
import sys


def _read_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\r\n") for ln in f if ln.strip()]


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument(
        "--buildrs-output",
        default=None,
        help="Directory containing cfg.txt / env.txt from a build-script run. "
        "Their contents get spliced into args/env before exec.",
    )
    args = parser.parse_args(argv)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    program = spec["program"]
    cmd_args = list(spec.get("args", ()))
    env = dict(os.environ)
    env.update(spec.get("env", {}))
    # Disable rustup's on-demand component installation. Many concurrent
    # rustc invocations through the rustup proxy race on partial-file
    # renames; the build plan was generated with the toolchain already
    # in place so we don't need rustup to fetch anything.
    env["RUSTUP_AUTO_INSTALL"] = "0"
    cwd = spec.get("cwd")

    if args.buildrs_output:
        for cfg in _read_lines(os.path.join(args.buildrs_output, "cfg.txt")):
            cmd_args.extend(["--cfg", cfg])
        for envline in _read_lines(os.path.join(args.buildrs_output, "env.txt")):
            if "=" in envline:
                k, _, v = envline.partition("=")
                env[k] = v

    # Splice cargo:rustc-link-{lib,search,arg}= directives from every
    # transitive build script's `linklibs.txt` into rustc args. cargo
    # normally aggregates these for crates that produce a binary /
    # dylib / staticlib; rustc ignores extra `-l` flags for rlib
    # output, so emitting them unconditionally is safe.
    for d in spec.get("linklibs_dirs", ()):
        for line in _read_lines(os.path.join(d, "linklibs.txt")):
            if line.startswith("cargo:rustc-link-lib="):
                cmd_args.extend(["-l", line[len("cargo:rustc-link-lib="):]])
            elif line.startswith("cargo:rustc-link-search="):
                cmd_args.extend(["-L", line[len("cargo:rustc-link-search="):]])
            elif line.startswith("cargo:rustc-link-arg="):
                cmd_args.extend([
                    "-C",
                    "link-arg=" + line[len("cargo:rustc-link-arg="):],
                ])
            elif line.startswith("cargo:rustc-flags="):
                # Free-form rustc flags; pass through verbatim.
                cmd_args.extend(line[len("cargo:rustc-flags="):].split())

    rc = subprocess.call([program] + cmd_args, env=env, cwd=cwd)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
