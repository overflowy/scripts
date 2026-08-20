#!/usr/bin/env python3
"""Back up docker volumes, and optionally a sqlite db inside them, per a TOML config.

usage: backup.py <config.toml>

destination = "/srv/backups"

[[backup]]
container = "gitea"                                  # container to stop during backup
volume = "/var/lib/docker/volumes/gitea/_data"       # host path of the volume
keep = 5                                             # backups to keep per kind
sqlite = "gitea/gitea.db"                            # optional, relative to volume
stop_timeout = 60                                    # optional, seconds before docker stop kills
name = "gitea"                                       # optional, backup subfolder; must be unique
                                                     # per entry (default: container/service name)

[[backup]]
service = "auth-postgres"                            # swarm service: scaled to 0 replicas and
volume = "/var/lib/docker/volumes/auth-pg/_data"     # back instead of container stop/start;
keep = 5                                             # stop_timeout does not apply (swarm uses
                                                     # the service's stop grace period)

[[backup]]
container = "crowdsec"                               # several volumes, one stop/start cycle:
[[backup.volumes]]                                   # volume/sqlite/name move into
volume = "/var/lib/docker/volumes/crowdsec-db/_data" # [[backup.volumes]] sub-tables, with a
sqlite = "crowdsec.db"                               # unique name per volume
name = "crowdsec-db"
[[backup.volumes]]
volume = "/var/lib/docker/volumes/crowdsec-config/_data"
name = "crowdsec-config"

Layout: destination/<container>/sqlite/<name>_<date>_<time>_<crc32>.sqlite
        destination/<container>/data/data_<date>_<time>_<crc32>.tar.gz

A backup whose checksum matches the newest existing one is dropped and
consumes no keep slot. The container is restarted only if it was running.
When the sqlite db is the only file in the volume, the data tarball is
skipped since it would only duplicate the sqlite backup.
"""

import fcntl
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.parse
import zlib
from datetime import datetime, timezone
from pathlib import Path

import tomllib

TIME_FMT = "%Y%m%d_%H%M%S"
LOG_TIME_FMT = "%Y-%m-%d %H:%M:%S UTC"
DEFAULT_KEEP = 5
DEFAULT_STOP_TIMEOUT = 60


class Tee:
    """Write-through to a console stream and the log file."""

    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file

    def write(self, data: str) -> None:
        self.stream.write(data)
        self.log_file.write(data)

    def flush(self) -> None:
        self.stream.flush()
        self.log_file.flush()


