# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import sys

from mozbuild.packaging import multilocale_content


def main(output, *locales):
    output.write(multilocale_content(locales))


if __name__ == "__main__":
    main(sys.stdout, *sys.argv[1:])
