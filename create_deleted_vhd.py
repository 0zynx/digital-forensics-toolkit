"""Populate an existing fixed-size VHD, delete test files, and acquire it raw.

Run from an elevated Windows terminal. The VHD is mounted with Windows'
built-in Storage cmdlets, so Hyper-V is not required. The VHD must be blank.

Example:
    py create_deleted_vhd.py --source-dir .\vhd_source --run-toolkit
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


POWERSHELL = "powershell.exe"


def run_powershell(script: str) -> str:
    """Run PowerShell and raise a useful error when it fails."""
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"PowerShell command failed: {details}")
    return result.stdout.strip()


def ps_quote(value: Path) -> str:
    """Return a single-quoted PowerShell string literal."""
    return "'" + str(value.resolve()).replace("'", "''") + "'"


def convert_fixed_vhd_to_raw(vhd_path: Path, raw_path: Path) -> None:
    """Copy a fixed VHD's disk payload without its 512-byte VHD footer."""
    file_size = vhd_path.stat().st_size
    if file_size < 512:
        raise ValueError(f"{vhd_path} is too small to contain a VHD footer")

    with vhd_path.open("rb") as vhd_file:
        vhd_file.seek(-512, 2)
        footer = vhd_file.read(512)

    if footer[:8] != b"conectix":
        raise ValueError(f"{vhd_path} does not have a valid VHD footer")
    if int.from_bytes(footer[60:64], "big") != 2:
        raise ValueError("Only fixed-size VHD files can be converted to raw images")

    disk_size = int.from_bytes(footer[48:56], "big")
    if disk_size + 512 != file_size:
        raise ValueError("VHD footer size does not match the fixed-size VHD file")

    with vhd_path.open("rb") as vhd_file, raw_path.open("wb") as raw_file:
        remaining = disk_size
        while remaining:
            chunk = vhd_file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise OSError("Unexpected end of VHD while copying its raw payload")
            raw_file.write(chunk)
            remaining -= len(chunk)


def create_deleted_vhd(
    source_dir: Path,
    vhd_path: Path | None,
    raw_path: Path,
    size_mb: int,
    overwrite: bool,
) -> None:
    if not source_dir.is_dir():
        raise ValueError(f"Source directory does not exist: {source_dir}")
    if vhd_path is None:
        vhd_files = [
            path
            for path in source_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".vhd"
        ]
        if len(vhd_files) != 1:
            raise ValueError(
                f"Expected exactly one .vhd in {source_dir}; found {len(vhd_files)}. "
                "Pass its path with --vhd."
            )
        vhd_path = vhd_files[0]
    if not vhd_path.is_file() or vhd_path.suffix.lower() != ".vhd":
        raise ValueError(f"VHD file does not exist or is not a .vhd file: {vhd_path}")

    vhd_resolved = vhd_path.resolve()
    files = [
        path
        for path in source_dir.iterdir()
        if path.is_file() and path.resolve() != vhd_resolved
    ]
    if len(files) != 4:
        raise ValueError(
            f"{source_dir} must contain exactly four payload files besides the VHD; "
            f"found {len(files)}"
        )
    if size_mb < 32:
        raise ValueError("size_mb must be at least 32 for a FAT32 test image")
    if raw_path.resolve() == vhd_resolved or any(
        raw_path.resolve() == path.resolve() for path in files
    ):
        raise ValueError("The raw output path must not overwrite the VHD or a source file")
    if not overwrite and raw_path.exists():
        raise FileExistsError("Raw image already exists. Use --overwrite to replace it.")

    raw_path.parent.mkdir(parents=True, exist_ok=True)

    source_literal = ps_quote(source_dir)
    vhd_literal = ps_quote(vhd_path)
    size_bytes = size_mb * 1024 * 1024

    # Operate only on the selected blank VHD; never initialize a host disk.
    script = f"""
$ErrorActionPreference = 'Stop'
$vhdPath = {vhd_literal}
$sourceDir = {source_literal}
$mountedByScript = $false
$operationCompleted = $false
try {{
    $image = Get-DiskImage -ImagePath $vhdPath
    if (-not $image.Attached) {{
        $image = Mount-DiskImage -ImagePath $vhdPath -PassThru
        $mountedByScript = $true
    }}
    $disk = $image | Get-Disk
    if ($disk.PartitionStyle -ne 'RAW') {{
        throw "Refusing to format a non-blank VHD (partition style: $($disk.PartitionStyle))."
    }}
    if ($disk.Size -lt {size_bytes}) {{
        throw "VHD is too small; it must be at least {size_bytes} bytes."
    }}
    if ((Get-Item -LiteralPath $vhdPath).Length -ne ($disk.Size + 512)) {{
        throw 'Only fixed-size VHD files are supported for raw image acquisition.'
    }}
    $sourceFiles = @(Get-ChildItem -LiteralPath $sourceDir -File | Where-Object {{
        [IO.Path]::GetFullPath($_.FullName) -ne [IO.Path]::GetFullPath($vhdPath)
    }})
    if ($sourceFiles.Count -ne 4) {{
        throw "Expected four payload files besides the VHD; found $($sourceFiles.Count)."
    }}

    Initialize-Disk -Number $disk.Number -PartitionStyle MBR -PassThru | Out-Null
    $partition = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
    Format-Volume -Partition $partition -FileSystem FAT32 -NewFileSystemLabel 'RECOVERY_TEST' -Confirm:$false | Out-Null
    $driveRoot = "$($partition.DriveLetter):\\"
    foreach ($file in $sourceFiles) {{
        Copy-Item -LiteralPath $file.FullName -Destination $driveRoot -Force
    }}
    Write-Host "Copied four files to $driveRoot"
    foreach ($file in $sourceFiles) {{
        Remove-Item -LiteralPath (Join-Path $driveRoot $file.Name) -Force
    }}
    Write-Host "Deleted four files from $driveRoot"
    $operationCompleted = $true
}} finally {{
    if ($mountedByScript -or $operationCompleted) {{
        $image = Get-DiskImage -ImagePath $vhdPath
        if ($image.Attached) {{
            Dismount-DiskImage -ImagePath $vhdPath
        }}
    }}
}}
"""
    print(run_powershell(script))
    convert_fixed_vhd_to_raw(vhd_path, raw_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Directory containing exactly four files to copy and delete",
    )
    parser.add_argument(
        "--vhd",
        type=Path,
        help="Existing blank fixed-size VHD; defaults to the only .vhd in --source-dir",
    )
    parser.add_argument("--raw", type=Path, default=Path("usb_test.dd"))
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--run-toolkit",
        action="store_true",
        help="Run main.py against the acquired raw image after creation",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("usb_test_output"))
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        raise SystemExit(
            "This script uses Windows Storage cmdlets and must run on Windows. "
            "On macOS, use the existing demo workflow."
        )

    try:
        create_deleted_vhd(
            args.source_dir,
            args.vhd,
            args.raw,
            args.size_mb,
            args.overwrite,
        )
        print(f"Detached VHD converted to raw image {args.raw.resolve()}")
        if args.run_toolkit:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    sys.executable,
                    "main.py",
                    "--image",
                    str(args.raw),
                    "--out-dir",
                    str(args.out_dir),
                    "--carve",
                ],
                check=True,
            )
    except (
        FileExistsError,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
