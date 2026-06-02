# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Merge several ninja build logs into a single edge-weight seed file.

The forked ninja seeds each edge's weight from ``build/.ninja-seed-weights``,
keyed by output path (``BuildLog::LookupByOutput``) with the weight being
``end - start``. Different platforms build different targets, so this unions
their ``.ninja_log`` files: for each output path, the line with the longest
duration across all inputs wins. Lines are kept verbatim (the hash and
timestamps are ignored when seeding but must stay well-formed), so
platform-specific path spellings coexist and each build matches its own.
"""

import argparse
import sys


def _fold_log(path, best, versions):
    """Merge one ninja log into ``best`` (output path -> (duration, line)),
    keeping the longest-duration line for each output path."""
    with open(path, encoding="utf-8") as fh:
        header = fh.readline().rstrip("\n")
        if not header.startswith("# ninja log v"):
            raise ValueError(f"{path}: not a ninja log (first line: {header!r})")
        versions.add(header)
        for lineno, line in enumerate(fh, start=2):
            line = line.rstrip("\n")
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 5:
                raise ValueError(
                    f"{path}:{lineno}: expected 5 tab-separated fields, "
                    f"got {len(fields)}"
                )
            output = fields[3]
            duration = int(fields[1]) - int(fields[0])
            current = best.get(output)
            if current is None or duration > current[0]:
                best[output] = (duration, line)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="+", help=".ninja_log files to merge")
    parser.add_argument(
        "-o",
        "--output",
        default="build/.ninja-seed-weights",
        help="seed file to write (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    best = {}
    versions = set()
    for path in args.logs:
        _fold_log(path, best, versions)

    if len(versions) != 1:
        raise SystemExit(
            f"inputs use mismatched ninja log versions: {sorted(versions)}"
        )
    header = versions.pop()

    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header + "\n")
        for output in sorted(best):
            fh.write(best[output][1] + "\n")

    print(f"merged {len(args.logs)} logs into {args.output} ({len(best)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
