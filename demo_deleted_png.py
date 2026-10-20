"""Safe demo: carve a PNG after its original file is deleted.

This uses only a temporary directory. The raw image acts as the forensic copy;
deleting the standalone PNG does not delete its bytes from that image.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from PIL import Image

from carver import carve

SECTOR_SIZE = 512
PNG_SECTOR = 8


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="png-recovery-demo-") as temp_dir:
        root = Path(temp_dir)
        original = root / "original.png"
        raw_image = root / "evidence.dd"
        recovered_dir = root / "recovered"

        Image.new("RGB", (32, 24), "red").save(original, format="PNG")
        original_bytes = original.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()

        # Create a raw image containing the PNG at a sector-aligned offset.
        raw_image.write_bytes(
            b"\0" * (PNG_SECTOR * SECTOR_SIZE)
            + original_bytes
            + b"\0" * SECTOR_SIZE
        )

        original.unlink()  # permanently remove the standalone source file
        assert not original.exists()

        results = carve(str(raw_image), str(recovered_dir))
        matches = [
            result
            for result in results
            if result["type"] == "png" and result["sha256"] == original_hash
        ]

        if len(matches) != 1:
            raise RuntimeError(f"Expected one matching recovered PNG, found {len(matches)}")

        match = matches[0]
        recovered = recovered_dir / match["file"]
        print(f"Original PNG exists after deletion: {original.exists()}")
        print(f"Recovered file: {recovered}")
        print(f"Recovered bytes match original: {recovered.read_bytes() == original_bytes}")
        print(f"Carve metadata: {json.dumps(match, indent=2)}")
        print(f"Demo files kept until process exit: {root}")


if __name__ == "__main__":
    main()
