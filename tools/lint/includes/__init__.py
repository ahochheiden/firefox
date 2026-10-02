# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import difflib
import os
import re
from collections import Counter

import yaml
from mozlint import result
from mozlint.pathutils import expand_exclusions

here = os.path.dirname(__file__)
with open(os.path.join(here, "..", "..", "..", "mfbt", "api.yml")) as fd:
    description = yaml.safe_load(fd)


def generate_diff(path, raw_content, line_to_delete):
    prev_content = raw_content.split("\n")
    new_content = [
        raw_line
        for lineno, raw_line in enumerate(prev_content, start=1)
        if lineno != line_to_delete
    ]
    diff = "\n".join(
        difflib.unified_diff(prev_content, new_content, fromfile=path, tofile=path)
    )
    return diff


symbol_pattern = r"\b{}\b"
literal_pattern = r'[0-9."\']{}\b'

categories_pattern = {
    "variables": symbol_pattern,
    "functions": symbol_pattern,
    "macros": symbol_pattern,
    "types": symbol_pattern,
    "literals": literal_pattern,
}


WORD_RE = re.compile(r"\w+")
STD_WORD_RE = re.compile(r"\bstd::(\w+)")


def check_mfbt_headers(path, raw_content, config):
    """Return list of (lineno, msg) for unused mfbt headers."""
    supported_keys = "variables", "functions", "macros", "types", "literals"
    unused = []
    words = None

    for header, categories in description.items():
        assert set(categories.keys()).issubset(supported_keys)

        if path.endswith(f"mfbt/{header}") or path.endswith(f"mfbt/{header[:-1]}.cpp"):
            continue

        headerline = rf'#\s*include "mozilla/{header}"'
        if not (match := re.search(headerline, raw_content)):
            continue

        quoted = f'"mozilla/{header}"'
        content = raw_content.replace(quoted, "")
        if re.search(rf"\w{re.escape(quoted)}|{re.escape(quoted)}\w", raw_content):
            content_words = None
        else:
            if words is None:
                words = Counter(WORD_RE.findall(raw_content))
            removed = Counter(WORD_RE.findall(quoted))
            count = raw_content.count(quoted)
            content_words = {w for w, n in words.items() if n > removed[w] * count}

        for category, pattern in categories_pattern.items():
            identifiers = categories.get(category, [])
            if pattern is symbol_pattern and content_words is not None:
                if not content_words.isdisjoint(identifiers):
                    break
            elif any(
                re.search(pattern.format(identifier), content)
                for identifier in identifiers
            ):
                break
        else:
            msg = f"{path} includes {header} but does not reference any of its API"
            lineno = 1 + raw_content.count("\n", 0, match.start())
            unused.append((lineno, msg))

    return unused


def lint_std_headers(path, raw_content, cxx_api):
    if re.search(r"\bnamespace\s+std\b", raw_content):
        used = set(WORD_RE.findall(raw_content))
    else:
        used = set(STD_WORD_RE.findall(raw_content))

    changes = []
    for header, symbols in cxx_api.items():
        headerline = rf"#\s*include <{header}>"
        if not (match := re.search(headerline, raw_content)):
            continue
        if not used.isdisjoint(symbols):
            continue

        msg = f"{path} includes <{header}> but does not reference any of its API"
        lineno = 1 + raw_content.count("\n", 0, match.start())
        changes.append((lineno, msg))

    return changes


def lint_cstd_headers(path, raw_content, c_api):
    used = None
    changes = []
    for header, symbols in c_api.items():
        headerline = rf"#\s*include <({header}|c{header[:-2]})>"
        if not (match := re.search(headerline, raw_content)):
            continue
        if used is None:
            used = set(WORD_RE.findall(raw_content))
        if not used.isdisjoint(symbols):
            continue

        msg = (
            f"{path} includes <{match.group(1)}> but does not reference any of its API"
        )
        lineno = 1 + raw_content.count("\n", 0, match.start())
        changes.append((lineno, msg))

    return changes


def lint(paths, config, **lintargs):
    import diskarzhan

    results = {"results": [], "fixed": 0}
    paths = list(expand_exclusions(paths, config, lintargs["root"]))
    fix = lintargs.get("fix")

    for path in paths:
        try:
            with open(path) as fd:
                raw_content = fd.read()
        except UnicodeDecodeError:
            continue

        mfbt_results = check_mfbt_headers(path, raw_content, config)
        diskarzhan_results = lint_std_headers(
            path, raw_content, diskarzhan.diskarzhan.cxx_api
        )
        diskarzhan_results += lint_cstd_headers(
            path, raw_content, diskarzhan.diskarzhan.c_api
        )

        all_results = mfbt_results + diskarzhan_results

        if fix:
            if all_results:
                diskarzhan.diskarzhan.fix_includes(path, raw_content, all_results)
                results["fixed"] += len(all_results)
        else:
            for lineno, msg in all_results:
                results["results"].append(
                    result.from_config(
                        config,
                        path=path,
                        message=msg,
                        level="error",
                        lineno=lineno,
                        diff=generate_diff(path, raw_content, lineno),
                    )
                )

    return results
