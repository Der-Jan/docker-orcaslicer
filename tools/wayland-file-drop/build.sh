#!/bin/sh
set -eu
cd "$(dirname "$0")"
for protocol in layer-shell virtual-pointer xdg-shell; do
    wayland-scanner client-header "$protocol.xml" "$protocol.h"
    wayland-scanner private-code "$protocol.xml" "$protocol.c"
done
cc -Wall -Wextra -Werror -O2 drop.c layer-shell.c virtual-pointer.c xdg-shell.c \
    -o wayland-file-drop $(pkg-config --cflags --libs wayland-client)
