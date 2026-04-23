# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.


def strip_tests(path):
    """Strip a leading `_tests/` from a manifest destination path.

    `TestHarnessFiles.install_target == "_tests"`, so destinations look
    like `_tests/<sub>/<file>`. The `_ninja_test_files` install manifest
    is installed with `_tests/` as its `install_dir`, so its entries
    must be relative to `_tests/`.
    """
    if path == "_tests":
        return ""
    if path.startswith("_tests/"):
        return path[len("_tests/") :]
    return path
