# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run `mozbuild.action.preprocessor` over many entries in one Python process.

`OBJDIR_PP_FILES` and `LOCALIZED_PP_FILES` produce one ninja edge per
file because each entry has its own `-D` defines (per-dir DEFINES,
ACDEFINES, AB_CD). Each edge invokes `python -m
mozbuild.action.preprocessor`, paying Python startup once per file.
This wrapper reads a JSON manifest of `{src, dst, defines}` entries
and calls `mozbuild.action.preprocessor.main(args)` for each in a
loop — same outputs, one Python startup.

Argv:
  pp_install_batch <manifest>

Manifest format:
  [
    {"src": ..., "dst": ..., "defines": ["-DKEY=val", ...]},
    ...
  ]
"""

import json
import sys

from mozbuild.action import preprocessor


def main(argv):
    if len(argv) != 1:
        print("usage: pp_install_batch <manifest>", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        manifest = json.load(f)
    for entry in manifest:
        args = list(entry.get("defines", ()))
        args += ["-o", entry["dst"], entry["src"]]
        preprocessor.main(args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
