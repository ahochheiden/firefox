# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import locale
import os
import re
from collections import defaultdict
from contextlib import contextmanager

from mozlint import result
from mozlint.pathutils import get_ancestors_by_name
from yamllint import linter
from yamllint.cli import find_files_recursively, find_project_config_filepath
from yamllint.config import YamlLintConfig

YAMLLINT_MESSAGE_REGEX = re.compile(r"(.*) \((.*)\)$")


def get_yamllint_version():
    from yamllint import APP_VERSION

    return APP_VERSION


def load_config(conf_file):
    if conf_file != "default":
        return YamlLintConfig(file=conf_file)

    if project_config := find_project_config_filepath():
        return YamlLintConfig(file=project_config)

    if "YAMLLINT_CONFIG_FILE" in os.environ:
        user_global_config = os.path.expanduser(os.environ["YAMLLINT_CONFIG_FILE"])
    elif "XDG_CONFIG_HOME" in os.environ:
        user_global_config = os.path.join(
            os.environ["XDG_CONFIG_HOME"], "yamllint", "config"
        )
    else:
        user_global_config = os.path.expanduser("~/.config/yamllint/config")
    if os.path.isfile(user_global_config):
        return YamlLintConfig(file=user_global_config)

    return YamlLintConfig("extends: default")


@contextmanager
def scoped_locale(name):
    if name is None:
        yield
        return
    previous = locale.setlocale(locale.LC_ALL)
    locale.setlocale(locale.LC_ALL, name)
    try:
        yield
    finally:
        locale.setlocale(locale.LC_ALL, previous)


def lint(files, config, **lintargs):
    log = lintargs["log"]
    root = lintargs["root"]

    log.debug(f"Version: {get_yamllint_version()}")

    # Run any paths with a .yamllint file in the directory separately so
    # it gets picked up. This means only .yamllint files that live in
    # directories that are explicitly included will be considered.
    paths_by_config = defaultdict(list)
    for f in files:
        conf_files = get_ancestors_by_name(".yamllint", f, root)
        paths_by_config[conf_files[0] if conf_files else "default"].append(f)

    results = []
    for conf_file, paths in paths_by_config.items():
        conf = load_config(conf_file)
        with scoped_locale(conf.locale):
            for path in find_files_recursively(paths, conf):
                with open(path, "rb") as fh:
                    problems = list(linter.run(fh, conf, path.removeprefix("./")))
                for problem in problems:
                    if match := YAMLLINT_MESSAGE_REGEX.match(problem.message):
                        message, rule = match.groups()
                    else:
                        message, rule = problem.message, None
                    results.append(
                        result.from_config(
                            config,
                            path=os.path.relpath(path, root),
                            message=message,
                            level="error",
                            lineno=problem.line,
                            column=problem.column,
                            rule=rule,
                        )
                    )

    return results
