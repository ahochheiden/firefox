# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Diff the cargo invocation produced by `force-cargo-library-build` against
the one produced by `mozbuild.action.cargo_build --print-only`.

Modes:
  make-invoke <relobjdir>      Run make with a cargo wrapper, print its
                               captured argv + env.
  action-invoke <spec.json>    Invoke cargo_build with --print-only.
  diff <make.txt> <action.txt> Compare two captures.
  make-spec <relobjdir>        Emit a draft spec.json from backend.mk vars.
  _wrapper-cargo               Internal: drop-in cargo wrapper for make-invoke.
"""

import argparse
import os
import re
import subprocess
import sys


def _print_argv_env(argv, env_diff):
    print("CARGO_ARGV")
    for a in argv:
        print(a)
    print("END_CARGO_ARGV")
    print("ENV")
    for k in sorted(env_diff):
        print(f"{k}={env_diff[k]}")
    print("END_ENV")


def _parse_block(text):
    argv = []
    env = {}
    in_argv = False
    in_env = False
    for line in text.splitlines():
        if line == "CARGO_ARGV":
            in_argv = True
            continue
        if line == "END_CARGO_ARGV":
            in_argv = False
            continue
        if line == "ENV":
            in_env = True
            continue
        if line == "END_ENV":
            in_env = False
            continue
        if in_argv:
            argv.append(line)
        elif in_env:
            k, _, v = line.partition("=")
            env[k] = v
    return argv, env


def _wrapper_cargo(argv):
    base_path = os.environ.get("CARGO_VALIDATE_BASE_ENV", "")
    base_env = {}
    if base_path and os.path.exists(base_path):
        with open(base_path) as f:
            for line in f:
                line = line.rstrip("\n")
                if "=" in line:
                    k, _, v = line.partition("=")
                    base_env[k] = v

    diff = {
        k: v
        for k, v in os.environ.items()
        if base_env.get(k) != v and k != "CARGO_VALIDATE_BASE_ENV"
    }
    _print_argv_env(["cargo"] + argv, diff)
    return 0


def _make_invoke(relobjdir):
    import buildconfig

    topobjdir = buildconfig.topobjdir
    cargo_dir = os.path.join(topobjdir, relobjdir)
    base_env_file = os.path.join(topobjdir, ".cargo-validate-base-env")
    with open(base_env_file, "w") as f:
        for k, v in os.environ.items():
            f.write(f"{k}={v}\n")

    is_windows = sys.platform == "win32"
    wrapper_argv = [
        sys.executable,
        "-m",
        "mozbuild.action.cargo_validate",
        "_wrapper-cargo",
    ]
    if is_windows:
        wrapper_path = os.path.join(topobjdir, ".cargo-validate-wrapper.bat")
        quoted = " ".join(f'"{a}"' for a in wrapper_argv)
        with open(wrapper_path, "w", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write(f"{quoted} %*\r\n")
        make_program = (
            os.environ.get("MAKE")
            or os.environ.get("MOZMAKE")
            or buildconfig.substs.get("GMAKE")
            or "mozmake"
        )
    else:
        wrapper_path = os.path.join(topobjdir, ".cargo-validate-wrapper.sh")
        quoted = " ".join(f'"{a}"' for a in wrapper_argv)
        with open(wrapper_path, "w") as f:
            f.write("#!/bin/sh\n")
            f.write(f'exec {quoted} "$@"\n')
        os.chmod(wrapper_path, 0o755)
        make_program = os.environ.get("MAKE", "make")

    env = dict(os.environ)
    env["CARGO_VALIDATE_BASE_ENV"] = base_env_file

    cmd = [
        make_program,
        "-s",
        "-C",
        cargo_dir,
        "force-cargo-library-build",
        f"CARGO={wrapper_path}",
        "CARGO_NO_AUTO_ARG=1",
        "MACH=1",
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def _action_invoke(spec_path):
    cmd = [
        sys.executable,
        "-m",
        "mozbuild.action.cargo_build",
        "--spec",
        spec_path,
        "--print-only",
    ]
    return subprocess.call(cmd)


_IGNORED_ENV_KEYS = {
    "CARGO",
    "CARGO_NO_AUTO_ARG",
    "CARGO_VALIDATE_BASE_ENV",
    "INCLUDE",
    "LIB",
    "MACH",
    "MAKE",
    "MAKEFLAGS",
    "MAKELEVEL",
    "MAKE_TERMERR",
    "MAKE_TERMOUT",
    "MAKEOVERRIDES",
    "MFLAGS",
    "PATH",
}


_IGNORED_ARGV_TOKENS = {"--keep-going"}


def _normalize_argv(argv):
    return [a for a in argv if a not in _IGNORED_ARGV_TOKENS]


def _ci_env(env):
    out = {}
    for k, v in env.items():
        out[k.upper()] = v
    return out


def _normalize_ws(s):
    return " ".join(s.split())


_WS_NORMALIZED_KEYS = {
    "CFLAGS_",
    "CXXFLAGS_",
    "RUSTFLAGS",
    "MOZ_CARGO_WRAP_LDFLAGS",
    "MOZ_CARGO_WRAP_HOST_LDFLAGS",
    "BINDGEN_EXTRA_CLANG_ARGS",
    "RUSTDOCFLAGS",
}


def _should_ws_normalize(key):
    for prefix in _WS_NORMALIZED_KEYS:
        if prefix.endswith("_"):
            if key.startswith(prefix) or key.startswith(prefix.upper()):
                return True
        elif key == prefix or key == prefix.upper():
            return True
    return False


def _diff(make_path, action_path):
    with open(make_path) as f:
        make_argv, make_env_raw = _parse_block(f.read())
    with open(action_path) as f:
        action_argv, action_env_raw = _parse_block(f.read())

    make_argv = _normalize_argv(make_argv)
    action_argv = _normalize_argv(action_argv)

    case_insensitive = sys.platform == "win32"
    if case_insensitive:
        make_env = _ci_env(make_env_raw)
        action_env = _ci_env(action_env_raw)
        ignored = {k.upper() for k in _IGNORED_ENV_KEYS}
    else:
        make_env = make_env_raw
        action_env = action_env_raw
        ignored = set(_IGNORED_ENV_KEYS)

    diffs = 0

    if make_argv != action_argv:
        diffs += 1
        print("=== ARGV mismatch ===")
        max_len = max(len(make_argv), len(action_argv))
        for i in range(max_len):
            m = make_argv[i] if i < len(make_argv) else "<MISSING>"
            a = action_argv[i] if i < len(action_argv) else "<MISSING>"
            mark = "  " if m == a else "* "
            print(f"{mark}[{i}] make={m!r} action={a!r}")

    all_keys = sorted(set(make_env) | set(action_env))
    env_mismatches = []
    for k in all_keys:
        if k in ignored:
            continue
        m = make_env.get(k)
        a = action_env.get(k)
        if _should_ws_normalize(k):
            m_cmp = _normalize_ws(m) if m is not None else m
            a_cmp = _normalize_ws(a) if a is not None else a
        else:
            m_cmp, a_cmp = m, a
        if m_cmp != a_cmp:
            env_mismatches.append((k, m, a))
    if env_mismatches:
        diffs += 1
        print("=== ENV mismatch ===")
        for k, m, a in env_mismatches:
            print(f"  {k}:")
            print(f"    make   = {m!r}")
            print(f"    action = {a!r}")

    if diffs == 0:
        print("OK: argv and env match")
    return 0 if diffs == 0 else 1


_MAKE_VAR_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*(\+=|:?=)\s*(.*?)\s*$")


def _read_make_vars(path):
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        for line in f:
            m = _MAKE_VAR_RE.match(line)
            if not m:
                continue
            key, op, val = m.group(1), m.group(2), m.group(3)
            if op == "+=":
                prev = out.get(key, "")
                out[key] = f"{prev} {val}".strip() if prev else val
            else:
                out[key] = val
    return out


def _resolve_make_path(value, topsrcdir, topobjdir, relobjdir):
    srcdir = f"{topsrcdir}/{relobjdir}"
    return (
        value
        .replace("$(srcdir)", srcdir)
        .replace("$(DEPTH)", topobjdir)
        .replace("$(topobjdir)", topobjdir)
        .replace("$(topsrcdir)", topsrcdir)
    )


def _make_spec(relobjdir):
    import buildconfig
    from mozfile import json

    topobjdir = buildconfig.topobjdir.replace("\\", "/")
    topsrcdir = buildconfig.topsrcdir.replace("\\", "/")
    backend_mk = os.path.join(topobjdir, relobjdir, "backend.mk")
    vars_ = _read_make_vars(backend_mk)

    output_path = _resolve_make_path(
        vars_.get("RUST_LIBRARY_FILE", ""), topsrcdir, topobjdir, relobjdir
    )
    cargo_file = _resolve_make_path(
        vars_.get("CARGO_FILE", ""), topsrcdir, topobjdir, relobjdir
    )
    features_csv = vars_.get("RUST_LIBRARY_FEATURES", "")
    features = [f for f in features_csv.split(",") if f]

    target_triple = buildconfig.substs.get("RUST_TARGET", "")
    is_megazord = "megazord" in output_path
    is_gkrust_gtest = "gkrust_gtest" in output_path

    spec = {
        "kind": "library",
        "subcommand": "build",
        "manifest_path": cargo_file,
        "output_path": output_path,
        "features": features,
        "target_triple": target_triple,
        "is_megazord": is_megazord,
        "is_gkrust_gtest": is_gkrust_gtest,
        "is_ltoable": True,
        "extra_rustcflags": [],
        "cargo_extra_cli_flags": [],
        "output_category": vars_.get("RUST_LIBRARY_OUTPUT_CATEGORY") or None,
        "relsrcdir": relobjdir,
        "relobjdir": relobjdir,
        "computed_cflags": vars_.get("COMPUTED_CFLAGS", ""),
        "computed_cxxflags": vars_.get("COMPUTED_CXXFLAGS", ""),
        "computed_host_cflags": vars_.get("COMPUTED_HOST_CFLAGS", ""),
        "computed_host_cxxflags": vars_.get("COMPUTED_HOST_CXXFLAGS", ""),
        "computed_ldflags": vars_.get("COMPUTED_LDFLAGS", ""),
    }
    print(json.dumps(spec, indent=2))
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("make-invoke")
    p.add_argument("relobjdir")

    p = sub.add_parser("action-invoke")
    p.add_argument("spec")

    p = sub.add_parser("diff")
    p.add_argument("make_capture")
    p.add_argument("action_capture")

    p = sub.add_parser("make-spec")
    p.add_argument("relobjdir")

    p = sub.add_parser("_wrapper-cargo")
    p.add_argument("rest", nargs=argparse.REMAINDER)

    args = ap.parse_args(argv)

    if args.mode == "make-invoke":
        return _make_invoke(args.relobjdir)
    if args.mode == "action-invoke":
        return _action_invoke(args.spec)
    if args.mode == "diff":
        return _diff(args.make_capture, args.action_capture)
    if args.mode == "make-spec":
        return _make_spec(args.relobjdir)
    if args.mode == "_wrapper-cargo":
        return _wrapper_cargo(args.rest)
    return 2


if __name__ == "__main__":
    sys.exit(main())
