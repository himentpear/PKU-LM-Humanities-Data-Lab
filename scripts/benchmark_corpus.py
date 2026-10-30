"""Run all supplied PDFs through the same filename-independent pipeline."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.pipeline import run_document
from src.ingest.page_pipeline import atomic_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser()
    parser.add_argument("--pdf-dir",type=Path,default=Path("docs/考古报告"))
    parser.add_argument("--output",type=Path,default=Path("data/processed/library"))
    parser.add_argument("--max-pages",type=int)
    parser.add_argument("--rebuild-index",action="store_true")
    parser.add_argument("--skip-models",action="store_true")
    args=parser.parse_args()
    files=sorted(args.pdf_dir.glob("*.pdf"),key=lambda p:("观台" in p.name,p.name))
    results=[]
    for pdf in files:
        started=time.time()
        try:
            result=run_document(pdf,args.output,max_pages=args.max_pages,rebuild_index=args.rebuild_index,skip_models=args.skip_models)
            results.append({"file":pdf.name,"status":result["status"],"run_id":result["run_id"],
                            "stats":result["stats"],"wall_seconds":round(time.time()-started,2)})
        except Exception as error:
            results.append({"file":pdf.name,"status":"failed","error_type":type(error).__name__})
            print(json.dumps(results[-1],ensure_ascii=False),flush=True)
        atomic_json(args.output/"corpus_results.json",results)
    print(json.dumps(results,ensure_ascii=False,indent=2))
    return 1 if any(r["status"]!="completed" for r in results) else 0


if __name__=="__main__":
    raise SystemExit(main())
