#!/usr/bin/env python3
"""
network-pick-subnet.py — find a free private subnet + static IPs for an
isolated (internal) Docker network, and print a ready-to-paste Compose template.

Zero required arguments. Run it on the Docker host:

    ./network-pick-subnet.py                  # 1 service IP (e.g. a database)
    ./network-pick-subnet.py --services 2     # 2 service IPs
    ./network-pick-subnet.py --explain        # also show what's already in use

How it works, in plain words:
  * Docker networks and your host's routes each "own" a range of IP
    addresses (a subnet). Two ranges must never overlap.
  * This script collects every range already in use (Docker networks +
    host routes), then walks through several large private "pools" of
    addresses until it finds a small /24 range (256 addresses) that
    collides with nothing.
  * You don't need to know or choose the pool: if one pool is fully
    blocked (a VPN, for example, can reserve a huge range), the script
    automatically moves on to the next pool.
"""

import argparse
import ipaddress
import json
import subprocess
import sys

# Pools to try, in order. Spread across different private ranges so that a
# single VPN/LAN/Docker range can't block all of them at once.
#
# Ordering rationale (learned in production):
#  * Docker auto-allocates unpinned bridge networks from 172.17-172.31 as
#    whole /16s -- a host with enough stacks WILL swallow 172.30/172.31
#    (observed: two compose 'default' networks blocked both). So 10.x first.
#  * Swarm's overlay allocator crawls 10.0.0.0/8 from the bottom
#    (10.0.0.x, 10.0.1.x ...); starting at 10.66 keeps us far above it.
#  * Tailscale's CGNAT range is 100.64.0.0/10 -- outside all pools here.
#  * 172.x pools kept only as fallback for hosts where 10.x is taken by
#    a corporate LAN/VPN; 192.168.240/20 last (tail of Docker's 192.168
#    auto range, and plausible on some home LANs).
DEFAULT_POOLS = [
    "10.66.0.0/16",
    "10.99.0.0/16",
    "10.130.0.0/16",
    "172.30.0.0/16",
    "172.31.0.0/16",
    "192.168.240.0/20",
]

FIRST_CHILD_INDEX = 50  # inside a pool, start at x.x.50.0/24 (matches old script)


def run(cmd):
    try:
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return ""  # tool not installed (e.g. running off-host); treat as no data
    return result.stdout if result.returncode == 0 else ""


def parse_v4_network(value):
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
    return net if net.version == 4 else None


def docker_networks():
    ids = run(["docker", "network", "ls", "-q"]).split()
    if not ids:
        return []
    raw = run(["docker", "network", "inspect", *ids])
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    found = []
    for item in data:
        name = item.get("Name", "unknown")
        for cfg in item.get("IPAM", {}).get("Config") or []:
            net = parse_v4_network(cfg.get("Subnet") or "")
            if net:
                found.append((f"docker:{name}", net))
    return found


def host_routes():
    raw = run(["ip", "-j", "-4", "route"])
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    found = []
    for item in data:
        dst = item.get("dst")
        if not dst or dst == "default":
            continue
        net = parse_v4_network(dst)
        if net:
            found.append((f"route:{dst}", net))
    return found


def first_conflict(candidate, used):
    for name, net in used:
        if candidate.overlaps(net):
            return name, net
    return None


def ip_at(subnet, offset):
    ip = ipaddress.ip_address(int(subnet.network_address) + offset)
    if ip not in subnet or ip in (subnet.network_address, subnet.broadcast_address):
        raise ValueError(f"offset {offset} unusable in {subnet}")
    return ip


def find_free_subnet(pools, used, prefix, services, first_offset):
    """Return (subnet, notes). notes maps pool -> why it was skipped."""
    notes = {}
    for pool_str in pools:
        pool = parse_v4_network(pool_str)
        if not pool or prefix < pool.prefixlen:
            notes[pool_str] = "invalid pool / prefix too broad"
            continue

        # If something huge overlaps the whole pool, say so and move on fast.
        whole = first_conflict(pool, used)
        blockers = set()

        children = list(pool.subnets(new_prefix=prefix))
        start = FIRST_CHILD_INDEX if FIRST_CHILD_INDEX < len(children) else 0
        ordered = children[start:] + children[:start]

        for child in ordered:
            conflict = first_conflict(child, used)
            if conflict:
                blockers.add(conflict[0])
                continue
            try:
                for off in range(first_offset, first_offset + services):
                    ip_at(child, off)
            except ValueError:
                continue
            return child, notes

        if whole and len(blockers) == 1:
            notes[pool_str] = f"entirely covered by {whole[0]} ({whole[1]})"
        else:
            notes[pool_str] = "all /%d children blocked by: %s" % (
                prefix,
                ", ".join(sorted(blockers)) or "unknown",
            )
    return None, notes


