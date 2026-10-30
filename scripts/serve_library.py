import argparse
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.server import make_server

parser=argparse.ArgumentParser(description="Serve the local archaeological document library")
parser.add_argument("--library",type=Path,default=Path("data/processed/library"))
parser.add_argument("--port",type=int,default=8000)
args=parser.parse_args()
server=make_server(args.library,port=args.port)
print(f"Library ready at http://127.0.0.1:{args.port}",flush=True)
try:server.serve_forever()
finally:
    server.server_close();server.engine.dispose()
