#!/usr/bin/env bash
# recursion.sh — bounded VM recursion policy shared by the lab-vm CLI/tests.

VM_RECURSION_MAX_SUPPORTED=32

vm_recursion_apply_guest_identity() {
  local identity_file="${1:-/etc/lab-vm-recursion}"
  local depth maximum extra
  [[ -e "$identity_file" || -L "$identity_file" ]] || return 0
  [[ -f "$identity_file" && ! -L "$identity_file" ]] || {
    echo "REFUSED: persisted recursion identity is not a regular file: $identity_file" >&2
    return 1
  }
  read -r depth maximum extra <"$identity_file" || {
    echo "REFUSED: cannot read persisted recursion identity: $identity_file" >&2
    return 1
  }
  [[ -z "$extra" && "$depth" =~ ^(0|[1-9][0-9]*)$ && "$maximum" =~ ^(0|[1-9][0-9]*)$ ]] || {
    echo "REFUSED: invalid persisted recursion identity: $identity_file" >&2
    return 1
  }
  (( depth <= VM_RECURSION_MAX_SUPPORTED && maximum <= VM_RECURSION_MAX_SUPPORTED && depth <= maximum )) || {
    echo "REFUSED: persisted recursion identity exceeds the supported policy: $identity_file" >&2
    return 1
  }
  if [[ -n "${LAB_VM_RECURSION_DEPTH+x}" && "$LAB_VM_RECURSION_DEPTH" != "$depth" ]] || \
     [[ -n "${LAB_VM_MAX_DEPTH+x}" && "$LAB_VM_MAX_DEPTH" != "$maximum" ]]; then
    echo "REFUSED: environment recursion settings do not match the guest's persisted identity." >&2
    return 1
  fi
  export LAB_VM_RECURSION_DEPTH="$depth" LAB_VM_MAX_DEPTH="$maximum"
}

