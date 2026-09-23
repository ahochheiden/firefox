# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os
from collections import defaultdict

import mozpack.path as mozpath
from mozshellutil import split as shell_split

C_SUFFIXES = (".c",)
CXX_SUFFIXES = (".cpp", ".cc", ".cxx")
OBJC_SUFFIXES = (".m",)
OBJCXX_SUFFIXES = (".mm",)


def find_compile_db(topobjdir):
    for path in (
        mozpath.join(topobjdir, "clangd", "compile_commands.json"),
        mozpath.join(topobjdir, "compile_commands.json"),
    ):
        if os.path.exists(path):
            return path
    return None


def compile_flags_for(entries, path):
    """Return the compile flags for ``path`` from the compilation database
    ``entries``, without the compiler, output and source arguments.

    A file with no entry of its own, such as a header, takes the flags of a file
    compiled in the nearest directory at or above it, in C for a `.c` or `.m`
    file and C++ otherwise, or in Objective-C or Objective-C++ when that
    directory compiles none of the first, without the flags that file sets for
    itself alone.
    """

    def key(p):
        return os.path.normcase(mozpath.normpath(p))

    by_file = {}
    by_dir = defaultdict(list)
    for entry in entries:
        k = key(entry["file"])
        by_file[k] = entry
        by_dir[os.path.dirname(k)].append(k)

    path = key(path)
    entry = by_file.get(path)
    own_entry = entry is not None
    if path.endswith(C_SUFFIXES + OBJC_SUFFIXES):
        languages = (C_SUFFIXES, OBJC_SUFFIXES)
    else:
        languages = (CXX_SUFFIXES, OBJCXX_SUFFIXES)
    directory = os.path.dirname(path)
    while entry is None:
        for suffixes in languages:
            candidates = sorted(f for f in by_dir[directory] if f.endswith(suffixes))
            if candidates:
                entry = by_file[candidates[0]]
                break
        if entry is not None:
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent

    args = shell_split(entry["command"])
    output = args.index("-o")
    compiler = 0
    while compiler < output and not args[compiler].startswith("-"):
        compiler += 1
    start = args.index("-c") + 1
    source = args.index("-ferror-limit=0") + 1
    per_source = args[source + 1 :] if own_entry else []
    return sanitize_cflags(
        args[compiler:output] + args[start : source - 1] + per_source
    )


def sanitize_cflags(flags):
    # We filter out -Xclang arguments as clang based tools typically choke on
    # passing these flags down to the clang driver.  -Xclang tells the clang
    # driver driver to pass whatever comes after it down to clang cc1, which is
    # why we skip -Xclang and the argument immediately after it.  Here is an
    # example: the following two invocations pass |-foo -bar -baz| to cc1:
    # clang -cc1 -foo -bar -baz
    # clang -Xclang -foo -Xclang -bar -Xclang -baz
    sanitized = []
    saw_xclang = False
    for flag in flags:
        if flag == "-Xclang":
            saw_xclang = True
        elif saw_xclang:
            saw_xclang = False
        else:
            sanitized.append(flag)
    return sanitized
