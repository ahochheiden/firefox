# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import fnmatch
import json
import os
import shutil
import sys


def _select_templates(directory, names):
    return [n for n in names if n.endswith(".in")]


def _install(source, dest):
    if os.path.isdir(source):
        shutil.copytree(source, dest, symlinks=False, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(source, dest)


def _load_patterns(path):
    patterns = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            patterns.append(stripped)
    return patterns


def _copy_entry(source, dest, follow_symlinks):
    if os.path.lexists(dest):
        if os.path.isdir(dest) and not os.path.islink(dest):
            shutil.rmtree(dest)
        else:
            os.remove(dest)
    if os.path.isdir(source) and (not os.path.islink(source) or follow_symlinks):
        shutil.copytree(source, dest, symlinks=not follow_symlinks)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(source, dest, follow_symlinks=follow_symlinks)


def _stage(stage_dir, contents, macos_files, macos_copy_files):
    resources = os.path.join(contents, "Resources")
    macos = os.path.join(contents, "MacOS")
    include = _load_patterns(macos_files)
    deep_copy = _load_patterns(macos_copy_files)
    for name in sorted(os.listdir(stage_dir)):
        source = os.path.join(stage_dir, name)
        if any(fnmatch.fnmatch(name, p) for p in include):
            _copy_entry(source, os.path.join(macos, name), follow_symlinks=False)
        else:
            _copy_entry(source, os.path.join(resources, name), follow_symlinks=False)
    for name in sorted(os.listdir(stage_dir)):
        if any(fnmatch.fnmatch(name, p) for p in deep_copy):
            _copy_entry(
                os.path.join(stage_dir, name),
                os.path.join(macos, name),
                follow_symlinks=True,
            )


def main(argv):
    with open(argv[0], encoding="utf-8") as fh:
        spec = json.load(fh)

    bundle = spec["bundle"]
    contents = os.path.join(bundle, "Contents")
    macos = os.path.join(contents, "MacOS")
    lproj = os.path.join(contents, "Resources", spec["lproj"])

    if os.path.isdir(bundle):
        shutil.rmtree(bundle)
    os.makedirs(macos)

    if skeleton := spec.get("skeleton"):
        remap = spec["lproj"] != "English.lproj"

        def ignore(directory, names):
            skip = _select_templates(directory, names)
            if (
                remap
                and os.path.basename(directory) == "Resources"
                and "English.lproj" in names
            ):
                skip.append("English.lproj")
            return skip

        shutil.copytree(
            skeleton, contents, ignore=ignore, symlinks=True, dirs_exist_ok=True
        )
        if remap:
            source = os.path.join(skeleton, "Resources", "English.lproj")
            if os.path.isdir(source):
                shutil.copytree(
                    source,
                    lproj,
                    ignore=_select_templates,
                    symlinks=True,
                    dirs_exist_ok=True,
                )

    if spec.get("info_plist"):
        shutil.copy(spec["info_plist"], os.path.join(contents, "Info.plist"))

    if spec.get("strings"):
        os.makedirs(lproj, exist_ok=True)
        shutil.copy(spec["strings"], os.path.join(lproj, "InfoPlist.strings"))

    if spec.get("stage"):
        _stage(spec["stage"], contents, spec["macos_files"], spec["macos_copy_files"])

    for source, dest in spec.get("binaries", []):
        _install(source, os.path.join(macos, dest))

    for source, dest in spec.get("extra_files", []):
        _install(source, os.path.join(contents, dest))

    for name in spec.get("move_to_frameworks", []):
        frameworks = os.path.join(contents, "Frameworks")
        os.makedirs(frameworks, exist_ok=True)
        shutil.move(
            os.path.join(contents, "Resources", name), os.path.join(frameworks, name)
        )

    if spec.get("pkginfo"):
        with open(
            os.path.join(contents, "PkgInfo"), "w", encoding="utf-8", newline=""
        ) as fh:
            fh.write(spec["pkginfo"])

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