def fmt_duration(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def crc_update_file(crc: int, path: Path) -> int:
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return crc


def crc_field(crc: int, data: bytes) -> int:
    """Length-prefix each variable-length field so records cannot blur together."""
    return zlib.crc32(len(data).to_bytes(8, "big") + data, crc)


def raise_walk_error(err: OSError) -> None:
    raise err


def manifest_crc(root: Path) -> int:
    """Checksum of relative paths, mode/uid/gid, symlink targets, and file contents."""
    st = root.lstat()
    crc = crc_field(0, f"{st.st_mode} {st.st_uid} {st.st_gid}".encode())
    for dirpath, dirnames, filenames in os.walk(root, onerror=raise_walk_error):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            crc = crc_field(crc, bytes(path.relative_to(root)))
            crc = crc_field(crc, f"{st.st_mode} {st.st_uid} {st.st_gid}".encode())
            if path.is_symlink():
                crc = crc_field(crc, os.fsencode(os.readlink(path)))
            elif path.is_file():
                crc = zlib.crc32(st.st_size.to_bytes(8, "big"), crc)
                crc = crc_update_file(crc, path)
    return crc


def sole_file(root: Path) -> Path | None:
    """The only regular file in the tree, or None if the tree holds anything else."""
    found = None
    for dirpath, dirnames, filenames in os.walk(root, onerror=raise_walk_error):
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                return None
        for name in filenames:
            path = Path(dirpath) / name
            if found is not None or path.is_symlink() or not path.is_file():
                return None
            found = path
    return found


def existing_backups(directory: Path, stem: str, suffix: str) -> list[tuple[str, str, Path]]:
    """Return (timestamp, crc, path) for files named <stem>_<date>_<time>_<crc><suffix>."""
    found = []
    for path in directory.iterdir():
        if not path.name.endswith(suffix) or path.is_symlink() or not path.is_file():
            continue
        parts = path.name.removesuffix(suffix).rsplit("_", 3)
        if len(parts) != 4:
            continue
        name, date, clock, crc = parts
        if name != stem or len(date) != 8 or len(clock) != 6:
            continue
        if len(crc) != 8 or not set(crc) <= set("0123456789abcdef"):
            continue
        try:
            datetime.strptime(f"{date}_{clock}", TIME_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        found.append((f"{date}_{clock}", crc, path))
    return sorted(found)


def rotate(
    directory: Path, stem: str, suffix: str, keep: int, tag: str, protect: Path | None = None
) -> None:
    for _, _, old in existing_backups(directory, stem, suffix)[:-keep]:
        if old == protect:
            continue
        old.unlink()
        print(f"{tag} rotated out {old.name}")


def store(
    tmp: Path,
    directory: Path,
    stem: str,
    suffix: str,
    crc: int,
    keep: int,
    tag: str,
    verify: bool = False,
) -> None:
    """Rename tmp into place unless the newest backup has the same crc, then rotate."""
    backups = existing_backups(directory, stem, suffix)
    crc_hex = f"{crc:08x}"
    unchanged = bool(backups) and backups[-1][1] == crc_hex
    if unchanged and verify and f"{crc_update_file(0, backups[-1][2]):08x}" != crc_hex:
        backups[-1][2].unlink()
        print(f"{tag} newest backup {backups[-1][2].name} was corrupt, replaced", file=sys.stderr)
        unchanged = False
    final = None
    if unchanged:
        tmp.unlink()
        print(f"{tag} unchanged (crc {crc_hex}), skipped")
    else:
        final = (
            directory / f"{stem}_{datetime.now(timezone.utc).strftime(TIME_FMT)}_{crc_hex}{suffix}"
        )
        os.replace(tmp, final)
        print(f"{tag} wrote {final.name}")
    rotate(directory, stem, suffix, keep, tag, protect=final)


def backup_sqlite(src: Path, directory: Path, keep: int, tag: str) -> None:
    tmp = directory / f".{src.stem}.tmp"
    tmp.unlink(missing_ok=True)
    try:
        src_conn = sqlite3.connect(f"file:{urllib.parse.quote(str(src))}?mode=ro", uri=True)
        try:
            dst_conn = sqlite3.connect(tmp)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        check_conn = sqlite3.connect(tmp)
        try:
            result = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check_conn.close()
        if result != "ok":
            raise RuntimeError(f"integrity_check failed: {result}")
        store(tmp, directory, src.stem, ".sqlite", crc_update_file(0, tmp), keep, tag, verify=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def backup_volume(volume: Path, directory: Path, keep: int, tag: str) -> None:
    crc = manifest_crc(volume)
    backups = existing_backups(directory, "data", ".tar.gz")
    if backups and backups[-1][1] == f"{crc:08x}":
        print(f"{tag} unchanged (crc {crc:08x}), skipped")
        rotate(directory, "data", ".tar.gz", keep, tag)
        return
    tmp = directory / ".data.tmp"
    tmp.unlink(missing_ok=True)
    try:
        with tarfile.open(tmp, "x:gz") as tar:
            tar.add(volume, arcname=".")
        store(tmp, directory, "data", ".tar.gz", crc, keep, tag)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def output_dir(directory: Path, dest_root: Path, vol: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    resolved = directory.resolve()
    if not resolved.is_relative_to(dest_root) or resolved.is_relative_to(vol):
        raise RuntimeError(f"backup dir resolves outside destination: {directory}")
    return resolved


def docker(*argv: str) -> str:
    proc = subprocess.run(["docker", *argv], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {argv[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def wait_service_stopped(service: str, timeout: float = 300) -> None:
    """Wait until no container of the service is left running."""
    deadline = time.monotonic() + timeout
    while docker("ps", "-q", "--filter", f"label=com.docker.swarm.service.name={service}"):
        if time.monotonic() > deadline:
            raise RuntimeError(f"service {service} still has running containers after {timeout}s")
        time.sleep(2)


def process(entry: dict, destination: Path) -> bool:
    is_service = "service" in entry
    name = entry["service"] if is_service else entry["container"]
    keep = entry.get("keep", DEFAULT_KEEP)
    stop_timeout = entry.get("stop_timeout", DEFAULT_STOP_TIMEOUT)
    tag = f"[{name}]"
    if is_service and "container" in entry:
        print(f"{tag} set either container or service, not both", file=sys.stderr)
        return False
    if ("volumes" in entry) == ("volume" in entry):
        print(f"{tag} set exactly one of volume or volumes", file=sys.stderr)
        return False
    if type(keep) is not int or keep < 1:
        print(f"{tag} keep must be an integer >= 1, got {keep!r}", file=sys.stderr)
        return False
    if type(stop_timeout) is not int or stop_timeout < 0:
        print(f"{tag} stop_timeout must be an integer >= 0, got {stop_timeout!r}", file=sys.stderr)
        return False
    dest_resolved = destination.resolve()
    specs = []
    for raw in entry.get("volumes", [entry]):
        if not isinstance(raw, dict) or "volume" not in raw:
            print(f"{tag} each volumes entry needs a volume path", file=sys.stderr)
            return False
        folder = raw.get("name", name)
        if type(folder) is not str or not folder or folder != Path(folder).name:
            print(f"{tag} name must be a plain folder name, got {folder!r}", file=sys.stderr)
            return False
        if any(folder == s[0] for s in specs):
            print(f"{tag} duplicate backup folder name: {folder}", file=sys.stderr)
            return False
        volume = Path(raw["volume"])
        if not volume.is_dir():
            print(f"{tag} volume path not found: {volume}", file=sys.stderr)
            return False
        vol_resolved = volume.resolve()
        if dest_resolved.is_relative_to(vol_resolved) or vol_resolved.is_relative_to(dest_resolved):
            print(f"{tag} destination and volume must not contain each other", file=sys.stderr)
            return False
        specs.append((folder, vol_resolved, raw.get("sqlite")))
    ok = True
    if is_service:
        replicas = docker(
            "service", "inspect", "--format", "{{.Spec.Mode.Replicated.Replicas}}", name
        )
        if not replicas.isdigit():
            print(f"{tag} only replicated swarm services are supported", file=sys.stderr)
            return False
        was_running = int(replicas) > 0

        def stop() -> None:
            docker("service", "scale", "--detach=false", f"{name}=0")
            wait_service_stopped(name)

        def start() -> None:
            docker("service", "scale", "--detach=false", f"{name}={replicas}")

    else:
        status = docker("inspect", "--format", "{{.State.Status}}", name)
        if status == "paused":
            print(f"{tag} container is paused, skipping", file=sys.stderr)
            return False
        was_running = status in ("running", "restarting")

        def stop() -> None:
            docker("stop", "-t", str(stop_timeout), name)

        def start() -> None:
            docker("start", name)

    try:
        stop()
        if was_running:
            print(f"{tag} stopped")
        for folder, vol_resolved, sqlite_rel in specs:
            vtag = f"[{folder}]"
            sqlite_src = None
            if sqlite_rel is not None:
                try:
                    src = (vol_resolved / sqlite_rel).resolve()
                    if not src.is_relative_to(vol_resolved):
                        raise RuntimeError(f"sqlite path escapes the volume: {sqlite_rel}")
                    sqlite_dir = output_dir(
                        destination / folder / "sqlite", dest_resolved, vol_resolved
                    )
                    backup_sqlite(src, sqlite_dir, keep, vtag)
                    sqlite_src = src
                except Exception as e:  # noqa: BLE001
                    print(f"{vtag} sqlite backup failed: {e}", file=sys.stderr)
                    ok = False
            try:
                if sqlite_src is not None and sole_file(vol_resolved) == sqlite_src:
                    print(f"{vtag} volume holds only the sqlite db, data backup skipped")
                else:
                    data_dir = output_dir(
                        destination / folder / "data", dest_resolved, vol_resolved
                    )
                    backup_volume(vol_resolved, data_dir, keep, vtag)
            except Exception as e:  # noqa: BLE001
                print(f"{vtag} volume backup failed: {e}", file=sys.stderr)
                ok = False
    finally:
        if was_running:
            for delay in (5, 10, 20, 40, None):
                try:
                    start()
                    print(f"{tag} started")
                    break
                except Exception:
                    if delay is None:
                        raise
                    time.sleep(delay)
    return ok


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: backup.py <config.toml>", file=sys.stderr)
        return 2
    # SIGTERM only sets a flag: no exception is ever injected, so a stopped
    # container is always restarted before the script honors the signal.
    term = []
    signal.signal(signal.SIGTERM, lambda *_: term.append(True))
    with open(sys.argv[1], "rb") as config_file:
        try:
            fcntl.flock(config_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another backup run is in progress", file=sys.stderr)
            return 1
        config = tomllib.load(config_file)
        destination = Path(config["destination"])
        destination.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        with open(destination / "backup.log", "a", encoding="utf-8", buffering=1) as log_file:
            log_file.write(f"--- start {datetime.now(timezone.utc):{LOG_TIME_FMT}} ---\n")
            orig_out, orig_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = Tee(orig_out, log_file), Tee(orig_err, log_file)
            try:
                failed = 0
                for entry in config.get("backup", []):
                    if term:
                        break
                    try:
                        if not process(entry, destination):
                            failed += 1
                    except Exception as e:  # noqa: BLE001
                        name = "?"
                        if isinstance(entry, dict):
                            name = entry.get("container") or entry.get("service") or "?"
                        print(f"[{name}] failed: {e}", file=sys.stderr)
                        failed += 1
                if term:
                    print(
                        "terminated by SIGTERM after finishing the current entry", file=sys.stderr
                    )
                    return 143
                return 1 if failed else 0
            finally:
                sys.stdout, sys.stderr = orig_out, orig_err
                log_file.write(
                    f"--- stop {datetime.now(timezone.utc):{LOG_TIME_FMT}}"
                    f" - took: {fmt_duration(time.monotonic() - started)} ---\n"
                )


if __name__ == "__main__":
    sys.exit(main())
