import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from src.db.session import init_db,get_session
from src.db.models import Manifest
from src.model_enrichment import enrich_with_models
from src.pipeline import _export

parser=argparse.ArgumentParser(description="Run local-first model enrichment using external settings")
parser.add_argument("--library",type=Path,default=Path("data/processed/library"))
parser.add_argument("--settings",type=Path)
parser.add_argument("--manifest")
args=parser.parse_args()
engine=init_db("sqlite:///"+(args.library.resolve()/"library.db").as_posix())
try:
    with get_session(engine) as session:
        documents=[(m.id,m.work.slug) for m in session.scalars(select(Manifest)) if not args.manifest or m.id==args.manifest]
    for mid,slug in documents:
        result=enrich_with_models(engine,mid,args.library/slug,"http://127.0.0.1:8000/iiif",args.settings)
        print(json.dumps({"manifest":mid,**result},ensure_ascii=False),flush=True)
        with get_session(engine) as session:_export(session,mid,args.library/slug,"http://127.0.0.1:8000/iiif")
finally:engine.dispose()
