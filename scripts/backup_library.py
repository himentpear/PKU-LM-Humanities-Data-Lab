"""Make a transactionally consistent SQLite backup while the library is in use."""
import argparse
import sqlite3
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument("--source",type=Path,default=Path("data/processed/library/library.db"))
parser.add_argument("destination",type=Path)
args=parser.parse_args()
if args.destination.exists():raise SystemExit("Destination exists; choose a new backup filename")
args.destination.parent.mkdir(parents=True,exist_ok=True)
source=sqlite3.connect(args.source.resolve().as_uri()+"?mode=ro",uri=True)
destination=sqlite3.connect(args.destination)
try:
    source.backup(destination)
    if destination.execute("pragma integrity_check").fetchone()[0]!="ok":
        raise RuntimeError("Backup integrity check failed")
finally:
    destination.close();source.close()
print("Verified backup: "+str(args.destination.resolve()))
