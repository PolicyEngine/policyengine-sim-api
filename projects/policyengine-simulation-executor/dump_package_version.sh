#!/bin/bash

script_dir=$(dirname "$0")
cd ${script_dir}
uv install &> /dev/null
python scripts/dump_package_version.py uv.lock "$@"
