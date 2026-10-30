"""Local document library, IIIF resources, source search and review endpoints."""
from __future__ import annotations

import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from src.db.models import Canvas, Manifest, Work, AltoLine, ReviewTask
from src.db.session import get_session, init_db
from src.iiif.generator import IiifBuilder


def make_server(library: Path, host: str="127.0.0.1", port: int=8000):
    if host not in {"127.0.0.1","localhost"}:
        raise ValueError("Review service currently supports loopback only")
    library=library.resolve()
    engine=init_db("sqlite:///"+(library/"library.db").as_posix())
    base_url=f"http://127.0.0.1:{port}/iiif"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,data,status=200,mime="application/json; charset=utf-8"):
            if not isinstance(data,bytes):
                data=json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(data)))
            self.send_header("X-Content-Type-Options","nosniff")
            self.end_headers();self.wfile.write(data)

        def do_GET(self):
            try:
                self.handle_get()
            except (ValueError,KeyError) as error:
                self.send({"error":str(error)},400)
            except LookupError:
                self.send({"error":"Not found"},404)
            except Exception:
                self.send({"error":"Internal error"},500)

        def handle_get(self):
            parsed=urlparse(self.path);path=parsed.path;query=parse_qs(parsed.query)
            if path=="/":
                return self.send((Path(__file__).parent/"web/library.html").read_bytes(),mime="text/html; charset=utf-8")
            if path=="/health":
                return self.send({"status":"ok","database":engine.dialect.name})
            with get_session(engine) as session:
                builder=IiifBuilder(session,base_url)
                if path=="/api/documents":
                    return self.send([{"id":m.id,"title":m.work.title,"pages":m.total_pages,
                        "manifest":f"{base_url}/manifest/{m.id}/manifest.json"} for m in session.scalars(select(Manifest))])
                if path=="/api/pages":
                    mid=query.get("manifest",[""])[0]
                    return self.send([{"id":c.id,"scan_index":c.scan_index,"label":c.printed_page_label,
                        "domain":c.page_domain,"revision":c.revision_no} for c in session.scalars(
                        select(Canvas).where(Canvas.manifest_id==mid).order_by(Canvas.scan_index))])
                if path=="/api/page":
                    cid=query.get("id",[""])[0]
                    canvas=session.get(Canvas,cid)
                    if canvas is None:raise LookupError()
                    result=builder.build_page_bundle(cid)
                    result["lines"]=[{"id":l.id,"text":l.content_text,"raw":l.raw_ocr,"xywh":l.xywh,
                                      "status":l.review_status} for l in session.scalars(select(AltoLine).where(AltoLine.canvas_id==cid))]
                    return self.send(result)
                if path=="/api/search":
                    term=query.get("q",[""])[0].strip()
                    if not term or len(term)>200:raise ValueError("Enter a query of 1-200 characters")
                    return self.send(builder.search_text_coordinates(query.get("manifest",[""])[0],term))
                if path=="/api/reviews":
                    mid=query.get("manifest",[""])[0]
                    return self.send([{"id":r.id,"canvas_id":r.canvas_id,"risk":r.risk_type,"status":r.status,
                        "description":r.description} for r in session.scalars(select(ReviewTask).where(ReviewTask.manifest_id==mid))])
                match=re.fullmatch(r"/iiif/manifest/([^/]+)/manifest.json",path)
                if match:return self.send(builder.build_manifest(match[1]))
                match=re.fullmatch(r"/iiif/manifest/([^/]+)/canvas/([^/]+)(/annotations/ocr.json)?",path)
                if match:
                    canvas=session.get(Canvas,match[2])
                    if not canvas or canvas.manifest_id!=match[1]:raise LookupError()
                    return self.send(builder.build_canvas_ocr_annotation_page(canvas.id) if match[3]
                                     else builder.build_page_bundle(canvas.id))
                match=re.fullmatch(r"/iiif/image/([^/]+)/(\d+)/(info.json|[^/]+/[^/]+/0/default.jpg)",path)
                if match:
                    canvas=session.scalar(select(Canvas).where(Canvas.manifest_id==match[1],Canvas.scan_index==int(match[2])))
                    if not canvas or not canvas.image_storage_path:raise LookupError()
                    service=f"{base_url}/image/{canvas.manifest_id}/{canvas.scan_index}"
                    if match[3]=="info.json":
                        return self.send({"@context":"http://iiif.io/api/image/3/context.json","id":service,
                            "type":"ImageService3","protocol":"http://iiif.io/api/image","profile":"level0",
                            "width":canvas.width,"height":canvas.height,"extraFeatures":["regionByPx"],"sizes":[{"width":canvas.width,"height":canvas.height}]})
                    image_path=Path(canvas.image_storage_path).resolve()
                    if not image_path.is_relative_to(library):raise LookupError()
                    from PIL import Image
                    with Image.open(image_path) as source:
                        image=source.convert("RGB").resize((canvas.width,canvas.height))
                    region,size,_,_=match[3].split("/")
                    if size!="max":raise ValueError("Only max size is supported")
                    if region!="full":
                        if not re.fullmatch(r"\d+,\d+,\d+,\d+",region):raise ValueError("Invalid region")
                        x,y,w,h=map(int,region.split(","))
                        if w<1 or h<1 or x+w>canvas.width or y+h>canvas.height:raise ValueError("Region outside page")
                        image=image.crop((x,y,x+w,y+h))
                    stream=io.BytesIO();image.save(stream,"JPEG",quality=90)
                    return self.send(stream.getvalue(),mime="image/jpeg")
                match=re.fullmatch(r"/api/export/([^/]+)/(manifest.json|quality.json|alto/\d{5}.xml|tables/\d{5}.json)",path)
                if match:
                    manifest=session.get(Manifest,match[1])
                    if not manifest:raise LookupError()
                    export=library/manifest.work.slug/match[2]
                    if not export.exists():raise LookupError()
                    return self.send(export.read_bytes(),mime="application/xml" if export.suffix==".xml" else "application/json")
            raise LookupError()

        def do_POST(self):
            if urlparse(self.path).path!="/api/review":return self.send({"error":"Not found"},404)
            origin=self.headers.get("Origin")
            if origin and origin!=base_url.removesuffix("/iiif"):
                return self.send({"error":"Origin rejected"},403)
            if not self.headers.get("Content-Type","").startswith("application/json"):
                return self.send({"error":"JSON required"},415)
            try:
                length=int(self.headers.get("Content-Length","0"))
                if not 0<length<=100000:raise ValueError("Invalid body length")
                body=json.loads(self.rfile.read(length))
                from src.review import resolve_review
                with get_session(engine) as session:
                    result=resolve_review(session,**body)
                    task=session.get(ReviewTask,body["task_id"])
                    manifest=session.get(Manifest,task.manifest_id)
                    export_id,export_slug=manifest.id,manifest.work.slug
                from src.pipeline import _export
                with get_session(engine) as session:
                    _export(session,export_id,library/export_slug,base_url)
                self.send(result)
            except (ValueError,TypeError) as error:self.send({"error":str(error)},409)
            except LookupError:self.send({"error":"Not found"},404)
            except OperationalError:self.send({"error":"Concurrent edit; reload and retry"},409)
            except Exception:self.send({"error":"Review failed"},500)

    server=ThreadingHTTPServer((host,port),Handler)
    base_url=f"http://127.0.0.1:{server.server_address[1]}/iiif"
    server.engine=engine
    return server
