# Build a minimal desktop-session environment from validated owner-owned sockets.
# Caller fixes PATH and passes the effective UID and the expected runtime path.
vm_collect_desktop_env() {
  local desktop_uid="$1" desktop_runtime="$2" runtime_owner_mode bus_owner wayland_owner
  desktop_env=()
  runtime_owner_mode="$(/usr/bin/stat -c '%u:%a' -- "$desktop_runtime" 2>/dev/null || true)"
  if [[ "${XDG_RUNTIME_DIR-}" == "$desktop_runtime" && -d "$desktop_runtime" \
     && ! -L "$desktop_runtime" && "$runtime_owner_mode" == "$desktop_uid:700" ]]; then
    desktop_env+=("XDG_RUNTIME_DIR=$desktop_runtime")
    bus_owner="$(/usr/bin/stat -c '%u' -- "$desktop_runtime/bus" 2>/dev/null || true)"
    if [[ "${DBUS_SESSION_BUS_ADDRESS-}" == "unix:path=$desktop_runtime/bus" \
       && -S "$desktop_runtime/bus" && ! -L "$desktop_runtime/bus" \
       && "$bus_owner" == "$desktop_uid" ]]; then
      desktop_env+=("DBUS_SESSION_BUS_ADDRESS=unix:path=$desktop_runtime/bus")
    fi
  fi
  if [[ "${DISPLAY-}" =~ ^([A-Za-z0-9._/-]+)?:[0-9]{1,5}(\.[0-9]{1,3})?$ ]]; then
    desktop_env+=("DISPLAY=$DISPLAY")
  fi
  if [[ "${XAUTHORITY-}" =~ ^/[A-Za-z0-9._/-]+$ && "/${XAUTHORITY#/}/" != *"/../"* \
     && -f "$XAUTHORITY" && ! -L "$XAUTHORITY" ]]; then
    local xauthority_owner_mode
    xauthority_owner_mode="$(/usr/bin/stat -c '%u:%a' -- "$XAUTHORITY" 2>/dev/null || true)"
    if [[ "$xauthority_owner_mode" == "$desktop_uid:600" || "$xauthority_owner_mode" == "$desktop_uid:400" ]]; then
      desktop_env+=("XAUTHORITY=$XAUTHORITY")
    fi
  fi
  if [[ "${WAYLAND_DISPLAY-}" =~ ^[A-Za-z0-9_.-]{1,128}$ \
     && "${XDG_RUNTIME_DIR-}" == "$desktop_runtime" \
     && -S "$desktop_runtime/$WAYLAND_DISPLAY" && ! -L "$desktop_runtime/$WAYLAND_DISPLAY" ]]; then
    wayland_owner="$(/usr/bin/stat -c '%u' -- "$desktop_runtime/$WAYLAND_DISPLAY" 2>/dev/null || true)"
    if [[ "$wayland_owner" == "$desktop_uid" ]]; then
      desktop_env+=("WAYLAND_DISPLAY=$WAYLAND_DISPLAY")
    fi
  fi
}
