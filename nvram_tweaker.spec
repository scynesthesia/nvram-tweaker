# PyInstaller build definition for the NVRAM Tweaker Windows executable.
#
#   python -m pip install -r requirements-build.txt
#   python scripts/fetch_fonts.py
#   python -m PyInstaller --clean --noconfirm nvram_tweaker.spec
#
# Output: dist/NVRAM_Tweaker.exe. The same steps run in .github/workflows/build.yml.
from pathlib import Path

root = Path(SPECPATH)
fonts = root / "fonts"
required_fonts = ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSansMono.ttf")
missing = [name for name in required_fonts if not (fonts / name).is_file()]
if missing:
    raise SystemExit(f"Missing fonts {missing}; run: python scripts/fetch_fonts.py")

a = Analysis(
    [str(root / "nvram_gui.py")],
    pathex=[str(root)],
    datas=[(str(fonts), "fonts")],
    hiddenimports=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NVRAM_Tweaker",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
