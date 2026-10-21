# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import buildconfig
from glean_parser import translate
from mozpack.copier import Jarrer
from mozpack.files import File, FileFinder

from mozbuild.util import get_rust_build_kind

APPSERVICES = "third_party/application-services"
IOS_RUST = f"{APPSERVICES}/megazords/ios-rust"
FRAMEWORK = "MozillaRustComponents.framework"
MEGAZORDS = {
    "MozillaRustComponents": (
        "megazord_ios",
        IOS_RUST,
        [
            "components/ads-client/metrics.yaml",
            "components/nimbus/metrics.yaml",
            "components/nimbus/pings.yaml",
            "components/logins/metrics.yaml",
            "components/sync_manager/metrics.yaml",
            "components/sync_manager/pings.yaml",
        ],
    ),
    "FocusRustComponents": (
        "megazord_focus",
        f"{IOS_RUST}/focus",
        [
            "components/nimbus/metrics.yaml",
            "components/nimbus/pings.yaml",
        ],
    ),
}


def external_symbols(archive):
    defined, undefined = set(), set()
    output = subprocess.check_output(
        [buildconfig.substs["LLVM_OBJDUMP"], "-t", str(archive)], text=True
    )
    for line in output.splitlines():
        fields = line.split()
        if "*UND*" in fields:
            undefined.add(fields[-1])
        elif len(fields) > 1 and fields[1] in ("g", "w"):
            defined.add(fields[-1])
    return defined, undefined


def check_nss_is_bundled(library, nss_defined):
    defined, undefined = external_symbols(library)
    if missing := sorted((undefined & nss_defined) - defined):
        raise Exception(
            f"{library.name} does not bundle NSS: {len(missing)} of its symbols "
            f"are undefined, starting with {missing[0]}"
        )


def generate_bindings(stage, bindgen, library, megazord_dir):
    substs = buildconfig.substs
    env = dict(os.environ)
    env["CARGO"] = substs["CARGO"]
    env["RUSTC"] = substs["RUSTC"]
    env["CARGO_HOME"] = str(Path(buildconfig.topobjdir) / ".cargo")
    env["CARGO_NET_OFFLINE"] = "true"

    def run_bindgen(out_dir, *flags):
        subprocess.check_call(
            [bindgen, "-l", library, "swift", out_dir, *flags],
            cwd=buildconfig.topsrcdir,
            env=env,
        )

    generated = stage / "Generated"
    run_bindgen(generated)
    headers = stage / FRAMEWORK / "Headers"
    headers.mkdir(parents=True)
    for header in generated.glob("*.h"):
        shutil.copyfile(header, headers / header.name)
    (generated / "glean_sym.swift").unlink(missing_ok=True)
    run_bindgen(
        stage / FRAMEWORK / "Modules",
        "--modulemap",
        "--xcframework",
        "--modulemap-filename",
        "module.modulemap",
    )

    srcdir = Path(buildconfig.topsrcdir)
    for header in (
        srcdir / megazord_dir / "MozillaRustComponents.h",
        srcdir
        / IOS_RUST
        / "Sources/MozillaRustComponentsWrapper/Viaduct/RustViaductFFI.h",
    ):
        shutil.copyfile(header, headers / header.name)

    modulemap = stage / FRAMEWORK / "Modules" / "module.modulemap"
    first, rest = modulemap.read_text(encoding="utf-8").split("\n", 1)
    with open(modulemap, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'{first}\n    header "RustViaductFFI.h"\n{rest}')


def generate_glean_metrics(stage, yaml_files):
    srcdir = Path(buildconfig.topsrcdir) / APPSERVICES
    if translate.translate(
        [srcdir / f for f in yaml_files],
        "swift",
        stage / "Generated" / "Metrics",
        {"glean_namespace": "Glean", "build_date": "0"},
    ):
        raise Exception("glean_parser failed to generate the Swift metrics")


def main(argv):
    parser = argparse.ArgumentParser(
        description="Package the application-services iOS megazords, their "
        "headers, module maps and generated Swift sources for one target."
    )
    parser.add_argument(
        "output_dir", help="Directory to write one zip archive per megazord to"
    )
    args = parser.parse_args(argv)

    substs = buildconfig.substs
    topobjdir = Path(buildconfig.topobjdir)
    profile = get_rust_build_kind(substs, profile_suffix="megazord")
    bindgen = (
        topobjdir
        / "dist/host/bin"
        / f"uniffi-bindgen-library-mode{substs['HOST_BIN_SUFFIX']}"
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    nss_library = (
        topobjdir
        / "dist/lib"
        / f"{substs['LIB_PREFIX']}nss_rustlib.{substs['LIB_SUFFIX']}"
    )
    nss_defined, _ = external_symbols(nss_library)
    if not nss_defined:
        raise Exception(f"Found no defined symbols in {nss_library.name}")

    for name, (crate, megazord_dir, yaml_files) in MEGAZORDS.items():
        library = (
            topobjdir
            / substs["RUST_TARGET"]
            / profile
            / f"{substs['LIB_PREFIX']}{crate}.{substs['LIB_SUFFIX']}"
        )
        check_nss_is_bundled(library, nss_defined)
        with tempfile.TemporaryDirectory() as tmpdir:
            stage = Path(tmpdir)
            if not substs.get("IPHONEOS_IS_SIMULATOR"):
                generate_bindings(stage, bindgen, library, megazord_dir)
                generate_glean_metrics(stage, yaml_files)
            jarrer = Jarrer()
            jarrer.add(f"{FRAMEWORK}/MozillaRustComponents", File(str(library)))
            for path, file in FileFinder(str(stage)).find("**"):
                jarrer.add(path, file)
            jarrer.copy(str(output_dir / f"{name}.zip"), skip_if_older=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
