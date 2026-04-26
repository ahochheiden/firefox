# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Compute package and langpack file names from configure substs and a
# locale code.


_FORMAT_SUFFIX = {
    "TAR": ".tar",
    "TGZ": ".tar.gz",
    "XZ": ".tar.xz",
    "BZ2": ".tar.bz2",
    "ZIP": ".zip",
    "DMG": ".dmg",
    "APK": "",
}


def pkg_basename(substs, ab_cd):
    simple = substs.get("MOZ_SIMPLE_PACKAGE_NAME")
    if simple:
        return simple
    return (
        f"{substs['MOZ_PKG_APPNAME']}-{substs['MOZ_PKG_VERSION']}"
        f".{ab_cd}.{substs['MOZ_PKG_PLATFORM']}"
    )


def pkg_path(substs):
    return ""


def pkg_suffix(substs):
    return _FORMAT_SUFFIX[substs["MOZ_PKG_FORMAT"]]


def pkg_langpack_basename(substs, ab_cd):
    simple = substs.get("MOZ_SIMPLE_PACKAGE_NAME")
    if simple:
        return f"{simple}.langpack"
    return f"{substs['MOZ_PKG_APPNAME']}-{substs['MOZ_PKG_VERSION']}.{ab_cd}.langpack"


def pkg_langpack_path(substs):
    if substs.get("MOZ_SIMPLE_PACKAGE_NAME"):
        return ""
    return f"{substs['MOZ_PKG_PLATFORM']}/xpi/"


def pkg_inst_basename(substs, ab_cd):
    return f"{pkg_basename(substs, ab_cd)}.installer"


def langpack_eid(substs, ab_cd):
    return f"langpack-{ab_cd}@{substs['MOZ_LANGPACK_EID_HOST']}"
