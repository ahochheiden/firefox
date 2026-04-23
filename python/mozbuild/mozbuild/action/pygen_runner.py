# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run `mozbuild.action.file_generate`, then post-process its depfile
via `mozbuild.action.filter_depfile`, in a single Python process.

The depfile post-process is required because file_generate writes
Makefile-style depfiles that wrap conditional inputs in
`$(wildcard X)`. Ninja's gcc-depfile parser does not understand that
syntax and would mark every output dirty on every build. filter_depfile
unwraps the wildcards and drops missing-file deps so ninja's up-to-date
check works.

argv layout: ``DEPFILE FILE_GENERATE_ARGS...``

``DEPFILE`` is the depfile to post-process. It also appears as one of
the positional args inside ``FILE_GENERATE_ARGS`` because file_generate
writes its own depfile from that argument. The redundancy keeps this
wrapper trivially simple without having to parse file_generate's argv
shape.
"""

import sys

from mozbuild.action import file_generate, filter_depfile


def main(argv):
    if len(argv) < 2:
        print(
            "usage: pygen_runner DEPFILE FILE_GENERATE_ARGS...",
            file=sys.stderr,
        )
        return 2
    depfile = argv[0]
    rc = file_generate.main(argv[1:])
    if rc:
        return rc
    return filter_depfile.main([depfile])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