def main():
    parser = argparse.ArgumentParser(
        description="Suggest a non-overlapping Compose subnet + static IPs "
        "and print the isolated-network (gVisor-friendly) template.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--services",
        type=int,
        default=1,
        help="How many static service IPs to generate (1-10). Default: 1",
    )
    parser.add_argument(
        "--network-name",
        default="app-db-net",
        help="Compose network name used in the printed template. Default: app-db-net",
    )
    parser.add_argument(
        "--prefix", type=int, default=24, help="Subnet size. Default: /24 (256 addresses, plenty)"
    )
    parser.add_argument(
        "--first-service-offset",
        type=int,
        default=10,
        help="Host number of the first service IP. Default: .10",
    )
    parser.add_argument(
        "--pools",
        nargs="*",
        default=DEFAULT_POOLS,
        help="Override the candidate pools (advanced; normally leave alone)",
    )
    parser.add_argument(
        "--explain", action="store_true", help="List every subnet already in use before choosing"
    )
    args = parser.parse_args()

    if not 1 <= args.services <= 10:
        sys.exit("--services must be between 1 and 10")

    used = docker_networks() + host_routes()
    if not used:
        print(
            "warning: found no Docker networks and no host routes -- "
            "am I running on the Docker host?",
            file=sys.stderr,
        )

    if args.explain:
        print("Already in use on this host:")
        for name, net in sorted(used, key=lambda p: int(p[1].network_address)):
            print(f"  {str(net):<20} {name}")
        print()

    subnet, notes = find_free_subnet(
        args.pools, used, args.prefix, args.services, args.first_service_offset
    )

    if subnet is None:
        print("No free subnet found. Why each pool was rejected:", file=sys.stderr)
        for pool, why in notes.items():
            print(f"  {pool}: {why}", file=sys.stderr)
        print(
            "\nTip: run with --explain to see everything in use, or pass "
            "your own pools, e.g. --pools 10.201.0.0/16",
            file=sys.stderr,
        )
        sys.exit(1)

    if notes:
        for pool, why in notes.items():
            print(f"note: skipped pool {pool}: {why}", file=sys.stderr)

    ips = [
        (f"SERVICE_{i:02d}_IP", ip_at(subnet, args.first_service_offset + i - 1))
        for i in range(1, args.services + 1)
    ]

    print(f"# Chosen subnet: {subnet}  (gateway will be {ip_at(subnet, 1)})")
    print()
    print("# ---- .env values (rename SERVICE_01_IP to taste, e.g. POSTGRES_IP) ----")
    print(f"SUBNET={subnet}")
    for env, ip in ips:
        print(f"{env}={ip}")

    net = args.network_name
    print(f"""
# ---- Compose template (isolated internal net + sandboxed app) ----
#
# Pattern notes -- learned the hard way:
#  * gVisor (runtime: runsc) cannot use Docker's embedded DNS (127.0.0.11),
#    so: services get STATIC IPs, the app reaches them via extra_hosts
#    (/etc/hosts), and external DNS comes from a mounted resolv.conf.
#  * In Dokploy: create the resolv.conf as a File Mount (Advanced > Mounts),
#    content "nameserver 1.1.1.1" (+ optional fallback), file name resolv.conf.
#  * The db-style service sits ONLY on the internal network: no internet,
#    no ports:, unreachable from other stacks.
#  * Dokploy auto-attaches its ingress network and a default egress network
#    to the app service -- that's expected and provides Traefik + internet.
#  * After ANY network/mount change, force-recreate the runsc container
#    (restart is not enough; gVisor can't hot-attach networks).

services:
  db:                              # your protected backend service
    networks:
      {net}:
        ipv4_address: ${{SERVICE_01_IP}}
    # no ports:, no other networks -> fully isolated

  app:                             # the exposed, sandboxed service
    runtime: runsc
    networks:
      - {net}
    extra_hosts:
      - "db:${{SERVICE_01_IP}}"      # name resolution that works inside gVisor
    volumes:
      - ../files/resolv.conf:/etc/resolv.conf:ro   # Dokploy File Mount
    # connect to the db as e.g.: postgres://user:pass@db:5432/dbname

networks:
  {net}:
    internal: true                 # no gateway, no NAT, no internet
    ipam:
      config:
        - subnet: ${{SUBNET}}""")
    if args.services > 1:
        print("""
# Extra service IPs generated: attach each service like `db` above with its
# own ipv4_address, and add one extra_hosts line per name the app must reach.""")


if __name__ == "__main__":
    main()
