# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Ninja syntax helpers and a small writer class.

Two layers:

* Module-level functions for the three escaping contexts that come up
  in a build.ninja file: paths inside `build` statements, variable
  values, and arguments destined for a compiler response file.
* `NinjaWriter` for emitting `rule`, `build`, `default`, comments, and
  variables with consistent formatting. Callers pass already-escaped
  strings; the writer concerns itself with structural formatting only,
  not escaping policy.

This module has no `mozbuild` runtime dependencies beyond `mozpack.path`
so it can be used by any backend that wants to emit ninja syntax.
"""

import os
import subprocess

import mozpack.path as mozpath


def value(s):
    """Escape a string used as a Ninja variable value or as the body of
    a rule field. Ninja interprets `$` for variable expansion and treats
    a literal newline as a statement terminator, so both must be
    escaped."""
    return s.replace("$", "$$").replace("\n", "$\n")


def path(p):
    """Escape a path for use in a Ninja `build` statement. Ninja parses
    `:` as the separator between outputs and the rule name, so drive
    letters like `D:` must be escaped as `D$:`. Spaces escape as `$ `."""
    p = mozpath.normsep(p)
    return p.replace("$", "$$").replace(" ", "$ ").replace(":", "$:")


def response_arg(flag, msvc=None):
    """Quote a flag for inclusion in a compiler response file.

    Compilers consume `@response.file` using their target argument-parsing
    rules, not shell rules. clang-cl and lld-link use MSVC
    (CommandLineToArgvW) parsing regardless of host OS; gcc/clang use a
    simpler shell-like grammar they implement themselves. Pass `msvc=True`
    when emitting flags for a clang-cl target on a non-Windows host (e.g.
    Linux/macOS cross-compiling Firefox for Windows). When `msvc` is left
    as `None`, the host OS is used as a default — correct for native
    builds.

    The result is embedded in ninja's `rspfile_content`, so escape `$`
    after platform quoting."""
    if msvc is None:
        msvc = os.name == "nt"
    if msvc:
        return subprocess.list2cmdline([flag]).replace("$", "$$")
    # POSIX gcc/clang accept response files using a simple grammar:
    # whitespace separates args, single or double quotes preserve
    # whitespace, backslash escapes the next character. Quote the arg
    # in single quotes if it contains anything special; embedded single
    # quotes terminate the run, escape via the standard `'\''` idiom.
    if not flag or any(c in flag for c in " \t\"'\\"):
        return "'" + flag.replace("'", "'\\''") + "'"
    return flag.replace("$", "$$")


def _as_list(x):
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    return list(x)


class NinjaWriter:
    """Emit a build.ninja file with consistent structural formatting.

    Callers pass already-escaped strings: paths via `path()`, response-
    file args via `response_arg()`, variable values via `value()`. The
    writer concerns itself with structure (where `:`, `|`, `||` go in a
    `build` line; what fields a `rule` block accepts) only.

    Example:

        w = NinjaWriter(fh)
        w.comment("Auto-generated. Do not edit.")
        w.variable("topobjdir", path(topobjdir))
        w.newline()
        w.rule(
            "cxx",
            command="$CXX @$out.rsp",
            description="CXX $out",
            rspfile="$out.rsp",
            rspfile_content="$cxxflags -c $in -o $out",
            deps="gcc",
            depfile="$out.d",
        )
        w.build(
            path(out_obj),
            "cxx",
            inputs=path(src),
            variables={"cxxflags": " ".join(response_arg(f) for f in flags)},
        )
    """

    def __init__(self, fh):
        self.fh = fh

    def comment(self, text):
        self.fh.write(f"# {text}\n")

    def newline(self):
        self.fh.write("\n")

    def variable(self, key, val):
        if val is None:
            return
        self.fh.write(f"{key} = {val}\n")

    def rule(
        self,
        name,
        command,
        description=None,
        depfile=None,
        deps=None,
        rspfile=None,
        rspfile_content=None,
        generator=False,
        restat=False,
        pool=None,
    ):
        self.fh.write(f"rule {name}\n")
        self.fh.write(f"  command = {command}\n")
        if description is not None:
            self.fh.write(f"  description = {description}\n")
        if depfile is not None:
            self.fh.write(f"  depfile = {depfile}\n")
        if deps is not None:
            self.fh.write(f"  deps = {deps}\n")
        if rspfile is not None:
            self.fh.write(f"  rspfile = {rspfile}\n")
        if rspfile_content is not None:
            self.fh.write(f"  rspfile_content = {rspfile_content}\n")
        if generator:
            self.fh.write("  generator = 1\n")
        if restat:
            self.fh.write("  restat = 1\n")
        if pool is not None:
            self.fh.write(f"  pool = {pool}\n")

    def build(
        self,
        outputs,
        rule_name,
        inputs=None,
        implicit=None,
        order_only=None,
        implicit_outputs=None,
        variables=None,
    ):
        out_list = _as_list(outputs)
        impl_out_list = _as_list(implicit_outputs)

        out_str = " ".join(out_list)
        if impl_out_list:
            out_str = f"{out_str} | {' '.join(impl_out_list)}"

        line = f"build {out_str}: {rule_name}"
        in_list = _as_list(inputs)
        if in_list:
            line += " " + " ".join(in_list)
        impl_list = _as_list(implicit)
        if impl_list:
            line += " | " + " ".join(impl_list)
        oo_list = _as_list(order_only)
        if oo_list:
            line += " || " + " ".join(oo_list)
        self.fh.write(line + "\n")
        if variables:
            for k, v in variables.items():
                if v is None:
                    continue
                self.fh.write(f"  {k} = {v}\n")

    def default(self, targets):
        target_list = _as_list(targets)
        if not target_list:
            return
        self.fh.write(f"default {' '.join(target_list)}\n")

    def pool(self, name, depth):
        self.fh.write(f"pool {name}\n")
        self.fh.write(f"  depth = {depth}\n")
