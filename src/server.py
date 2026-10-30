"""Local document library, IIIF resources, source search and review endpoints."""
from __future__ import annotations

import io
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select, or_
from sqlalchemy.exc import OperationalError
from src.db.models import (
    Canvas,
    Manifest,
    Work,
    AltoLine,
    ReviewTask,
    IiifRange,
    PlateFigure,
    ImageAsset,
    ImageOccurrence,
)
from src.db.session import get_session, init_db
from src.iiif.generator import IiifBuilder


def make_server(library: Path, host: str = "127.0.0.1", port: int = 8000):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Review service currently supports loopback only")
    library = library.resolve()
    engine = init_db("sqlite:///" + (library / "library.db").as_posix())
    base_url = f"http://127.0.0.1:{port}/iiif"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, status=200, mime="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self.handle_get()
            except (ValueError, KeyError) as error:
                self.send({"error": str(error)}, 400)
            except LookupError:
                self.send({"error": "Not found"}, 404)
            except Exception:
                self.send({"error": "Internal error"}, 500)

        def handle_get(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path == "/":
                return self.send(
                    (Path(__file__).parent / "web/library.html").read_bytes(),
                    mime="text/html; charset=utf-8",
                )
            if path == "/health":
                return self.send({"status": "ok", "database": engine.dialect.name})
            with get_session(engine) as session:
                builder = IiifBuilder(session, base_url)
                if path == "/api/documents":
                    return self.send(
                        [
                            {
                                "id": m.id,
                                "title": m.work.title,
                                "pages": m.total_pages,
                                "manifest": f"{base_url}/manifest/{m.id}/manifest.json",
                            }
                            for m in session.scalars(select(Manifest))
                        ]
                    )
                if path == "/api/pages":
                    mid = query.get("manifest", [""])[0]
                    return self.send(
                        [
                            {
                                "id": c.id,
                                "scan_index": c.scan_index,
                                "label": c.printed_page_label,
                                "domain": c.page_domain,
                                "revision": c.revision_no,
                            }
                            for c in session.scalars(
                                select(Canvas)
                                .where(Canvas.manifest_id == mid)
                                .order_by(Canvas.scan_index)
                            )
                        ]
                    )
                if path == "/api/page":
                    cid = query.get("id", [""])[0]
                    canvas = session.get(Canvas, cid)
                    if canvas is None:
                        raise LookupError()
                    result = builder.build_page_bundle(cid)
                    result["lines"] = [
                        {
                            "id": l.id,
                            "text": l.content_text,
                            "raw": l.raw_ocr,
                            "xywh": l.xywh,
                            "status": l.review_status,
                        }
                        for l in session.scalars(
                            select(AltoLine).where(AltoLine.canvas_id == cid)
                        )
                    ]
                    result["figures"] = [
                        {
                            "id": f.id,
                            "figure_number": f.figure_number,
                            "category": f.figure_category,
                            "caption": f.caption_text,
                            "xywh": f"{f.crop_x},{f.crop_y},{f.crop_w},{f.crop_h}",
                            "image_url": f"/api/figure_image?id={f.id}",
                        }
                        for f in session.scalars(
                            select(PlateFigure).where(PlateFigure.canvas_id == cid)
                        )
                    ]
                    return self.send(result)
                if path == "/api/search":
                    term = query.get("q", [""])[0].strip()
                    if not term or len(term) > 200:
                        raise ValueError("Enter a query of 1-200 characters")
                    mid = query.get("manifest", [""])[0]
                    if mid and mid != "all":
                        return self.send(builder.search_text_coordinates(mid, term))
                    stmt = (
                        select(AltoLine, Canvas, Manifest, Work)
                        .join(Canvas, AltoLine.canvas_id == Canvas.id)
                        .join(Manifest, Canvas.manifest_id == Manifest.id)
                        .join(Work, Manifest.work_id == Work.id)
                        .where(AltoLine.content_text.contains(term))
                        .order_by(Work.slug, Canvas.scan_index, AltoLine.line_index)
                        .limit(300)
                    )
                    hits = []
                    for line, canvas, manifest, work in session.execute(stmt).all():
                        c_uri = f"{base_url}/manifest/{manifest.id}/canvas/{canvas.id}"
                        hits.append(
                            {
                                "id": f"{c_uri}/search-hit/{line.id}",
                                "manifest_id": manifest.id,
                                "work_title": work.title,
                                "canvas_id": canvas.id,
                                "scan_index": canvas.scan_index,
                                "printed_page": canvas.printed_page_label,
                                "page_domain": canvas.page_domain,
                                "target": f"{c_uri}#xywh={line.xywh}",
                                "xywh": line.xywh,
                                "body": {"value": line.content_text},
                                "confidence": line.line_confidence,
                            }
                        )
                    return self.send({"total": len(hits), "items": hits, "query": term})
                if path == "/api/toc":
                    mid = query.get("manifest", [""])[0]
                    max_level = int(query.get("max_level", ["4"])[0])
                    stmt = (
                        select(IiifRange, Canvas, Manifest, Work)
                        .join(Manifest, IiifRange.manifest_id == Manifest.id)
                        .join(Work, Manifest.work_id == Work.id)
                        .outerjoin(Canvas, IiifRange.start_canvas_id == Canvas.id)
                    )
                    if mid and mid != "all":
                        stmt = stmt.where(Manifest.id == mid)
                    if max_level > 0:
                        stmt = stmt.where(IiifRange.level <= max_level)
                    stmt = stmt.order_by(Work.slug, IiifRange.sort_order, IiifRange.level)
                    ranges = []
                    for r, c, m, w in session.execute(stmt).all():
                        ranges.append(
                            {
                                "id": r.id,
                                "manifest_id": m.id,
                                "work_title": w.title,
                                "level": r.level,
                                "title": r.title,
                                "section_number": r.section_number,
                                "canvas_id": c.id if c else None,
                                "scan_index": c.scan_index if c else None,
                                "printed_page": c.printed_page_label if c else None,
                                "xywh": r.start_xywh,
                            }
                        )
                    return self.send(ranges)
                if path == "/api/figures":
                    mid = query.get("manifest", [""])[0]
                    q_fig = query.get("q", [""])[0].strip()
                    page = max(1, int(query.get("page", ["1"])[0]))
                    limit = max(1, min(120, int(query.get("limit", ["48"])[0])))
                    offset = (page - 1) * limit

                    stmt = (
                        select(PlateFigure, Canvas, Manifest, Work)
                        .join(Canvas, PlateFigure.canvas_id == Canvas.id)
                        .join(Manifest, PlateFigure.manifest_id == Manifest.id)
                        .join(Work, Manifest.work_id == Work.id)
                    )
                    if mid and mid != "all":
                        stmt = stmt.where(Manifest.id == mid)
                    if q_fig:
                        stmt = stmt.where(
                            or_(
                                PlateFigure.caption_text.contains(q_fig),
                                PlateFigure.figure_number.contains(q_fig),
                                PlateFigure.figure_category.contains(q_fig),
                            )
                        )
                    stmt = stmt.order_by(Work.slug, Canvas.scan_index, PlateFigure.figure_number)
                    all_rows = session.execute(stmt).all()
                    total = len(all_rows)
                    items = []
                    for pf, c, m, w in all_rows[offset : offset + limit]:
                        items.append(
                            {
                                "id": pf.id,
                                "manifest_id": m.id,
                                "work_title": w.title,
                                "canvas_id": c.id,
                                "scan_index": c.scan_index,
                                "printed_page": c.printed_page_label,
                                "figure_number": pf.figure_number,
                                "category": pf.figure_category,
                                "caption": pf.caption_text,
                                "xywh": f"{pf.crop_x},{pf.crop_y},{pf.crop_w},{pf.crop_h}",
                                "has_crop": bool(
                                    pf.crop_w and pf.crop_h and pf.crop_w > 10 and pf.crop_h > 10
                                ),
                                "image_url": f"/api/figure_image?id={pf.id}",
                            }
                        )
                    return self.send(
                        {"total": total, "page": page, "limit": limit, "items": items}
                    )
                if path == "/api/figure_image":
                    fid = query.get("id", [""])[0]
                    pf = session.get(PlateFigure, fid)
                    if not pf:
                        raise LookupError()
                    canvas = session.get(Canvas, pf.canvas_id)
                    if not canvas or not canvas.image_storage_path:
                        raise LookupError()
                    occ = session.scalar(
                        select(ImageOccurrence).where(ImageOccurrence.plate_figure_id == fid)
                    )
                    if occ and occ.image_asset and occ.image_asset.uri:
                        p_asset = Path(occ.image_asset.uri)
                        if p_asset.exists() and p_asset.is_file():
                            return self.send(p_asset.read_bytes(), mime="image/jpeg")
                    img_path = Path(canvas.image_storage_path).resolve()
                    if not img_path.is_relative_to(library) or not img_path.exists():
                        raise LookupError()
                    from PIL import Image

                    with Image.open(img_path) as source:
                        img = source.convert("RGB")
                        cw, ch = canvas.width, canvas.height
                        if img.width != cw or img.height != ch:
                            img = img.resize((cw, ch))
                        x, y, w, h = pf.crop_x, pf.crop_y, pf.crop_w, pf.crop_h
                        if w > 10 and h > 10:
                            x = max(0, min(x, cw - 1))
                            y = max(0, min(y, ch - 1))
                            w = min(w, cw - x)
                            h = min(h, ch - y)
                            img = img.crop((x, y, x + w, y + h))
                        buf = io.BytesIO()
                        img.save(buf, "JPEG", quality=88)
                        return self.send(buf.getvalue(), mime="image/jpeg")
                if path == "/api/reviews":
                    mid = query.get("manifest", [""])[0]
                    return self.send(
                        [
                            {
                                "id": r.id,
                                "canvas_id": r.canvas_id,
                                "risk": r.risk_type,
                                "status": r.status,
                                "description": r.description,
                            }
                            for r in session.scalars(
                                select(ReviewTask).where(ReviewTask.manifest_id == mid)
                            )
                        ]
                    )
                match = re.fullmatch(r"/iiif/manifest/([^/]+)/manifest.json", path)
                if match:
                    return self.send(builder.build_manifest(match[1]))
                match = re.fullmatch(
                    r"/iiif/manifest/([^/]+)/canvas/([^/]+)(/annotations/ocr.json)?", path
                )
                if match:
                    canvas = session.get(Canvas, match[2])
                    if not canvas or canvas.manifest_id != match[1]:
                        raise LookupError()
                    return self.send(
                        builder.build_canvas_ocr_annotation_page(canvas.id)
                        if match[3]
                        else builder.build_page_bundle(canvas.id)
                    )
                match = re.fullmatch(
                    r"/iiif/image/([^/]+)/(\d+)/(info.json|[^/]+/[^/]+/0/default.jpg)", path
                )
                if match:
                    canvas = session.scalar(
                        select(Canvas).where(
                            Canvas.manifest_id == match[1],
                            Canvas.scan_index == int(match[2]),
                        )
                    )
                    if not canvas or not canvas.image_storage_path:
                        raise LookupError()
                    service = f"{base_url}/image/{canvas.manifest_id}/{canvas.scan_index}"
                    if match[3] == "info.json":
                        return self.send(
                            {
                                "@context": "http://iiif.io/api/image/3/context.json",
                                "id": service,
                                "type": "ImageService3",
                                "protocol": "http://iiif.io/api/image",
                                "profile": "level0",
                                "width": canvas.width,
                                "height": canvas.height,
                                "extraFeatures": ["regionByPx"],
                                "sizes": [{"width": canvas.width, "height": canvas.height}],
                            }
                        )
                    image_path = Path(canvas.image_storage_path).resolve()
                    if not image_path.is_relative_to(library):
                        raise LookupError()
                    from PIL import Image

                    with Image.open(image_path) as source:
                        image = source.convert("RGB").resize((canvas.width, canvas.height))
                    region, size, _, _ = match[3].split("/")
                    if size != "max":
                        raise ValueError("Only max size is supported")
                    if region != "full":
                        if not re.fullmatch(r"\d+,\d+,\d+,\d+", region):
                            raise ValueError("Invalid region")
                        x, y, w, h = map(int, region.split(","))
                        if w < 1 or h < 1 or x + w > canvas.width or y + h > canvas.height:
                            raise ValueError("Region outside page")
                        image = image.crop((x, y, x + w, y + h))
                    stream = io.BytesIO()
                    image.save(stream, "JPEG", quality=90)
                    return self.send(stream.getvalue(), mime="image/jpeg")
                match = re.fullmatch(
                    r"/api/export/([^/]+)/(manifest.json|quality.json|alto/\d{5}.xml|tables/\d{5}.json)",
                    path,
                )
                if match:
                    manifest = session.get(Manifest, match[1])
                    if not manifest:
                        raise LookupError()
                    export = library / manifest.work.slug / match[2]
                    if not export.exists():
                        raise LookupError()
                    return self.send(
                        export.read_bytes(),
                        mime="application/xml"
                        if export.suffix == ".xml"
                        else "application/json",
                    )
            raise LookupError()

        def do_POST(self):
            if urlparse(self.path).path != "/api/review":
                return self.send({"error": "Not found"}, 404)
            origin = self.headers.get("Origin")
            if origin and origin != base_url.removesuffix("/iiif"):
                return self.send({"error": "Origin rejected"}, 403)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send({"error": "JSON required"}, 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 100000:
                    raise ValueError("Invalid body length")
                body = json.loads(self.rfile.read(length))
                from src.review import resolve_review

                with get_session(engine) as session:
                    result = resolve_review(session, **body)
                    task = session.get(ReviewTask, body["task_id"])
                    manifest = session.get(Manifest, task.manifest_id)
                    export_id, export_slug = manifest.id, manifest.work.slug
                from src.pipeline import _export

                with get_session(engine) as session:
                    _export(session, export_id, library / export_slug, base_url)
                self.send(result)
            except (ValueError, TypeError) as error:
                self.send({"error": str(error)}, 409)
            except LookupError:
                self.send({"error": "Not found"}, 404)
            except OperationalError:
                self.send({"error": "Concurrent edit; reload and retry"}, 409)
            except Exception:
                self.send({"error": "Review failed"}, 500)

    server = ThreadingHTTPServer((host, port), Handler)
    base_url = f"http://127.0.0.1:{server.server_address[1]}/iiif"
    server.engine = engine
    return server
