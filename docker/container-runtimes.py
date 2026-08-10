#!/usr/bin/env python3
"""
List running Docker containers and show whether each is using the 'runsc' (gVisor) runtime.
"""

import subprocess
import sys


def get_container_ids():
    """Return a list of IDs for all running containers."""
    result = subprocess.run(
        ["docker", "ps", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [cid for cid in result.stdout.splitlines() if cid.strip()]


def inspect_containers(container_ids):
    """Return a list of (name, runtime) tuples for the given container IDs."""
    if not container_ids:
        return []

    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.Name}} {{.HostConfig.Runtime}}"] + container_ids,
        capture_output=True,
        text=True,
        check=True,
    )

    containers = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        name = parts[0].lstrip("/")
        runtime = parts[1] if len(parts) > 1 else ""
        containers.append((name, runtime))
    return containers


def main():
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

    # Sort alphabetically by container name
    containers.sort(key=lambda c: c[0].lower())

    # Determine column width dynamically so long names don't collide with RUNTIME
    name_width = max([len("CONTAINER")] + [len(name) for name, _ in containers]) + 2

    # Header
    print(f"{'CONTAINER':<{name_width}}{'RUNTIME':<10}")
    print(f"{'-' * 9:<{name_width}}{'-' * 7:<10}")

    # Rows
    for name, runtime in containers:
        runtime_display = runtime if runtime else "(none)"
        print(f"{name:<{name_width}}{runtime_display:<10}")


if __name__ == "__main__":
    main()
