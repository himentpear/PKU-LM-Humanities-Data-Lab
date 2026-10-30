"""Retrieve locally ranked evidence chunks and their original-page anchors."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.db.session import init_db,get_session
from src.retrieval import retrieve

parser=argparse.ArgumentParser()
parser.add_argument("question")
parser.add_argument("--manifest",required=True)
parser.add_argument("--library",type=Path,default=Path("data/processed/library"))
parser.add_argument("--limit",type=int,default=5)
args=parser.parse_args()
engine=init_db("sqlite:///"+(args.library.resolve()/"library.db").as_posix())
try:
    with get_session(engine) as session:
        print(json.dumps({"retrieval":"local_character_bigram_BM25","sources":retrieve(session,args.manifest,args.question,max(1,min(20,args.limit)))},ensure_ascii=False,indent=2))
finally:engine.dispose()
