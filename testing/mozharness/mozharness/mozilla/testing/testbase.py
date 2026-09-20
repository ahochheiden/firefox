#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import copy
import os
import platform
import shutil
import ssl
import urllib.parse
import urllib.request
from urllib.parse import ParseResult, urlparse

from mozharness.base.errors import BaseErrorList
from mozharness.base.log import WARNING
from mozharness.base.python import (
    ResourceMonitoringMixin,
    VirtualenvMixin,
    virtualenv_config_options,
)
from mozharness.lib.python.authentication import get_credentials
from mozharness.mozilla.automation import TBPL_WARNING, AutomationMixin
from mozharness.mozilla.structuredlog import StructuredOutputParser
from mozharness.mozilla.testing.unittest import DesktopUnittestOutputParser
from mozharness.mozilla.testing.verify_tools import (
    VerifyToolsMixin,
    verify_config_options,
)
from mozharness.mozilla.tooltool import TooltoolMixin

# https://searchfox.org/firefox-main/source/testing/config/tooltool-manifests
TOOLTOOL_PLATFORM_DIR = {
    "linux": "linux32",
    "linux64": "linux64",
    "win32": "win32",
    "win64": "win32",
    "macosx": "macosx64",
}


testing_config_options = (
    [
        [
            ["--installer-url"],
            {
                "action": "store",
                "dest": "installer_url",
                "default": None,
                "help": "URL to the installer to install",
            },
        ],
        [
            ["--installer-path"],
            {
                "action": "store",
                "dest": "installer_path",
                "default": None,
                "help": "Path to the installer to install. "
                "This is set automatically if run with --download-and-extract.",
            },
        ],
        [
            ["--binary-path"],
            {
                "action": "store",
                "dest": "binary_path",
                "default": None,
                "help": "Path to installed binary.  This is set automatically if run with --install.",  # NOQA: E501
            },
        ],
        [
            ["--exe-suffix"],
            {
                "action": "store",
                "dest": "exe_suffix",
                "default": None,
                "help": "Executable suffix for binaries on this platform",
            },
        ],
        [
            ["--test-url"],
            {
                "action": "store",
                "dest": "test_url",
                "default": None,
                "help": "URL to the zip file containing the actual tests",
            },
        ],
        [
            ["--test-packages-url"],
            {
                "action": "store",
                "dest": "test_packages_url",
                "default": None,
                "help": "URL to a json file describing which tests archives to download",
            },
        ],
        [
            ["--jsshell-url"],
            {
                "action": "store",
                "dest": "jsshell_url",
                "default": None,
                "help": "URL to the jsshell to install",
            },
        ],
        [
            ["--download-symbols"],
            {
                "action": "store",
                "dest": "download_symbols",
                "type": "choice",
                "choices": ["ondemand", "true"],
                "help": "Download and extract crash reporter symbols.",
            },
        ],
        [
            ["--restartAfterFailure"],
            {
                "action": "store_true",
                "default": False,
                "dest": "restartAfterFailure",
                "help": "Instruct the test harness to terminate on failure and restart where it left off",
            },
        ],
        [
            ["--restart-between-tests"],
            {
                "action": "store_true",
                "default": False,
                "dest": "restartBetweenTests",
                "help": "Restart the browser between each test to identify tests with undocumented dependencies",
            },
        ],
    ]
    + copy.deepcopy(virtualenv_config_options)
    + copy.deepcopy(verify_config_options)
)


