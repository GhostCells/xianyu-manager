#!/bin/zsh
set -eu
launcher_dir="${0:A:h}"
/usr/bin/python3 "$launcher_dir/novnc_launcher.py" --launch
