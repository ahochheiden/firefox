# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run `xpidl-process` over many modules in a single Python process.

`xpidl-process.py` exposes `process(...)` for one module at a time.
Invoking it 167+ times via `python -m mozbuild.action.xpidl-process`
(once per XPIDL module) makes the early build phase pay Python startup
cost per module. mozmake's `config/makefiles/xpidl/Makefile.in` runs
xpidl-process inline as a single recipe; this is the ninja-side
equivalent: one edge, one Python startup, all modules processed in a
loop.

Argv:
  xpidl_batch --depsdir DIR --bindings-conf PATH --header-dir DIR
              --xpcrs-dir DIR --xpt-dir DIR
              --combined-depfile PATH --combined-target PATH
              [-I DIR ...] --manifest PATH

The manifest is JSON: `[{"module": NAME, "idls": [PATH, ...]}, ...]`.
After processing, per-module `.pp` depfiles in `--depsdir` are merged
into a single `--combined-depfile` keyed on `--combined-target` so
ninja's `deps = gcc` parser can consume it.
"""

import argparse
import importlib
import json
import os
import sys


def _load_xpidl_process():
    # The xpidl-process action's filename has a hyphen, so a normal
    # `import` won't work — load the module via its file path.
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "_xpidl_process", os.path.join(here, "xpidl-process.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _merge_depfiles(deps_dir, modules, combined_path, combined_target):
    deps = set()
    for name in modules:
        per_module = os.path.join(deps_dir, name + ".pp")
        if not os.path.exists(per_module):
            continue
        with open(per_module, encoding="utf-8", errors="replace") as f:
            text = f.read()
        # gcc-style depfiles are `target: dep1 dep2 ...`. We don't care
        # about the per-module target — ninja matches the combined
        # target against our edge's primary output.
        _, _, rest = text.partition(":")
        for tok in rest.split():
            if tok and tok != "\\":
                deps.add(tok)
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined_target + ":")
        for d in sorted(deps):
            f.write(" " + d)
        f.write("\n")


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--depsdir", required=True)
    parser.add_argument("--bindings-conf", required=True)
    parser.add_argument("--input-dir", dest="input_dirs", action="append", default=[])
    parser.add_argument("-I", dest="incpath", action="append", default=[])
    parser.add_argument("--header-dir", required=True)
    parser.add_argument("--xpcrs-dir", required=True)
    parser.add_argument("--xpt-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--combined-depfile", required=True)
    parser.add_argument("--combined-target", required=True)
    args = parser.parse_args(argv)

    xpidl_process = _load_xpidl_process()
    incpath = [os.path.join(xpidl_process.topsrcdir, p) for p in args.incpath]

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    module_names = []
    for entry in manifest:
        module_names.append(entry["module"])
        xpidl_process.process(
            args.input_dirs,
            incpath,
            args.bindings_conf,
            args.header_dir,
            args.xpcrs_dir,
            args.xpt_dir,
            args.depsdir,
            entry["module"],
            entry["idls"],
        )

    _merge_depfiles(
        args.depsdir, module_names, args.combined_depfile, args.combined_target
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
