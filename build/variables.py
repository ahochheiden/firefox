# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import sys

SOURCESTAMP_FILENAME = "sourcestamp.txt"


def get_buildid():
    import buildconfig

    path = os.path.join(buildconfig.topobjdir, "buildid.h")
    _define, _MOZ_BUILDID, buildid = open(path, encoding="utf-8").read().split()
    return buildid


def buildid_header(output, buildid_file):
    with open(buildid_file, encoding="utf-8") as fh:
        buildid = fh.read().strip()
    # If this output changes, be sure to update `get_buildid()`.
    output.write("#define MOZ_BUILDID %s\n" % buildid)


def get_info_from_sourcestamp(sourcestamp_path):
    """Read the repository and changelog information from the sourcestamp
    file. This assumes that the file exists and returns the results as a list
    (either strings or None in case of error).
    """

    # Load the content of the file.
    lines = None
    with open(sourcestamp_path) as f:
        lines = f.read().splitlines()

    # Parse the repo and the changeset. The sourcestamp file is supposed to
    # contain two lines: the first is the build id and the second is the source
    # URL.
    if len(lines) != 2 or not lines[1].startswith("http"):
        # Just return if the file doesn't contain what we expect.
        return None, None

    # Return the repo and the changeset.
    for separator in ["/rev/", "/commit/"]:
        if separator in lines[1]:
            return lines[1].split(separator)
    return None, None


def source_repo_header(output, source_repo_file):
    import buildconfig

    with open(source_repo_file, encoding="utf-8") as fh:
        changeset, repo = (fh.read().splitlines() + ["", ""])[:2]
    source = ""

    if not changeset and not repo:
        sourcestamp_path = os.path.join(buildconfig.topsrcdir, SOURCESTAMP_FILENAME)
        if os.path.exists(sourcestamp_path):
            repo, changeset = get_info_from_sourcestamp(sourcestamp_path)

    if changeset:
        output.write("#define MOZ_SOURCE_STAMP %s\n" % changeset)

    if repo and buildconfig.substs.get("MOZ_INCLUDE_SOURCE_INFO"):
        rev_path = "/rev/" if "hg.mozilla.org" in repo else "/commit/"
        source = f"{repo}{rev_path}{changeset}"
        output.write("#define MOZ_SOURCE_REPO %s\n" % repo)
        output.write("#define MOZ_SOURCE_URL %s\n" % source)

        git_repo = buildconfig.substs.get("MOZ_SOURCE_GIT_REPO")
        git_changeset = buildconfig.substs.get("MOZ_SOURCE_GIT_CHANGESET")
        if git_repo and git_changeset:
            output.write(
                "#define MOZ_SOURCE_GIT_URL %s/commit/%s\n" % (git_repo, git_changeset)
            )


def main(args):
    if len(args):
        func = globals().get(args[0])
        if func:
            return func(sys.stdout, *args[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
