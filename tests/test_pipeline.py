from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from carver import carve
from create_deleted_vhd import convert_fixed_vhd_to_raw
from fs_parser import parse_image
from image_file_checker import inspect_png
from image_loader import load_image
from index_writer import INDEX_COLUMNS, write_index
from main import _build_summary, main

DATA_DIR = Path(__file__).parent / "data"
FIXTURE = DATA_DIR / "fat12.img"


def test_fixed_vhd_conversion_strips_footer(tmp_path: Path) -> None:
    disk_payload = b"raw disk payload"
    footer = bytearray(512)
    footer[:8] = b"conectix"
    footer[48:56] = len(disk_payload).to_bytes(8, "big")
    footer[60:64] = (2).to_bytes(4, "big")
    vhd_path = tmp_path / "blank.vhd"
    raw_path = tmp_path / "image.dd"
    vhd_path.write_bytes(disk_payload + footer)

    convert_fixed_vhd_to_raw(vhd_path, raw_path)

    assert raw_path.read_bytes() == disk_payload


def test_fixed_vhd_conversion_rejects_dynamic_vhd(tmp_path: Path) -> None:
    footer = bytearray(512)
    footer[:8] = b"conectix"
    footer[48:56] = (512).to_bytes(8, "big")
    footer[60:64] = (3).to_bytes(4, "big")
    vhd_path = tmp_path / "dynamic.vhd"
    vhd_path.write_bytes(b"x" * 512 + footer)

    with pytest.raises(ValueError, match="Only fixed-size VHD"):
        convert_fixed_vhd_to_raw(vhd_path, tmp_path / "image.dd")


def test_cli_validates_standalone_png(tmp_path: Path) -> None:
    png_path = tmp_path / "sample.PNG"
    Image.new("RGB", (3, 2), color="red").save(png_path)

    result = main(["--image", str(png_path), "--out-dir", str(tmp_path / "out")])

    assert result == 0
    report = json.loads(
        (tmp_path / "out" / "image_check.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is True
    assert report["format"] == "PNG"
    assert (report["width"], report["height"]) == (3, 2)


def test_cli_reports_corrupt_png(tmp_path: Path) -> None:
    png_path = tmp_path / "broken.png"
    png_path.write_bytes(b"not a png")

    result = main(["--image", str(png_path), "--out-dir", str(tmp_path / "out")])

    assert result == 1
    report = json.loads(
        (tmp_path / "out" / "image_check.json").read_text(encoding="utf-8")
    )
    assert report["valid"] is False
    assert report["error"]


def test_png_checker_rejects_non_png_content(tmp_path: Path) -> None:
    renamed_jpeg = tmp_path / "renamed.png"
    Image.new("RGB", (2, 2), color="blue").save(renamed_jpeg, format="JPEG")

    report = inspect_png(renamed_jpeg)

    assert report["valid"] is False
    assert report["format"] == "JPEG"


def test_carver_records_recovery_confidence(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    image = tmp_path / "evidence.dd"
    Image.new("RGB", (3, 2), color="red").save(source, format="PNG")
    image.write_bytes(b"\0" * 512 + source.read_bytes())

    results = carve(str(image), str(tmp_path / "recovered"))

    assert len(results) == 1
    assert results[0]["valid"] is True
    assert results[0]["confidence_score"] == 100
    assert json.loads(
        (tmp_path / "recovered" / "carve_log.json").read_text(encoding="utf-8")
    )[0]["confidence_score"] == 100


def test_e01_without_pyewf_has_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "evidence.E01"
    image_path.write_bytes(b"optional E01 test")
    monkeypatch.setitem(sys.modules, "pyewf", None)

    with pytest.raises(
        RuntimeError, match="E01 images require libewf and the optional pyewf"
    ):
        load_image(image_path)


def test_bare_fat_image_is_indexed_and_hashed() -> None:
    with FIXTURE.open("rb") as image_file:
        expected_hash = hashlib.sha256(image_file.read()).hexdigest()

    loaded = load_image(FIXTURE)
    try:
        parsed = parse_image(loaded)
        assert loaded.sha256 == expected_hash
        assert parsed.partitions_found[0]["byte_offset"] == 0
        assert len(parsed.fs_types) == 1
        assert "FAT" in str(parsed.fs_types[0]["type"]).upper()
        target = next(row for row in parsed.rows if row["name"] == "HELLO.TXT")
        assert target["path"] == "/HELLO.TXT"
        assert target["type"] == "file"
        assert target["size"] == len(b"forensic fixture\n")
        assert target["status"] == "allocated"
        assert target["data_runs"] == [{"start_offset": 16896, "length": 512}]
        assert any(row["type"] == "file" and row["status"] == "deleted" for row in parsed.rows)
    finally:
        loaded.close()


def test_partitioned_fat_image_is_indexed(tmp_path: Path) -> None:
    partition_payload = FIXTURE.read_bytes()
    partition_start_sector = 128
    partition_length = (len(partition_payload) + 511) // 512
    mbr = bytearray(512)
    partition_entry = 446
    mbr[partition_entry + 4] = 0x01
    mbr[partition_entry + 8 : partition_entry + 12] = partition_start_sector.to_bytes(
        4, "little"
    )
    mbr[partition_entry + 12 : partition_entry + 16] = partition_length.to_bytes(
        4, "little"
    )
    mbr[510:512] = b"\x55\xaa"
    image_path = tmp_path / "partitioned.dd"
    image_path.write_bytes(
        mbr + b"\0" * (partition_start_sector * 512 - len(mbr)) + partition_payload
    )

    loaded = load_image(image_path)
    try:
        parsed = parse_image(loaded)
        partition_offset = partition_start_sector * 512
        assert any(
            partition["byte_offset"] == partition_offset
            for partition in parsed.partitions_found
        )
        assert any("FAT" in str(fs_type["type"]).upper() for fs_type in parsed.fs_types)
        assert any(row["name"] == "HELLO.TXT" for row in parsed.rows)
    finally:
        loaded.close()


def test_csv_and_json_have_the_same_index_rows(tmp_path: Path) -> None:
    loaded = load_image(FIXTURE)
    try:
        parsed = parse_image(loaded)
        summary = _build_summary(loaded, parsed)
        write_index(tmp_path, parsed.rows, summary)
    finally:
        loaded.close()

    json_rows = json.loads((tmp_path / "file_index.json").read_text(encoding="utf-8"))
    with (tmp_path / "file_index.csv").open(
        "r", encoding="utf-8", newline=""
    ) as csv_file:
        reader = csv.DictReader(csv_file)
        assert tuple(reader.fieldnames or ()) == INDEX_COLUMNS
        csv_rows = list(reader)

    assert len(csv_rows) == len(json_rows)
    for csv_row, json_row in zip(csv_rows, json_rows, strict=True):
        assert csv_row.keys() == json_row.keys()
        for key in INDEX_COLUMNS:
            if key == "data_runs":
                assert json.loads(csv_row[key]) == json_row[key]
            elif key in {"partition_index", "inode", "size"}:
                assert int(csv_row[key]) == json_row[key]
            elif key in {"created_utc", "modified_utc", "accessed_utc", "changed_utc"}:
                assert csv_row[key] == (json_row[key] or "")
            else:
                assert csv_row[key] == json_row[key]

    summary_data = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary_data["image_sha256"] == loaded.sha256
    assert summary_data["total_files"] >= 1
