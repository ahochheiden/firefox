# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import os
import tempfile
import unittest
from unittest import mock

from mozunit import main

from mozbuild.compilation.util import compile_flags_for


def entry(directory, file, flags, source=None, extra=""):
    return {
        "directory": directory,
        "command": f"ccache clang++ -std=c++20 -o /dev/null -c {flags} -ferror-limit=0 "
        f"{source or file} {extra}".strip(),
        "file": file,
    }


ENTRIES = [
    entry("/obj/a", "/src/a/one.cpp", "-DONE -Xclang -load -Xclang plugin.so"),
    entry("/obj/a", "/src/a/two.c", "-DTWO"),
    entry("/obj/a", "/src/a/three.cpp", "-DTHREE", extra="-DPER_SOURCE"),
    entry("/obj/b", "/src/b/four.cpp", "-DFOUR", source="Unified_cpp_b0.cpp"),
    entry("/obj/c", "/src/c/five.cpp", "-DFIVE", extra="-DFIVE_ONLY"),
    entry("/obj/a", "/src/a/a.mm", "-DOBJCXX"),
    entry("/obj/d", "/src/d/six.mm", "-DSIX"),
    entry("/obj/d", "/src/d/seven.m", "-DSEVEN"),
]


class TestCompileFlagsFor(unittest.TestCase):
    def test_compiled_file(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/one.cpp"), ["-std=c++20", "-DONE"]
        )
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/two.c"), ["-std=c++20", "-DTWO"]
        )

    def test_per_source_flags(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/three.cpp"),
            ["-std=c++20", "-DTHREE", "-DPER_SOURCE"],
        )

    def test_unified_source(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/b/four.cpp"), ["-std=c++20", "-DFOUR"]
        )

    def test_header_per_source_flags(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/c/five.h"), ["-std=c++20", "-DFIVE"]
        )
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/c/five.cpp"),
            ["-std=c++20", "-DFIVE", "-DFIVE_ONLY"],
        )

    def test_header_language(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/one.h"), ["-std=c++20", "-DONE"]
        )
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/other.c"), ["-std=c++20", "-DTWO"]
        )
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/a/other.m"), ["-std=c++20", "-DTWO"]
        )

    def test_header_objective_c(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/d/six.h"), ["-std=c++20", "-DSIX"]
        )
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/d/other.c"), ["-std=c++20", "-DSEVEN"]
        )

    def test_header_parent_directory(self):
        self.assertEqual(
            compile_flags_for(ENTRIES, "/src/b/sub/five.h"), ["-std=c++20", "-DFOUR"]
        )

    def test_no_compiled_file(self):
        self.assertIsNone(compile_flags_for(ENTRIES, "/elsewhere/six.h"))


class TestCompileFlagsCommand(unittest.TestCase):
    def _run(self, what):
        from mach.registrar import Registrar

        if "devenv" not in Registrar.categories:
            Registrar.register_category("devenv", "devenv", "devenv")
        from mozbuild.compilation import codecomplete

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "compile_commands.json")
            with open(db, "w", encoding="utf-8") as fh:
                json.dump(
                    [{**e, "file": os.path.abspath(e["file"])} for e in ENTRIES], fh
                )
            with mock.patch(
                "mozbuild.compilation.util.find_compile_db", return_value=db
            ), mock.patch("builtins.print") as printed:
                rc = codecomplete.compileflags(mock.Mock(topobjdir=tmp), what)
        return rc, printed

    def test_compileflags(self):
        rc, printed = self._run("/src/a/one.cpp")

        self.assertIsNone(rc)
        printed.assert_called_once_with("-std=c++20 -DONE")

    def test_compileflags_no_compiled_file(self):
        rc, printed = self._run("/elsewhere/six.h")

        self.assertEqual(rc, 1)
        printed.assert_called_once_with(
            "No file compiled at or above /elsewhere/six.h in the compilation database."
        )


if __name__ == "__main__":
    main()