# TestingMixin {{{1
class TestingMixin(
    VirtualenvMixin,
    AutomationMixin,
    ResourceMonitoringMixin,
    TooltoolMixin,
    VerifyToolsMixin,
):
    """
    The steps to identify + download the proper bits for [browser] unit
    tests and Talos.
    """

    installer_url = None
    installer_path = None
    binary_path = None
    test_url = None
    test_packages_url = None
    symbols_url = None
    symbols_path = None
    jsshell_url = None
    minidump_stackwalk_path = None
    ssl_context = None

    def query_build_dir_url(self, file_name):
        """
        Resolve a file name to a potential url in the build upload directory where
        that file can be found.
        """
        if self.test_packages_url:
            reference_url = self.test_packages_url
        elif self.installer_url:
            reference_url = self.installer_url
        else:
            self.fatal(
                "Can't figure out build directory urls without an installer_url "
                "or test_packages_url!"
            )

        reference_url = urllib.parse.unquote(reference_url)
        parts = list(urlparse(reference_url))

        last_slash = parts[2].rfind("/")
        parts[2] = "/".join([parts[2][:last_slash], file_name])

        url = ParseResult(*parts).geturl()

        return url

    def _pre_config_lock(self, rw_config):
        for i, (target_file, target_dict) in enumerate(
            rw_config.all_cfg_files_and_dicts
        ):
            if "developer_config" in target_file:
                self._developer_mode_changes(rw_config)

    def _developer_mode_changes(self, rw_config):
        """This function is called when you append the config called
        developer_config.py. This allows you to run a job
        outside of the Release Engineering infrastructure.

        What this functions accomplishes is:
        * --installer-url is set
        * --test-url is set if needed
        * every url is substituted by another external to the
            Release Engineering network
        """
        c = self.config
        orig_config = copy.deepcopy(c)
        self.actions = tuple(rw_config.actions)

        def _replace_url(url, changes):
            for from_, to_ in changes:
                if url.startswith(from_):
                    new_url = url.replace(from_, to_)
                    self.info("Replacing url %s -> %s" % (url, new_url))
                    return new_url
            return url

        if c.get("installer_url") is None:
            self.exception("You must use --installer-url with developer_config.py")
        if c.get("require_test_zip"):
            if not c.get("test_url") and not c.get("test_packages_url"):
                self.exception(
                    "You must use --test-url or --test-packages-url with "
                    "developer_config.py"
                )

        c["installer_url"] = _replace_url(c["installer_url"], c["replace_urls"])
        if c.get("test_url"):
            c["test_url"] = _replace_url(c["test_url"], c["replace_urls"])
        if c.get("test_packages_url"):
            c["test_packages_url"] = _replace_url(
                c["test_packages_url"], c["replace_urls"]
            )

        for key, value in self.config.items():
            if type(value) is str and value.startswith("http"):
                self.config[key] = _replace_url(value, c["replace_urls"])

        # Any changes to c means that we need credentials
        if not c == orig_config:
            get_credentials()

    def _urlopen(self, url, **kwargs):
        """
        This function helps dealing with downloading files while outside
        of the releng network.
        """

        # Code based on http://code.activestate.com/recipes/305288-http-basic-authentication
        def _urlopen_basic_auth(url, **kwargs):
            self.info("We want to download this file %s" % url)
            if not hasattr(self, "https_username"):
                self.info(
                    "NOTICE: Files downloaded from outside of "
                    "Release Engineering network require LDAP "
                    "credentials."
                )

            self.https_username, self.https_password = get_credentials()
            # This creates a password manager
            passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            # Because we have put None at the start it will use this username/password
            # combination from here on
            passman.add_password(None, url, self.https_username, self.https_password)
            authhandler = urllib.request.HTTPBasicAuthHandler(passman)

            return urllib.request.build_opener(authhandler).open(url, **kwargs)

        # If we have the developer_run flag enabled then we will switch
        # URLs to the right place and enable http authentication
        if "developer_config.py" in self.config["config_files"]:
            return _urlopen_basic_auth(url, **kwargs)
        # windows certificates need to be refreshed (https://bugs.python.org/issue36011)
        elif self.platform_name() in ("win64",) and platform.architecture()[0] in (
            "x64",
        ):
            if self.ssl_context is None:
                self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS)
                self.ssl_context.load_default_certs()
            return urllib.request.urlopen(url, context=self.ssl_context, **kwargs)
        else:
            return urllib.request.urlopen(url, **kwargs)

    def _query_binary_version(self, regex, cmd):
        output = self.get_output_from_command(cmd, silent=False)
        return regex.search(output).group(0)

    def preflight_download_and_extract(self):
        message = ""
        if not self.installer_url:
            message += """installer_url isn't set!

You can set this by specifying --installer-url URL
"""
        if (
            self.config.get("require_test_zip")
            and not self.test_url
            and not self.test_packages_url
        ):
            message += """test_url isn't set!

You can set this by specifying --test-url URL
"""
        if message:
            self.fatal(message + "Can't run download-and-extract... exiting")

    def structured_output(self, suite_category):
        unstructured_suites = self.config.get("unstructured_suites", [])
        return suite_category not in unstructured_suites

    def get_test_output_parser(
        self,
        suite_category,
        strict=False,
        fallback_parser_class=DesktopUnittestOutputParser,
        **kwargs,
    ):
        """Derive and return an appropriate output parser, either the structured
        output parser or a fallback based on the type of logging in use as determined by
        configuration.
        """
        if not self.structured_output(suite_category):
            if fallback_parser_class is DesktopUnittestOutputParser:
                return DesktopUnittestOutputParser(
                    suite_category=suite_category, **kwargs
                )
            return fallback_parser_class(**kwargs)
        self.info("Structured output parser in use for %s." % suite_category)
        return StructuredOutputParser(
            suite_category=suite_category, strict=strict, **kwargs
        )

    def _fetched_tests_dirs(self):
        dirs = self.query_abs_dirs()
        tests = os.path.join(dirs["abs_fetches_dir"], "tests")
        test_install_dir = dirs.get(
            "abs_test_install_dir", os.path.join(dirs["abs_work_dir"], "tests")
        )
        return tests, test_install_dir

    def clobber(self):
        tests, test_install_dir = self._fetched_tests_dirs()
        if os.path.isdir(test_install_dir) and not os.path.exists(tests):
            shutil.move(test_install_dir, tests)
        super().clobber()

    def download_and_extract(self):
        """
        Take the installer, the tests and the symbols from the fetches directory
        """
        dirs = self.query_abs_dirs()
        installer = os.path.basename(
            urllib.parse.unquote(urlparse(self.installer_url).path)
        )
        self.installer_path = os.path.join(dirs["abs_fetches_dir"], installer)
        tests, test_install_dir = self._fetched_tests_dirs()
        if os.path.isdir(tests):
            self.mkdir_p(os.path.dirname(test_install_dir))
            try:
                os.rename(tests, test_install_dir)
            except OSError:
                self.warning(f"Copying {tests} to {test_install_dir}, rename failed")
                shutil.move(tests, test_install_dir)
        elif not os.path.isdir(test_install_dir):
            self.fatal(f"Neither {tests} nor {test_install_dir} exists")
        download_symbols = self.config.get("download_symbols")
        if download_symbols == "ondemand":
            self.symbols_url = os.environ.get("MOZ_SYMBOLS_URL")
            if self.symbols_url:
                self.symbols_path = self.symbols_url
            else:
                self.warning(
                    "No symbols_url found. Let minidump-stackwalk query for symbols."
                )
        elif download_symbols:
            self.symbols_url = os.environ["MOZ_SYMBOLS_URL"]
            self.symbols_path = os.path.join(dirs["abs_fetches_dir"], "symbols")

    # create_virtualenv is in VirtualenvMixin.

    def preflight_install(self):
        if not self.installer_path:
            if self.config.get("installer_path"):
                self.installer_path = self.config["installer_path"]
            else:
                self.fatal(
                    """installer_path isn't set!

You can set this by:

1. specifying --installer-path PATH, or
2. running the download-and-extract action
"""
                )
        if not self.is_python_package_installed("mozInstall"):
            self.fatal(
                """Can't call install() without mozinstall!
Did you run with --create-virtualenv? Is mozinstall in virtualenv_modules?"""
            )

    def install_app(self, app=None, target_dir=None, installer_path=None):
        """Dependent on mozinstall"""
        # install the application
        cmd = [self.query_python_path("mozinstall")]
        if app:
            cmd.extend(["--app", app])
        dirs = self.query_abs_dirs()
        if not target_dir:
            target_dir = dirs.get(
                "abs_app_install_dir", os.path.join(dirs["abs_work_dir"], "application")
            )
        self.mkdir_p(target_dir)
        if not installer_path:
            installer_path = self.installer_path
        cmd.extend([installer_path, "--destination", target_dir])
        # TODO we'll need some error checking here
        return self.get_output_from_command(
            cmd, halt_on_failure=True, fatal_exit_code=3
        )

    def install(self):
        self.binary_path = self.install_app(app=self.config.get("application"))
        self.install_dir = os.path.dirname(self.binary_path)

    def uninstall_app(self, install_dir=None):
        """Dependent on mozinstall"""
        # uninstall the application
        cmd = self.query_exe(
            "mozuninstall",
            default=self.query_python_path("mozuninstall"),
            return_type="list",
        )
        dirs = self.query_abs_dirs()
        if not install_dir:
            install_dir = dirs.get(
                "abs_app_install_dir", os.path.join(dirs["abs_work_dir"], "application")
            )
        cmd.append(install_dir)
        # TODO we'll need some error checking here
        self.get_output_from_command(cmd, halt_on_failure=True, fatal_exit_code=3)

    def uninstall(self):
        self.uninstall_app()

    def query_minidump_stackwalk(self, manifest=None):
        if self.minidump_stackwalk_path:
            return self.minidump_stackwalk_path

        minidump_stackwalk_path = None

        if "MOZ_FETCHES_DIR" in os.environ:
            minidump_stackwalk_path = os.path.join(
                os.environ["MOZ_FETCHES_DIR"],
                "minidump-stackwalk",
                "minidump-stackwalk",
            )

            if self.platform_name() in ("win32", "win64"):
                minidump_stackwalk_path += ".exe"

        if not minidump_stackwalk_path or not os.path.isfile(minidump_stackwalk_path):
            self.error("minidump-stackwalk path was not fetched?")
            # don't burn the job but we should at least turn them orange so it is caught
            self.record_status(TBPL_WARNING, WARNING)
            return None

        self.minidump_stackwalk_path = minidump_stackwalk_path
        return self.minidump_stackwalk_path

    def query_options(self, *args, **kwargs):
        if "str_format_values" in kwargs:
            str_format_values = kwargs.pop("str_format_values")
        else:
            str_format_values = {}

        arguments = []

        for arg in args:
            if arg is not None:
                arguments.extend(argument % str_format_values for argument in arg)

        return arguments

    def query_tests_args(self, *args, **kwargs):
        if "str_format_values" in kwargs:
            str_format_values = kwargs.pop("str_format_values")
        else:
            str_format_values = {}

        arguments = []

        for arg in reversed(args):
            if arg:
                arguments.append("--")
                arguments.extend(argument % str_format_values for argument in arg)
                break

        return arguments

    def _run_cmd_checks(self, suites):
        if not suites:
            return
        dirs = self.query_abs_dirs()
        for suite in suites:
            # XXX platform.architecture() may give incorrect values for some
            # platforms like mac as excutable files may be universal
            # files containing multiple architectures
            # NOTE 'enabled' is only here while we have unconsolidated configs
            if not suite["enabled"]:
                continue
            if suite.get("architectures"):
                arch = platform.architecture()[0]
                if arch not in suite["architectures"]:
                    continue
            cmd = suite["cmd"]
            name = suite["name"]
            self.info(
                "Running pre test command %(name)s with '%(cmd)s'"
                % {"name": name, "cmd": " ".join(cmd)}
            )
            self.run_command(
                cmd,
                cwd=dirs["abs_work_dir"],
                error_list=BaseErrorList,
                halt_on_failure=suite["halt_on_failure"],
                fatal_exit_code=suite.get("fatal_exit_code", 3),
            )

    def preflight_run_tests(self):
        """preflight commands for all tests"""
        c = self.config
        if c.get("skip_preflight"):
            self.info("skipping preflight")
            return

        if c.get("run_cmd_checks_enabled"):
            self._run_cmd_checks(c.get("preflight_run_cmd_suites", []))
        elif c.get("preflight_run_cmd_suites"):
            self.warning(
                "Proceeding without running prerun test commands."
                " These are often OS specific and disabling them may"
                " result in spurious test results!"
            )

    def postflight_run_tests(self):
        """preflight commands for all tests"""
        c = self.config
        if c.get("run_cmd_checks_enabled"):
            self._run_cmd_checks(c.get("postflight_run_cmd_suites", []))

    def query_abs_dirs(self):
        abs_dirs = super().query_abs_dirs()
        if "MOZ_FETCHES_DIR" in os.environ:
            abs_dirs["abs_fetches_dir"] = os.environ["MOZ_FETCHES_DIR"]
        return abs_dirs
