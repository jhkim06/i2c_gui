#!/usr/bin/env python3
"""Copy queued local plots to the CERNBox plot directory after measurements."""

import argparse
from collections import Counter
import fcntl
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile


def required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ValueError(
            f"Missing {name}. Run: source ~/ETL/i2c_gui/helpers/setup_i2c_measurement_env.sh"
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {value}")
    return path


def queued_paths(pending_file: Path, lock_file: Path) -> list[str]:
    with lock_file.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not pending_file.exists():
            return []
        return pending_file.read_text(encoding="utf-8").splitlines()


def remove_copied(pending_file: Path, lock_file: Path, copied: Counter[str]) -> None:
    with lock_file.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = pending_file.read_text(encoding="utf-8").splitlines()
        remaining = []
        for entry in current:
            if copied[entry]:
                copied[entry] -= 1
            else:
                remaining.append(entry)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=pending_file.parent,
            prefix=".pending_cernbox_plots.", delete=False,
        ) as temporary:
            temporary.write("".join(f"{entry}\n" for entry in remaining))
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, pending_file)
        finally:
            temporary_path.unlink(missing_ok=True)


def copy_plot(entry: str, local_root: Path, cernbox_root: Path) -> None:
    relative = PurePosixPath(entry)
    if not entry or relative.is_absolute() or ".." in relative.parts or "\\" in entry:
        raise ValueError("invalid relative plot path")

    source = local_root.joinpath(*relative.parts)
    if not source.resolve().is_relative_to(local_root.resolve()) or not source.is_file():
        raise FileNotFoundError(f"local plot missing or outside root: {source}")

    destination = cernbox_root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(cernbox_root.resolve()):
        raise ValueError("destination directory is outside CERNBox root")

    original_size = source.stat().st_size
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.name}.", delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copy2(source, temporary_path)
        if source.stat().st_size != original_size or temporary_path.stat().st_size != original_size:
            raise OSError("plot changed during copy")
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="directory containing the pending text file")
    parser.add_argument("--dry-run", action="store_true", help="list pending plots without copying")
    args = parser.parse_args()

    try:
        local_root = required_path("ETROC_LOCAL_FIGURE_ROOT")
        configured_output_dir = required_path("ETROC_SQLITE_OUTPUT_DIR")
        output_dir = args.output_dir or configured_output_dir
        cernbox_root = required_path("ETROC_FIGURE_ROOT")
        if not output_dir.is_absolute():
            raise ValueError("output directory must be an absolute path")
        if local_root.resolve() == cernbox_root.resolve():
            raise ValueError("local and CERNBox plot roots must differ")
        if not cernbox_root.is_dir() and not args.dry_run:
            raise ValueError(f"CERNBox plot root is not available: {cernbox_root}")
        pending_file = output_dir / "pending_cernbox_plots.txt"
        if not output_dir.is_dir():
            raise ValueError(f"pending-file directory does not exist: {output_dir}")
        lock_file = output_dir / "pending_cernbox_plots.txt.lock"
        entries = queued_paths(pending_file, lock_file)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    if not entries:
        print("No pending plots.")
        return 0

    copied = Counter()
    failed = 0
    for entry in entries:
        if args.dry_run:
            print(f"PENDING {entry}")
            continue
        try:
            copy_plot(entry, local_root, cernbox_root)
        except (OSError, ValueError) as error:
            print(f"FAILED {entry}: {error}", file=sys.stderr)
            failed += 1
        else:
            copied[entry] += 1
            print(f"COPIED {entry}")

    copied_count = sum(copied.values())
    if copied:
        try:
            remove_copied(pending_file, lock_file, copied)
        except OSError as error:
            print(f"ERROR: could not update pending file: {error}", file=sys.stderr)
            return 2
    print(f"Copied {copied_count}; pending {failed}." if not args.dry_run else f"Pending {len(entries)}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
