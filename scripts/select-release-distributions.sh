#!/usr/bin/env bash
# Check that a downloaded release directory holds only one native package's wheels and sdist.
# Usage: select-release-distributions.sh <directory> <namespace>   (namespace is the file prefix, e.g. rowx_postgres)
set -euo pipefail
shopt -s nullglob

if (($# != 2)) || [[ -z "$1" || ! "$2" =~ ^[a-z_]+$ ]]; then
  echo "usage: $0 <directory> <namespace>" >&2
  exit 2
fi
directory="${1%/}"
namespace="$2"
label="${namespace//_/-}"

files=("$directory"/*)
wheels=("$directory/${namespace}"-*.whl)
sdists=("$directory/${namespace}"-*.tar.gz)
if ((${#wheels[@]} == 0 || ${#sdists[@]} == 0)); then
  echo "$label wheels or sdist are missing" >&2
  exit 1
fi
for path in "${files[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "unexpected release artifact: $path" >&2
    exit 1
  fi
  name="$(basename "$path")"
  case "$name" in
    "$namespace"-*) ;;
    *)
      echo "unexpected release artifact: $name" >&2
      exit 1
      ;;
  esac
done
