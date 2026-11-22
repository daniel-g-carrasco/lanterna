#!/bin/bash
# The README's pictures, from the real window driven with the tests' fake TV, on a Broadway display of its own
# (never in the desktop session), without the user's GTK CSS and accent:
#   gtk4-broadwayd --address 127.0.0.1 :13 &
#   tools/screenshots/run.sh /tmp/frames light          (DEMO_SCALE=2 for the still pictures)
#   python3 tools/screenshots/compose.py /tmp/frames photos data/screenshots/usage-photos.gif
set -e
cd "$(dirname "$0")"
empty="$(mktemp -d)"
exec env -u WAYLAND_DISPLAY -u DISPLAY XDG_CONFIG_HOME="$empty" ADW_DISABLE_PORTAL=1 GDK_BACKEND=broadway \
  BROADWAY_DISPLAY="${BROADWAY_DISPLAY:-:13}" "${PYTHON:-python3}" demo.py "$@"
