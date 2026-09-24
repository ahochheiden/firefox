#!/bin/sh

set -e
set -x

export GRADLE_MAVEN_REPOSITORIES="file://$MOZ_FETCHES_DIR/geckoview","file://$MOZ_FETCHES_DIR/android-gradle-dependencies/mozilla","file://$MOZ_FETCHES_DIR/android-gradle-dependencies/google","file://$MOZ_FETCHES_DIR/android-gradle-dependencies/central","file://$MOZ_FETCHES_DIR/android-gradle-dependencies/gradle-plugins"
GRADLE=$MOZ_FETCHES_DIR/android-gradle-dependencies/gradle-dist/bin/gradle

export MOZ_OBJDIR=/builds/worker/workspace/obj-build

./mach configure

# Partial build ensures that `moz.build` generated code is in place.
./mach build pre-export export -v

eval $PRE_GRADLEW

eval $GET_SECRETS

mkdir -p "$BUILD_METRICS_DIR"
$GRADLE listRepositories $GRADLEW_ARGS -PbuildMetricsOutputDir="$BUILD_METRICS_DIR"

eval $POST_GRADLEW
