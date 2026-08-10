#!/usr/bin/env bash
set -euo pipefail

MIRROR_DIR="$HOME/.local/share/github-mirror"
mkdir -p "$MIRROR_DIR" && cd "$MIRROR_DIR" || exit

gh repo list --limit 1000 --json nameWithOwner -q '.[].nameWithOwner' | while read -r repo; do
	dir="$(basename \""$repo\"").git"
	if [ -d "${dir}" ]; then
		echo "Updating $repo"
		git -C "${dir}" remote update --prune
	else
		echo "Cloning $repo"
		git clone --mirror "https://github.com/$repo.git" "${dir}"
	fi
done
