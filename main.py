"""Command-line entry point for image ingestion and file-system parsing."""


from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from carver import carve
from fs_parser import ParsedIndex, parse_image
from image_file_checker import inspect_png, write_png_report
from image_loader import LoadedImage, load_image
from index_writer import write_index

LOGGER = logging.getLogger(__name__)


def _build_summary(image: LoadedImage, parsed: ParsedIndex) -> dict[str, object]:
    rows = parsed.rows
    files = [row for row in rows if row["type"] == "file"]
    return {
        "image_path": str(image.path),
        "image_sha256": image.sha256,
        "partitions_found": parsed.partitions_found,
        "fs_types": parsed.fs_types,
        "total_files": len(files),
        "allocated_files": sum(row["status"] == "allocated" for row in files),
        "deleted_files": sum(row["status"] == "deleted" for row in files),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline and return a process exit code."""
    parser = argparse.ArgumentParser(
        description="Check a PNG file or index a raw/E01 disk image."
    )
    parser.add_argument("--image", required=True, type=Path, help="Input image path")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--carve", action="store_true", help="Run file carving on the image")

    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    loaded_image: LoadedImage | None = None
    try:
        if arguments.image.suffix.casefold() == ".png":
            report = inspect_png(arguments.image)
            report_path = write_png_report(arguments.out_dir, report)
            if report["valid"]:
                LOGGER.info(
                    "PNG is valid (%s x %s); report written to %s",
                    report["width"],
                    report["height"],
                    report_path,
                )
                return 0
            LOGGER.error("PNG validation failed: %s", report["error"])
            return 1

        loaded_image = load_image(arguments.image)
        parsed = parse_image(loaded_image)
        write_index(arguments.out_dir, parsed.rows, _build_summary(loaded_image, parsed))
        LOGGER.info("Wrote %s records to %s", len(parsed.rows), arguments.out_dir)
        if arguments.carve:
            LOGGER.info("Starting file carving...")
            recovered_dir = arguments.out_dir / "recovered"
            carved = carve(str(arguments.image), str(recovered_dir))
            LOGGER.info(
                "Carved %s files (%s valid); results in %s",
                len(carved), sum(1 for r in carved if r["valid"]), recovered_dir,
            )
             
    except Exception as exc:
        LOGGER.error("Image indexing failed: %s", exc)
        return 1
    finally:
        if loaded_image is not None:
            loaded_image.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
