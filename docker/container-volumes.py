#!/usr/bin/env python3
"""
List all Docker containers with a tree of their mounts (host source -> container path)
and the files inside each mount, up to a configurable depth.
"""

import argparse
import json
import os
import subprocess
import sys

MAX_ENTRIES_PER_DIR = 40


def get_container_ids():
    """Return a list of IDs for all containers, running or not."""
    result = subprocess.run(
        ["docker", "ps", "-aq"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [cid for cid in result.stdout.splitlines() if cid.strip()]


def inspect_containers(container_ids):
    """Return a list of (name, mounts) tuples for the given container IDs."""
    if not container_ids:
        return []

    result = subprocess.run(
        ["docker", "inspect"] + container_ids,
        capture_output=True,
        text=True,
        check=True,
    )

    containers = []
    for info in json.loads(result.stdout):
        name = info["Name"].lstrip("/")
        mounts = []
        for mount in info.get("Mounts", []):
            source = mount.get("Source") or mount.get("Name", "?")
            mode = "rw" if mount.get("RW", True) else "ro"
            mounts.append((source, mount["Destination"], mount["Type"], mode))
        mounts.sort(key=lambda m: m[1])
        containers.append((name, mounts))
    return containers


def tree_lines(root, depth, prefix):
    """Render the contents of root as tree lines, directories first, depth levels deep."""
    if depth < 1:
        return []
    try:
        entries = sorted(
            os.scandir(root), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name)
        )
    except OSError as e:
        return [f"{prefix}└── (unreadable: {e.strerror or e})"]
    shown = entries[:MAX_ENTRIES_PER_DIR]
    hidden = len(entries) - len(shown)
    lines = []
    for i, entry in enumerate(shown):
        last = i == len(shown) - 1 and not hidden
        connector = "└──" if last else "├──"
        is_dir = entry.is_dir(follow_symlinks=False)
        if entry.is_symlink():
            label = f"{entry.name} -> {os.readlink(entry.path)}"
        else:
            label = entry.name + ("/" if is_dir else "")
        lines.append(f"{prefix}{connector} {label}")
        if is_dir and not entry.is_symlink():
            extension = "    " if last else "│   "
            lines.extend(tree_lines(entry.path, depth - 1, prefix + extension))
    if hidden:
        lines.append(f"{prefix}└── … (+{hidden} more)")
    return lines


def main():
    parser = argparse.ArgumentParser(
        description="List containers with their mounts and the files inside each mount."
    )
    parser.add_argument(
        "-d",
        "--depth",
        type=int,
        default=2,
        help="how many directory levels to show inside each mount, 0 for mounts only"
        " (default: %(default)s)",
    )
    args = parser.parse_args()
    if args.depth < 0:
        parser.error("depth must be >= 0")

    try:
        container_ids = get_container_ids()
        containers = inspect_containers(container_ids)
    except subprocess.CalledProcessError as e:
        print(f"Error running docker command: {e}", file=sys.stderr)
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(
            "Error: 'docker' command not found. Is Docker installed and on PATH?", file=sys.stderr
        )
        sys.exit(1)

    containers.sort(key=lambda c: c[0].lower())

    for name, mounts in containers:
        print(name)
        for i, (source, destination, kind, mode) in enumerate(mounts):
            last = i == len(mounts) - 1
            connector = "└──" if last else "├──"
            print(f"{connector} {source} -> {destination} ({kind}, {mode})")
            for line in tree_lines(source, args.depth, "    " if last else "│   "):
                print(line)
        if not mounts:
            print("└── (no mounts)")
        print()


if __name__ == "__main__":
    main()
