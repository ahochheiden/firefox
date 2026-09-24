#!/bin/bash -vex

set -x -e

echo "running as" $(id)

set -v

cd $GECKO_PATH

. taskcluster/scripts/misc/android-gradle-dependencies/before.sh

./mach build

./mach android gradle-dependencies

. taskcluster/scripts/misc/android-gradle-dependencies/after.sh
