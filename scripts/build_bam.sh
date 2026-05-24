#!/bin/bash

set -ex

script_dir=$(dirname $(realpath $0))
cd "${script_dir}/../bam"

usage() {
    echo "Usage: $0 [-g] [--clean]"
    echo "  -g       Set build type to Debug"
    echo "  --clean  Clean build"
    exit 1
}

build_type="Release"
clean_flag=false
while [[ "$1" != "" ]]; do
    case "$1" in
    -g)
        build_type="Debug"
        shift
        ;;
    --clean)
        clean_flag=true
        shift
        ;;
    *)
        usage
        exit 1
        ;;
    esac
done

install_dir=$(realpath "./${build_type}")
build_dir=build
if [[ "$clean_flag" == true ]]; then
    rm -rf $install_dir
    rm -rf $build_dir
fi
mkdir -p $build_dir && cd $build_dir

cmake .. \
    -DCMAKE_INSTALL_PREFIX=$install_dir \
    -DCMAKE_BUILD_TYPE=$build_type

make -j"$(nproc)" install
