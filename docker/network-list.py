#!/usr/bin/env python3

import json
import subprocess
import sys


def run(cmd):
    result = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)

    return result.stdout


def container_count(containers):
    if not containers:
        return 0

    return len(containers)


def main():
    ids = run(["docker", "network", "ls", "-q"]).split()

    if not ids:
        print("No Docker networks found.")
        return

    raw = run(["docker", "network", "inspect", *ids])
    networks = json.loads(raw)

    rows = []

    for item in networks:
        name = item.get("Name", "")
        driver = item.get("Driver", "")
        internal = item.get("Internal", False)
        attachable = item.get("Attachable", False)
        scope = item.get("Scope", "")
        containers = container_count(item.get("Containers") or {})

        configs = item.get("IPAM", {}).get("Config") or []

        if not configs:
            rows.append(
                {
                    "name": name,
                    "driver": driver,
                    "scope": scope,
                    "internal": internal,
                    "attachable": attachable,
                    "subnet": "",
                    "gateway": "",
                    "containers": containers,
                }
            )
            continue

        for cfg in configs:
            rows.append(
                {
                    "name": name,
                    "driver": driver,
                    "scope": scope,
                    "internal": internal,
                    "attachable": attachable,
                    "subnet": cfg.get("Subnet", ""),
                    "gateway": cfg.get("Gateway", ""),
                    "containers": containers,
                }
            )

    name_width = max(len("network"), *(len(row["name"]) for row in rows))
    subnet_width = max(len("subnet"), *(len(row["subnet"]) for row in rows))
    gateway_width = max(len("gateway"), *(len(row["gateway"]) for row in rows))
    driver_width = max(len("driver"), *(len(row["driver"]) for row in rows))
    scope_width = max(len("scope"), *(len(row["scope"]) for row in rows))
    internal_width = len("internal")
    attachable_width = len("attachable")
    containers_width = max(
        len("containers"),
        *(len(str(row["containers"])) for row in rows),
    )

    header = (
        f"{'network':<{name_width}}  "
        f"{'subnet':<{subnet_width}}  "
        f"{'gateway':<{gateway_width}}  "
        f"{'driver':<{driver_width}}  "
        f"{'scope':<{scope_width}}  "
        f"{'internal':<{internal_width}}  "
        f"{'attachable':<{attachable_width}}  "
        f"{'containers':>{containers_width}}"
    )

    print(header)
    print("-" * len(header))

    for row in sorted(rows, key=lambda r: r["name"]):
        print(
            f"{row['name']:<{name_width}}  "
            f"{row['subnet']:<{subnet_width}}  "
            f"{row['gateway']:<{gateway_width}}  "
            f"{row['driver']:<{driver_width}}  "
            f"{row['scope']:<{scope_width}}  "
            f"{str(row['internal']).lower():<{internal_width}}  "
            f"{str(row['attachable']).lower():<{attachable_width}}  "
            f"{row['containers']:>{containers_width}}"
        )


if __name__ == "__main__":
    main()
