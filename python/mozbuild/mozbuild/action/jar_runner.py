# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run `mozbuild.action.jar_maker`, then touch a stamp file, in one
Python process.

Ninja invokes commands via `CreateProcess` directly on Windows, so the
shell chaining `<jar_maker> && touch <stamp>` won't work — the tokens
after `&&` arrive as positional args to jar_maker. This wrapper
performs both steps in-process instead.

argv layout: ``STAMP JAR_MAKER_ARGS...``
"""

import os
import sys

from mozbuild.action import jar_maker


def main(argv):
    if len(argv) < 2:
        print("usage: jar_runner STAMP JAR_MAKER_ARGS...", file=sys.stderr)
        return 2
    stamp = argv[0]
    rc = jar_maker.main(argv[1:])
    if rc:
        return rc
    open(stamp, "w").close()
    os.utime(stamp, None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
