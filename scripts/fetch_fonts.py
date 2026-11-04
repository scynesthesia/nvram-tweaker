"""Download the DejaVu fonts the GUI bundles, verify them, and place them in ./fonts.

The archive is pinned by SHA-256 so a build always embeds the same bytes. DejaVu
fonts are freely redistributable; their LICENSE is copied next to the fonts.

Usage: python scripts/fetch_fonts.py
"""
from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ARCHIVE_URL = (
    "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
)
ARCHIVE_SHA256 = "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a"
MEMBERS = {
    "dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf": "DejaVuSans.ttf",
    "dejavu-fonts-ttf-2.37/ttf/DejaVuSans-Bold.ttf": "DejaVuSans-Bold.ttf",
    "dejavu-fonts-ttf-2.37/ttf/DejaVuSansMono.ttf": "DejaVuSansMono.ttf",
    "dejavu-fonts-ttf-2.37/LICENSE": "DejaVu-LICENSE.txt",
}
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"


def main() -> int:
    if all((FONTS_DIR / name).is_file() for name in MEMBERS.values()):
        print(f"Fonts already present in {FONTS_DIR}")
        return 0

    print(f"Downloading {ARCHIVE_URL}")
    with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response:
        payload = response.read()

    digest = hashlib.sha256(payload).hexdigest()
    if digest != ARCHIVE_SHA256:
        print(f"error: SHA-256 mismatch: expected {ARCHIVE_SHA256}, got {digest}", file=sys.stderr)
        return 1

    FONTS_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member, target in MEMBERS.items():
            (FONTS_DIR / target).write_bytes(archive.read(member))
            print(f"  wrote fonts/{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
