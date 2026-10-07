"""Copy the built map (v2_full) into deploy/site so it ships inside the Connect bundle."""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
src, dst = HERE.parent / "v2_full", HERE / "site"
if not (src / "index.html").exists():
    raise SystemExit("v2_full/index.html not found: run make_v2_full.py first")
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst)
mb = sum(p.stat().st_size for p in dst.rglob("*") if p.is_file()) / 1e6
print(f"copied v2_full -> {dst} ({mb:.0f} MB)")
