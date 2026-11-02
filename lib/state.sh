#!/usr/bin/env bash
# Canonical per-user Little Mester state root shared by host launchers.
# The default is ~/.local/share/little-mester. XDG_DATA_HOME and an explicit
# MESTER_STATE_DIR may relocate the whole root without tying it to a checkout.
: "${MESTER_STATE_DIR:=${XDG_DATA_HOME:-$HOME/.local/share}/little-mester}"
