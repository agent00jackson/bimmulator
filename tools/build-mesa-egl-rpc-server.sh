#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/.." && pwd)"
cxx="${IDRIVE_HOST_CXX:-${CXX:-c++}}"
pkg_config="${PKG_CONFIG:-pkg-config}"
output="${1-$repo_dir/build/mesa-egl-rpc-server}"

if (( $# > 1 )) || [[ -z "$output" ]]; then
  echo "Usage: $0 [output-path]" >&2
  exit 1
fi
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo "The Mesa EGL RPC server builder requires a Linux x86_64 host." >&2
  exit 1
fi
if ! command -v "$cxx" >/dev/null 2>&1; then
  echo "Host C++ compiler is unavailable: $cxx (set CXX to a compiler executable)." >&2
  exit 1
fi
if ! command -v "$pkg_config" >/dev/null 2>&1; then
  echo "pkg-config is unavailable: $pkg_config (set PKG_CONFIG to its executable)." >&2
  exit 1
fi
if ! "$pkg_config" --exists egl glesv2; then
  echo "Mesa EGL/GLES development dependencies are unavailable (pkg-config: egl glesv2)." >&2
  exit 1
fi
cflags_text="$("$pkg_config" --cflags egl glesv2)" || {
  echo "Failed to query EGL/GLES compiler flags with pkg-config." >&2
  exit 1
}
libs_text="$("$pkg_config" --libs egl glesv2)" || {
  echo "Failed to query EGL/GLES linker flags with pkg-config." >&2
  exit 1
}
# Split pkg-config flags without pathname expansion or shell evaluation.
read -r -a cflags <<< "$cflags_text"
read -r -a libs <<< "$libs_text"

mkdir -p "$(dirname "$output")"
"$cxx" -std=c++17 -O2 -Wall -Wextra -Werror -DIDRIVE_USE_MESA \
  "${cflags[@]}" -I"$script_dir" \
  "$script_dir/angle-egl-rpc-server.cpp" \
  "${libs[@]}" -o "$output"
echo "Built host Mesa EGL RPC server: $output"
