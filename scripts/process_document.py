"""Process any archaeological PDF without report-specific metadata or page offsets."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.pipeline import run_document


def main():
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf",type=Path)
    parser.add_argument("--output",type=Path,default=Path("data/processed/library"))
    parser.add_argument("--max-pages",type=int)
    parser.add_argument("--dpi",type=int,default=180)
    parser.add_argument("--threads",type=int,default=4)
    parser.add_argument("--base-url",default="http://127.0.0.1:8000/iiif")
    parser.add_argument("--stop-after",type=int,help="Interrupt after N extracted pages to verify recovery")
    parser.add_argument("--settings",dest="settings_path",type=Path,help="External .env file; defaults to user profile configuration")
    parser.add_argument("--rebuild-index",action="store_true",help="Back up and atomically regenerate an unreviewed stale index")
    parser.add_argument("--skip-models",action="store_true",help="Run extraction and indexing only; language model stage can be resumed separately")
    args=parser.parse_args()
    result=run_document(**vars(args))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
