# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Execute a Rust build-script binary, parse cargo: directives from
stdout, write structured outputs that the corresponding lib/proc-macro
invocation consumes.

Emitted files in <output-dir>:
  out/                 -- the cargo OUT_DIR; build script writes here
  cfg.txt              -- one `<cfg>` per line, derived from cargo:rustc-cfg=
  env.txt              -- KEY=VAL per line, derived from cargo:rustc-env=
  linklibs.txt         -- raw cargo:rustc-link-lib= / link-search lines for
                          libxul-link aggregation in a later phase.
  rerun_files.txt      -- absolute paths from cargo:rerun-if-changed=
  warnings.txt         -- cargo:warning= lines (informational).

The lib/proc-macro rustc edge consumes cfg.txt and env.txt via
mozbuild.action.rust_invoke's --buildrs-output flag."""

import argparse
import json
import os
import subprocess
import sys


_RECOGNIZED = (
    "cargo:rustc-cfg=",
    "cargo:rustc-env=",
    "cargo:rustc-link-lib=",
    "cargo:rustc-link-search=",
    "cargo:rustc-flags=",
    "cargo:rerun-if-changed=",
    "cargo:rerun-if-env-changed=",
    "cargo:warning=",
)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    out_dir = os.path.join(args.output_dir, "out")
    os.makedirs(out_dir, exist_ok=True)

    env = dict(os.environ)
    env.update(spec.get("env", {}))
    env["OUT_DIR"] = out_dir
    env["RUSTUP_AUTO_INSTALL"] = "0"

    program = spec["program"]
    cmd_args = list(spec.get("args", ()))
    cwd = spec.get("cwd")

    proc = subprocess.run(
        [program] + cmd_args,
        env=env,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    cfg_lines = []
    env_lines = []
    linklib_lines = []
    rerun_lines = []
    warning_lines = []

    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line.startswith("cargo:"):
            continue
        if line.startswith("cargo:rustc-cfg="):
            cfg_lines.append(line[len("cargo:rustc-cfg="):])
        elif line.startswith("cargo:rustc-env="):
            env_lines.append(line[len("cargo:rustc-env="):])
        elif line.startswith("cargo:rustc-link-lib=") or line.startswith(
            "cargo:rustc-link-search="
        ):
            linklib_lines.append(line)
        elif line.startswith("cargo:rerun-if-changed="):
            rerun_lines.append(line[len("cargo:rerun-if-changed="):])
        elif line.startswith("cargo:warning="):
            warning_lines.append(line[len("cargo:warning="):])

    def _write(name, lines):
        with open(os.path.join(args.output_dir, name), "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")

    _write("cfg.txt", cfg_lines)
    _write("env.txt", env_lines)
    _write("linklibs.txt", linklib_lines)
    _write("rerun_files.txt", rerun_lines)
    _write("warnings.txt", warning_lines)

    if proc.returncode != 0:
        # Build scripts print failures (e.g. cc-rs's cl.exe stderr)
        # via `println!` which lands on stdout. Dump both streams so
        # the actual error reaches the user.
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        return proc.returncode

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