vm_recursion_paths_overlap() {
  local workspace state
  workspace="$(realpath "${1:?workspace required}")" || return 2
  state="$(realpath -m "${2:?VM state path required}")" || return 2
  [[ "$workspace" == "$state" || "$workspace" == "$state"/* || "$state" == "$workspace"/* ]]
}

vm_recursion_validate_state_boundary() {
  local workspace="${1:?workspace required}" state="${2:?VM state path required}" legacy
  if vm_recursion_paths_overlap "$workspace" "$state"; then
    echo "REFUSED: selected workspace overlaps private VM state ($state); move VM state outside the workspace before sharing it with a guest." >&2
    return 1
  fi
  [[ -n "${LAB_DIR:-}" ]] || return 0
  for legacy in "$LAB_DIR/.mester/vm" "$LAB_DIR/.mester/vm-services" "$LAB_DIR/.mester/logs"; do
    [[ -e "$legacy" || -L "$legacy" ]] || continue
    if vm_recursion_paths_overlap "$workspace" "$legacy"; then
      echo "REFUSED: selected workspace overlaps legacy host runtime state ($legacy); move that state outside the workspace before sharing it with a guest." >&2
      return 1
    fi
  done
}

vm_recursion_configure() {
  local command="${1:-}" parent_depth=0 inherited_max=0
  local has_parent_depth=0

  if [[ "${VM_UNSAFE_RECURSE:-0}" == 1 ]]; then
    VM_RECURSE=1
  fi

  if [[ -n "${LAB_VM_RECURSION_DEPTH+x}" ]]; then
    has_parent_depth=1
    parent_depth="$LAB_VM_RECURSION_DEPTH"
    if [[ ! "$parent_depth" =~ ^(0|[1-9][0-9]*)$ ]] || (( parent_depth > VM_RECURSION_MAX_SUPPORTED )); then
      echo "REFUSED: invalid LAB_VM_RECURSION_DEPTH '$parent_depth'; expected 0-$VM_RECURSION_MAX_SUPPORTED." >&2
      return 1
    fi
    inherited_max="${LAB_VM_MAX_DEPTH:-0}"
    if [[ ! "$inherited_max" =~ ^(0|[1-9][0-9]*)$ ]] || (( inherited_max > VM_RECURSION_MAX_SUPPORTED )); then
      echo "REFUSED: invalid LAB_VM_MAX_DEPTH '$inherited_max'; expected 0-$VM_RECURSION_MAX_SUPPORTED." >&2
      return 1
    fi
  elif [[ -n "${LAB_VM_MAX_DEPTH+x}" ]]; then
    echo 'REFUSED: LAB_VM_MAX_DEPTH is only valid with an inherited LAB_VM_RECURSION_DEPTH; set the limit with --max-depth at the outer launcher.' >&2
    return 1
  fi

  if [[ "${VM_MAX_DEPTH_EXPLICIT:-0}" == 1 ]]; then
    if [[ "${VM_RECURSE:-0}" != 1 ]]; then
      echo '--max-depth requires --recurse or --unsafe-recurse.' >&2
      return 1
    fi
    if [[ ! "${VM_MAX_DEPTH:-}" =~ ^(0|[1-9][0-9]*)$ ]] || (( VM_MAX_DEPTH > VM_RECURSION_MAX_SUPPORTED )); then
      echo "REFUSED: --max-depth must be an integer from 0 to $VM_RECURSION_MAX_SUPPORTED." >&2
      return 1
    fi
    if (( has_parent_depth == 1 )) && (( VM_MAX_DEPTH > inherited_max )); then
      echo "REFUSED: a child cannot exceed inherited maximum depth $inherited_max." >&2
      return 1
    fi
    VM_EFFECTIVE_MAX_DEPTH="$VM_MAX_DEPTH"
  elif (( has_parent_depth == 1 )); then
    VM_EFFECTIVE_MAX_DEPTH="$inherited_max"
  else
    VM_EFFECTIVE_MAX_DEPTH=0
  fi

  if (( has_parent_depth == 1 )) && (( VM_EFFECTIVE_MAX_DEPTH < parent_depth )); then
    echo "REFUSED: maximum depth $VM_EFFECTIVE_MAX_DEPTH is below the current recursion depth $parent_depth." >&2
    return 1
  fi

  VM_NESTED_LAUNCH=0
  VM_GUEST_RECURSION_DEPTH=0
  if [[ "$command" == run || "$command" == up ]]; then
    if (( has_parent_depth == 1 )); then
      if [[ "${VM_RECURSE:-0}" != 1 ]]; then
        echo 'REFUSED: Nested VM launch requires --recurse or --unsafe-recurse.' >&2
        return 1
      fi
      if (( parent_depth >= VM_EFFECTIVE_MAX_DEPTH )); then
        echo "REFUSED: maximum nesting depth $VM_EFFECTIVE_MAX_DEPTH reached; no child VM or agent was started." >&2
        return 1
      fi
      VM_NESTED_LAUNCH=1
      VM_GUEST_RECURSION_DEPTH=$((parent_depth + 1))
    fi
  elif [[ "${VM_RECURSE:-0}" == 1 || "${VM_MAX_DEPTH_EXPLICIT:-0}" == 1 ]]; then
    echo '--recurse and --max-depth are only valid with run or up.' >&2
    return 1
  fi

  return 0
}

vm_recursion_workspace_is_repo_related() {
  local workspace repo
  workspace="$(realpath "${1:?workspace required}")" || return 1
  repo="$(realpath "${2:?repository required}")" || return 1
  [[ "$workspace" == "$repo" || "$workspace" == "$repo"/* || "$repo" == "$workspace"/* ]]
}

vm_recursion_validate_workspace() {
  local workspace repo
  workspace="$(realpath "${1:?workspace required}")" || return 1
  repo="$(realpath "${2:?repository required}")" || return 1
  if [[ "${VM_UNSAFE_RECURSE:-0}" == 1 ]] && [[ "$workspace" != "$repo" && "$repo" != "$workspace"/* ]]; then
    echo 'REFUSED: --unsafe-recurse requires a workspace containing the Little Mester checkout.' >&2
    return 1
  fi
}

vm_recursion_require_nested_kvm() {
  if ! _vm_kvm_available; then
    echo 'REFUSED: nested VM launch requires usable /dev/kvm in the current guest; nested virtualization is unavailable, so no child VM or agent was started.' >&2
    return 1
  fi
  if ! _vm_nested_cpu_virtualization_available; then
    echo 'REFUSED: nested VM launch requires a guest CPU with vmx or svm exposed; no child VM or agent was started.' >&2
    return 1
  fi
}

_vm_nested_cpu_virtualization_available() {
  grep -Eq '^flags[[:space:]]*:.*[[:space:]](vmx|svm)([[:space:]]|$)' /proc/cpuinfo 2>/dev/null
}

vm_recursion_store_profile() {
  local vm_dir="${1:?VM profile directory required}" policy_tmp state_possible=0
  [[ ! -L "$vm_dir" ]] || {
    echo "REFUSED: VM profile directory must not be a symlink: $vm_dir" >&2
    return 1
  }
  mkdir -p "$vm_dir"
  if [[ -e "$vm_dir/recursion_policy" || -L "$vm_dir/recursion_policy" ]]; then
    [[ -f "$vm_dir/recursion_policy" && ! -L "$vm_dir/recursion_policy" ]] || {
      echo "REFUSED: recursion metadata must be a regular file in VM profile: $vm_dir" >&2
      return 1
    }
    vm_recursion_load_profile "$vm_dir" || return 1
    state_possible="$VM_AGENT_RECURSION_STATE_POSSIBLE"
  fi
  if (( VM_EFFECTIVE_MAX_DEPTH > VM_GUEST_RECURSION_DEPTH )); then
    state_possible=1
  fi
  policy_tmp="$vm_dir/.recursion_policy.tmp.$$"
  printf '%s %s %s\n' "${VM_GUEST_RECURSION_DEPTH:-0}" "${VM_EFFECTIVE_MAX_DEPTH:-0}" "$state_possible" >"$policy_tmp"
  chmod 600 "$policy_tmp"
  mv -f -- "$policy_tmp" "$vm_dir/recursion_policy"
}

vm_recursion_clear_profile() {
  local vm_dir="${1:?VM profile directory required}"
  [[ ! -L "$vm_dir" && ! -L "$vm_dir/recursion_policy" ]] || {
    echo "REFUSED: cannot safely clear recursive policy from symlinked VM profile: $vm_dir" >&2
    return 1
  }
  rm -f -- "$vm_dir/recursion_policy"
}

vm_recursion_session_dir() {
  local profile="${1:?VM profile name required}"
  [[ "$profile" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  printf '%s/.cache/lab-vm-recursion/%s\n' "${HOME%/}" "$profile"
}

vm_recursion_load_profile() {
  local vm_dir="${1:?VM profile directory required}" state_field="" metadata_extra="" state_field_present=0
  VM_AGENT_RECURSION_DEPTH=0
  VM_AGENT_MAX_DEPTH=0
  VM_AGENT_RECURSION_STATE_POSSIBLE=0
  if [[ -e "$vm_dir/recursion_policy" || -L "$vm_dir/recursion_policy" ]]; then
    [[ -f "$vm_dir/recursion_policy" && ! -L "$vm_dir/recursion_policy" ]] || {
      echo "REFUSED: incomplete recursion metadata in VM profile: $vm_dir" >&2
      return 1
    }
    read -r VM_AGENT_RECURSION_DEPTH VM_AGENT_MAX_DEPTH state_field metadata_extra <"$vm_dir/recursion_policy"
    [[ -z "$metadata_extra" ]] || {
      echo "REFUSED: invalid recursion metadata in VM profile: $vm_dir" >&2
      return 1
    }
    if [[ -z "$state_field" ]]; then
      state_field=0
    elif [[ ! "$state_field" =~ ^[01]$ ]]; then
      echo "REFUSED: invalid recursion metadata in VM profile: $vm_dir" >&2
      return 1
    else
      state_field_present=1
    fi
  fi
  if [[ ! "$VM_AGENT_RECURSION_DEPTH" =~ ^(0|[1-9][0-9]*)$ || ! "$VM_AGENT_MAX_DEPTH" =~ ^(0|[1-9][0-9]*)$ ]] || \
    (( VM_AGENT_RECURSION_DEPTH > VM_RECURSION_MAX_SUPPORTED || VM_AGENT_MAX_DEPTH > VM_RECURSION_MAX_SUPPORTED || VM_AGENT_RECURSION_DEPTH > VM_AGENT_MAX_DEPTH )); then
    echo "REFUSED: invalid recursion metadata in VM profile: $vm_dir" >&2
    return 1
  fi
  if (( VM_AGENT_MAX_DEPTH > VM_AGENT_RECURSION_DEPTH )); then
    if (( state_field_present == 1 )) && [[ "$state_field" != 1 ]]; then
      echo "REFUSED: invalid recursion metadata in VM profile: $vm_dir" >&2
      return 1
    fi
    state_field=1
  fi
  VM_AGENT_RECURSION_STATE_POSSIBLE="$state_field"
}

vm_recursion_require_discard_ack() {
  local vm_dir="${1:?VM profile directory required}"
  [[ ! -L "$vm_dir" && ! -L "$vm_dir/recursion_policy" ]] || {
    echo "REFUSED: cannot verify recursive state for symlinked VM profile: $vm_dir" >&2
    return 1
  }
  [[ -e "$vm_dir/recursion_policy" ]] || return 0
  if [[ "${VM_DISCARD_RECURSIVE:-0}" == 1 ]]; then
    echo 'WARNING: --discard-recursive acknowledges that this teardown may delete nested VM state.' >&2
    return 0
  fi
  vm_recursion_load_profile "$vm_dir" || return 1
  if [[ "$VM_AGENT_RECURSION_STATE_POSSIBLE" == 1 ]]; then
    echo "REFUSED: VM profile may contain nested VM state (depth $VM_AGENT_RECURSION_DEPTH of $VM_AGENT_MAX_DEPTH, or from an earlier higher limit). Clean child profiles from this guest or pass --discard-recursive to explicitly delete them with the outer VM." >&2
    return 1
  fi
}
