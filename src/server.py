"""Local document library, IIIF resources, cross-volume index, gallery, search and review endpoints."""
from __future__ import annotations

import io
import json
import re
import threading
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select, or_, func
from sqlalchemy.exc import OperationalError
from src.db.models import (
    AltoBlock,
    AltoLine,
    Artifact,
    ArtifactOccurrence,
    Canvas,
    IiifRange,
    ImageAsset,
    ImageOccurrence,
    Manifest,
    PlateFigure,
    ReviewTask,
    Work,
)
from src.db.session import get_session, init_db
from src.iiif.generator import IiifBuilder


_COLON_RE = re.compile(r"[∶︰：:]")
_SPACE_RE = re.compile(r"[\u3000\s]+")
_NUM_CAPTION_RE = re.compile(r"^(\d+)[\.．、\s]\s*(.+)$")
_SPECIMEN_EXTRACT_RE = re.compile(
    r"([A-Za-z0-9\u4e00-\u9fa5①-⑳Ⅰ-Ⅻ]+[∶︰：:][A-Za-z0-9\-～~—\.]+)"
)


def _norm_text(text: str | None) -> str:
    if not text:
        return ""
    s = _COLON_RE.sub(":", text)
    s = _SPACE_RE.sub(" ", s).strip().lower()
    return s


def _norm_tokens(query: str) -> list[str]:
    q = _norm_text(query)
    return [t for t in q.split(" ") if t]


def _matches(norm_haystack: str, tokens: list[str]) -> bool:
    if not tokens:
        return True
    return all(t in norm_haystack for t in tokens)


def _short_vol_title(title: str) -> str:
    m = re.search(r"(第[一二三四五六七八九十\d]+卷)", title)
    if m:
        return m.group(1)
    return title.replace("-已复", "")


def _extract_vessel_name(snippet: str) -> str:
    if not snippet:
        return ""
    s = snippet.strip().splitlines()[0]
    if s.startswith("标本"):
        return ""
    m = _NUM_CAPTION_RE.match(s)
    if m:
        s = m.group(2).strip()
    if re.search(r"\s+\d+[\.．、]", s):
        return ""
    s = re.sub(r"[（\(][^）\)]*[）\)]", "", s).strip()
    s = re.sub(r"(正面|背面|侧面|底面|内底|外底|俯视|仰视)$", "", s).strip()
    if len(s) > 24 or len(s) < 2:
        return ""
    return s


def _compute_plate_subfigures(
    cw: int, ch: int, print_blocks: list[tuple[int, int, int, int, str]]
) -> tuple[str, list[dict]]:
    """Given print_space blocks (x, y, w, h, text) on a plate canvas,
    return (plate_summary_title, list_of_subfigure_dicts).
    """
    numbered = []
    summaries = []
    for bx, by, bw, bh, btxt in print_blocks:
        txt = btxt.strip()
        if not txt:
            continue
        m = _NUM_CAPTION_RE.match(txt)
        if m and len(txt) < 120:
            numbered.append((bx, by, bw, bh, txt, m.group(1)))
        elif by > ch * 0.72 and len(txt) < 80:
            summaries.append(txt)

    plate_title = " · ".join(dict.fromkeys(summaries))

    # Group numbered captions into horizontal rows
    numbered.sort(key=lambda item: (item[1], item[0]))
    rows: list[list[tuple[int, int, int, int, str, str]]] = []
    for item in numbered:
        if not rows or abs(item[1] - rows[-1][0][1]) > 42:
            rows.append([item])
        else:
            rows[-1].append(item)

    sub_figs = []
    for r_idx, row in enumerate(rows):
        row.sort(key=lambda item: item[0])
        if r_idx == 0:
            row_top = int(ch * 0.11)
        else:
            prev_bottom = max(it[1] + it[3] for it in rows[r_idx - 1])
            row_top = prev_bottom + 14
        row_bottom = max(it[1] + it[3] for it in row) + 8
        photo_h = max(60, row_bottom - row_top)

        n_cols = len(row)
        for c_idx, (bx, by, bw, bh, txt, num_str) in enumerate(row):
            center_x = bx + bw / 2
            if n_cols == 1:
                if center_x < cw * 0.43:
                    x = int(cw * 0.12)
                    w = int(cw * 0.37)
                elif center_x > cw * 0.57:
                    x = int(cw * 0.51)
                    w = int(cw * 0.37)
                else:
                    x = int(cw * 0.12)
                    w = int(cw * 0.76)
            elif n_cols == 2:
                if c_idx == 0:
                    x = int(cw * 0.12)
                    w = int(cw * 0.37)
                else:
                    x = int(cw * 0.51)
                    w = int(cw * 0.37)
            else:
                usable_w = int(cw * 0.78)
                col_w = usable_w // n_cols
                x = int(cw * 0.11) + c_idx * col_w
                w = col_w - 8

            xywh = f"{x},{row_top},{w},{photo_h}"
            sub_figs.append(
                {
                    "sub_index": num_str,
                    "caption": txt,
                    "caption_xywh": f"{bx},{by},{bw},{bh}",
                    "xywh": xywh,
                }
            )

    return plate_title, sub_figs


def _build_index_cache(engine) -> dict:
    """Build an in-memory multi-volume index of documents, TOC, figures, and cross-volume artifacts."""
    with get_session(engine) as session:
        # 1. Load Works & Manifests ordered by Work.title
        doc_rows = session.execute(
            select(Manifest, Work)
            .join(Work, Manifest.work_id == Work.id)
            .order_by(Work.title)
        ).all()

        documents = []
        manifest_meta = {}
        for m, w in doc_rows:
            short = _short_vol_title(w.title)
            meta = {
                "id": m.id,
                "work_id": w.id,
                "slug": w.slug,
                "title": w.title,
                "short_title": short,
                "pages": m.total_pages,
            }
            documents.append(meta)
            manifest_meta[m.id] = meta

        # 2. Load Canvases
        canvases_by_id = {}
        for c in session.scalars(select(Canvas)):
            canvases_by_id[c.id] = {
                "id": c.id,
                "manifest_id": c.manifest_id,
                "scan_index": c.scan_index,
                "printed_page": c.printed_page_label,
                "domain": c.page_domain,
                "width": c.width,
                "height": c.height,
            }

        # 3. Load print_space and caption blocks per canvas
        blocks_by_canvas: dict[str, list[tuple[int, int, int, int, str, str, str]]] = (
            defaultdict(list)
        )
        for cid, btype, zone, x, y, w, h, txt in session.execute(
            select(
                AltoBlock.canvas_id,
                AltoBlock.block_type,
                AltoBlock.page_zone,
                AltoBlock.x,
                AltoBlock.y,
                AltoBlock.w,
                AltoBlock.h,
                AltoBlock.content_text,
            ).order_by(AltoBlock.y, AltoBlock.x)
        ).all():
            if txt:
                blocks_by_canvas[cid].append(
                    (x, y, w, h, txt.strip(), btype or "", zone or "")
                )

        # 4. Load ImageOccurrences to know which figures have pre-cropped files
        cropped_fig_ids = set(
            session.scalars(
                select(ImageOccurrence.plate_figure_id).where(
                    ImageOccurrence.plate_figure_id.is_not(None)
                )
            ).all()
        )

        # 5. Enrich Figures & Build Plate TOC for Plate Volumes
        raw_figures = session.execute(
            select(PlateFigure).order_by(PlateFigure.canvas_id, PlateFigure.figure_number)
        ).all()

        figures = []
        sub_figures_flat = []
        figures_by_canvas = defaultdict(list)
        plate_toc_by_manifest = defaultdict(list)
        # Map normalized specimen code -> list of figure/sub-figure occurrences
        specimen_fig_map = defaultdict(list)

        for (pf,) in raw_figures:
            c_info = canvases_by_id.get(pf.canvas_id)
            m_info = manifest_meta.get(pf.manifest_id)
            if not c_info or not m_info:
                continue

            cw, ch = c_info["width"], c_info["height"]
            c_blocks = blocks_by_canvas.get(pf.canvas_id, [])
            print_blocks = [
                (bx, by, bw, bh, btxt)
                for bx, by, bw, bh, btxt, btype, bzone in c_blocks
                if bzone == "print_space"
            ]

            fig_num = (pf.figure_number or "").strip()
            raw_cap = (pf.caption_text or "").strip()
            is_plate = fig_num.startswith("图版") or (
                pf.crop_h is not None and pf.crop_h <= 80 and "图版" in raw_cap
            )

            sub_figs = []
            if is_plate:
                plate_title, sub_figs_raw = _compute_plate_subfigures(
                    cw, ch, print_blocks
                )
                display_title = (
                    f"{fig_num} · {plate_title}" if plate_title else fig_num or raw_cap
                )
                sub_captions = [sf["caption"] for sf in sub_figs_raw]
                full_caption = (
                    f"{plate_title} — " + " | ".join(sub_captions)
                    if plate_title and sub_captions
                    else (" | ".join(sub_captions) if sub_captions else display_title)
                )
                if pf.crop_h is None or pf.crop_h <= 80:
                    xywh = f"{int(cw * 0.10)},{int(ch * 0.09)},{int(cw * 0.80)},{int(ch * 0.83)}"
                else:
                    xywh = f"{pf.crop_x},{pf.crop_y},{pf.crop_w},{pf.crop_h}"

                for sf in sub_figs_raw:
                    sf_url = f"/api/figure_image?id={pf.id}&xywh={sf['xywh']}"
                    sf_entry = {
                        "id": f"{pf.id}_sub_{sf['sub_index']}",
                        "parent_figure_id": pf.id,
                        "manifest_id": pf.manifest_id,
                        "work_title": m_info["title"],
                        "short_title": m_info["short_title"],
                        "canvas_id": pf.canvas_id,
                        "scan_index": c_info["scan_index"],
                        "printed_page": c_info["printed_page"],
                        "figure_number": f"{fig_num}-{sf['sub_index']}",
                        "category": "sub_plate",
                        "title": f"{fig_num}（{sf['sub_index']}）{plate_title}",
                        "caption": sf["caption"],
                        "xywh": sf["xywh"],
                        "image_url": sf_url,
                        "_norm": _norm_text(
                            f"{m_info['title']} {fig_num} {plate_title} {sf['caption']}"
                        ),
                    }
                    sub_figs.append(sf_entry)
                    sub_figures_flat.append(sf_entry)

                    # Link specimen codes in sub-figure caption
                    for code_match in _SPECIMEN_EXTRACT_RE.findall(sf["caption"]):
                        norm_c = _norm_text(code_match).upper()
                        specimen_fig_map[norm_c].append(
                            {
                                "manifest_id": pf.manifest_id,
                                "work_title": m_info["title"],
                                "short_title": m_info["short_title"],
                                "canvas_id": pf.canvas_id,
                                "scan_index": c_info["scan_index"],
                                "printed_page": c_info["printed_page"],
                                "role": "plate",
                                "figure_number": f"{fig_num}（{sf['sub_index']}）",
                                "snippet": sf["caption"],
                                "xywh": sf["xywh"],
                                "thumb_url": sf_url,
                            }
                        )
            else:
                # Line drawing or photo in Text Volume (Vol 1 & 2)
                extra_lines = []
                for bx, by, bw, bh, btxt in print_blocks:
                    if btxt != raw_cap and (
                        _NUM_CAPTION_RE.match(btxt)
                        or ("∶" in btxt or "︰" in btxt)
                        and len(btxt) < 160
                    ):
                        extra_lines.append(btxt)
                display_title = raw_cap.splitlines()[0] if raw_cap else fig_num
                full_caption = raw_cap
                if extra_lines:
                    extra_joined = " ".join(extra_lines)
                    if extra_joined not in full_caption:
                        full_caption = f"{full_caption}\n{extra_joined}".strip()
                xywh = f"{pf.crop_x},{pf.crop_y},{pf.crop_w},{pf.crop_h}"

                for code_match in _SPECIMEN_EXTRACT_RE.findall(full_caption):
                    norm_c = _norm_text(code_match).upper()
                    specimen_fig_map[norm_c].append(
                        {
                            "manifest_id": pf.manifest_id,
                            "work_title": m_info["title"],
                            "short_title": m_info["short_title"],
                            "canvas_id": pf.canvas_id,
                            "scan_index": c_info["scan_index"],
                            "printed_page": c_info["printed_page"],
                            "role": "drawing",
                            "figure_number": fig_num or "插图",
                            "snippet": display_title,
                            "xywh": xywh,
                            "thumb_url": f"/api/figure_image?id={pf.id}",
                        }
                    )

            fig_obj = {
                "id": pf.id,
                "manifest_id": pf.manifest_id,
                "work_title": m_info["title"],
                "short_title": m_info["short_title"],
                "canvas_id": pf.canvas_id,
                "scan_index": c_info["scan_index"],
                "printed_page": c_info["printed_page"],
                "figure_number": fig_num or "图版",
                "category": "plate" if is_plate else "line_drawing",
                "title": display_title,
                "caption": full_caption,
                "xywh": xywh,
                "has_crop": pf.id in cropped_fig_ids or is_plate,
                "image_url": f"/api/figure_image?id={pf.id}",
                "sub_figures": sub_figs,
                "_norm": _norm_text(
                    f"{m_info['title']} {fig_num} {display_title} {full_caption}"
                ),
            }
            figures.append(fig_obj)
            figures_by_canvas[pf.canvas_id].append(fig_obj)

            if is_plate:
                plate_toc_by_manifest[pf.manifest_id].append((fig_obj, sub_figs))

        # Sort figures by volume title, then scan_index
        figures.sort(key=lambda f: (f["work_title"], f["scan_index"], f["figure_number"]))
        sub_figures_flat.sort(
            key=lambda f: (f["work_title"], f["scan_index"], f["figure_number"])
        )

        # 6. Build Complete Multi-Volume TOC (covering Vol 1-2 chapters AND Vol 3-5 plates)
        toc_all = []
        raw_ranges = session.execute(
            select(IiifRange)
            .order_by(IiifRange.manifest_id, IiifRange.sort_order, IiifRange.level)
        ).all()
        ranges_by_manifest = defaultdict(list)
        for (r,) in raw_ranges:
            ranges_by_manifest[r.manifest_id].append(r)

        for doc in documents:
            mid = doc["id"]
            m_ranges = ranges_by_manifest.get(mid, [])
            has_high_level_toc = any(r.level <= 3 for r in m_ranges)

            if has_high_level_toc:
                for r in m_ranges:
                    c_info = canvases_by_id.get(r.start_canvas_id)
                    clean_title = re.sub(r"\u3000+", " ", (r.title or "").strip())
                    toc_all.append(
                        {
                            "id": r.id,
                            "manifest_id": mid,
                            "work_title": doc["title"],
                            "short_title": doc["short_title"],
                            "level": r.level,
                            "title": clean_title,
                            "section_number": r.section_number,
                            "canvas_id": c_info["id"] if c_info else None,
                            "scan_index": c_info["scan_index"] if c_info else None,
                            "printed_page": (
                                c_info["printed_page"] if c_info else r.start_label
                            ),
                            "xywh": r.start_xywh,
                            "_norm": _norm_text(f"{doc['title']} {clean_title}"),
                        }
                    )
            else:
                # Synthesize structured Plate TOC for Plate Volumes (Vol 3, 4, 5)
                plate_items = plate_toc_by_manifest.get(mid, [])
                plate_items.sort(key=lambda pair: pair[0]["scan_index"])
                current_prefix = None
                for fig_obj, sub_figs in plate_items:
                    fnum = fig_obj["figure_number"]
                    m_pref = re.match(r"^(图版\d+\.\d+)", fnum)
                    prefix = m_pref.group(1) if m_pref else "图版目录"
                    if prefix != current_prefix:
                        current_prefix = prefix
                        sec_desc = (
                            "遗迹与地层图版"
                            if prefix.endswith(".1")
                            else "出土器物标本图版"
                        )
                        toc_all.append(
                            {
                                "id": f"plate_group_{mid}_{prefix}",
                                "manifest_id": mid,
                                "work_title": doc["title"],
                                "short_title": doc["short_title"],
                                "level": 1,
                                "title": f"{prefix} 系列 · {sec_desc}",
                                "section_number": prefix,
                                "canvas_id": fig_obj["canvas_id"],
                                "scan_index": fig_obj["scan_index"],
                                "printed_page": fig_obj["printed_page"],
                                "xywh": fig_obj["xywh"],
                                "_norm": _norm_text(
                                    f"{doc['title']} {prefix} {sec_desc}"
                                ),
                            }
                        )
                    sub_count = len(sub_figs)
                    suffix = f"（共 {sub_count} 帧）" if sub_count else ""
                    toc_all.append(
                        {
                            "id": f"plate_toc_{fig_obj['id']}",
                            "manifest_id": mid,
                            "work_title": doc["title"],
                            "short_title": doc["short_title"],
                            "level": 2,
                            "title": f"{fig_obj['title']}{suffix}",
                            "section_number": fnum,
                            "canvas_id": fig_obj["canvas_id"],
                            "scan_index": fig_obj["scan_index"],
                            "printed_page": fig_obj["printed_page"],
                            "xywh": fig_obj["xywh"],
                            "_norm": fig_obj["_norm"],
                        }
                    )
                    for sf in sub_figs:
                        toc_all.append(
                            {
                                "id": sf["id"],
                                "manifest_id": mid,
                                "work_title": doc["title"],
                                "short_title": doc["short_title"],
                                "level": 4,
                                "title": f"[{fnum}] {sf['caption']}",
                                "section_number": sf["figure_number"],
                                "canvas_id": sf["canvas_id"],
                                "scan_index": sf["scan_index"],
                                "printed_page": sf["printed_page"],
                                "xywh": sf["xywh"],
                                "_norm": sf["_norm"],
                            }
                        )

        # 7. Build Cross-Volume Artifact & Excavation Unit Index
        occ_rows = session.execute(
            select(
                Artifact.artifact_no,
                Artifact.excavation_unit,
                ArtifactOccurrence.canvas_id,
                ArtifactOccurrence.context_snippet,
                ArtifactOccurrence.target_xywh,
            ).join(Artifact, ArtifactOccurrence.artifact_id == Artifact.id)
        ).all()

        artifacts_by_code = {}
        unit_counter = Counter()
        unit_cross_counter = Counter()

        for art_no, exc_unit, cid, snippet, t_xywh in occ_rows:
            c_info = canvases_by_id.get(cid)
            if not c_info:
                continue
            m_info = manifest_meta.get(c_info["manifest_id"])
            if not m_info:
                continue

            norm_code = _norm_text(art_no).upper()
            disp_code = _COLON_RE.sub("∶", (art_no or "").strip())
            unit = (exc_unit or "").strip()
            if not unit and "∶" in disp_code:
                unit = disp_code.split("∶")[0]
            # Normalize stratum symbols off unit to get base excavation unit (e.g. T3H5, F12, T10)
            base_unit_m = re.match(r"^([A-Za-z]+\d+(?:扩)?(?:[A-Za-z]+\d+)?)", unit)
            base_unit = base_unit_m.group(1).upper() if base_unit_m else unit

            entry = artifacts_by_code.get(norm_code)
            if not entry:
                entry = {
                    "code": disp_code,
                    "norm_code": norm_code,
                    "unit": base_unit,
                    "raw_unit": unit,
                    "vessel_name": "",
                    "occurrences": [],
                    "_seen_keys": set(),
                }
                artifacts_by_code[norm_code] = entry

            v_name = _extract_vessel_name(snippet or "")
            if v_name and (
                not entry["vessel_name"] or len(v_name) > len(entry["vessel_name"])
            ):
                entry["vessel_name"] = v_name

            dedup_key = (c_info["manifest_id"], c_info["scan_index"], (snippet or "")[:40])
            if dedup_key not in entry["_seen_keys"]:
                entry["_seen_keys"].add(dedup_key)
                is_plate_vol = c_info["domain"] == "plate" or m_info["title"].startswith(
                    ("3", "4", "5")
                )
                entry["occurrences"].append(
                    {
                        "manifest_id": c_info["manifest_id"],
                        "work_title": m_info["title"],
                        "short_title": m_info["short_title"],
                        "canvas_id": cid,
                        "scan_index": c_info["scan_index"],
                        "printed_page": c_info["printed_page"],
                        "role": "plate" if is_plate_vol else "text",
                        "snippet": (snippet or "").strip(),
                        "xywh": t_xywh,
                        "thumb_url": None,
                    }
                )

        # Attach figure/plate thumbnails to matching specimen codes
        for norm_code, fig_occs in specimen_fig_map.items():
            entry = artifacts_by_code.get(norm_code)
            if not entry:
                continue
            for f_occ in fig_occs:
                v_name = _extract_vessel_name(f_occ.get("snippet") or "")
                if v_name and not entry["vessel_name"]:
                    entry["vessel_name"] = v_name
                # Check if we already have an occurrence on the same canvas; if so, attach thumb_url and upgrade xywh
                matched_existing = False
                for occ in entry["occurrences"]:
                    if occ["canvas_id"] == f_occ["canvas_id"]:
                        occ["thumb_url"] = f_occ["thumb_url"]
                        occ["figure_number"] = f_occ["figure_number"]
                        if f_occ["role"] == "plate":
                            occ["xywh"] = f_occ["xywh"]
                            occ["role"] = "plate"
                        matched_existing = True
                        break
                if not matched_existing:
                    entry["occurrences"].append(
                        {
                            "manifest_id": f_occ["manifest_id"],
                            "work_title": f_occ["work_title"],
                            "short_title": f_occ["short_title"],
                            "canvas_id": f_occ["canvas_id"],
                            "scan_index": f_occ["scan_index"],
                            "printed_page": f_occ["printed_page"],
                            "role": f_occ["role"],
                            "figure_number": f_occ["figure_number"],
                            "snippet": f_occ["snippet"],
                            "xywh": f_occ["xywh"],
                            "thumb_url": f_occ["thumb_url"],
                        }
                    )

        cross_artifacts = []
        for norm_code, entry in artifacts_by_code.items():
            del entry["_seen_keys"]
            entry["occurrences"].sort(
                key=lambda o: (o["work_title"], o["scan_index"])
            )
            vols = list(
                dict.fromkeys(o["short_title"] for o in entry["occurrences"])
            )
            mids = list(
                dict.fromkeys(o["manifest_id"] for o in entry["occurrences"])
            )
            entry["volumes"] = vols
            entry["manifest_ids"] = mids
            entry["volume_count"] = len(vols)
            entry["has_thumb"] = any(o.get("thumb_url") for o in entry["occurrences"])
            snippets_joined = " ".join(o["snippet"] for o in entry["occurrences"])
            entry["_norm"] = _norm_text(
                f"{entry['code']} {entry['unit']} {entry['vessel_name']} {' '.join(vols)} {snippets_joined}"
            )
            cross_artifacts.append(entry)

            if entry["unit"]:
                unit_counter[entry["unit"]] += 1
                if entry["volume_count"] > 1:
                    unit_cross_counter[entry["unit"]] += 1

        # Sort artifacts: multi-volume with thumbnails first, then by volume_count desc, then code
        cross_artifacts.sort(
            key=lambda a: (
                -int(a["volume_count"] > 1 and a["has_thumb"]),
                -a["volume_count"],
                -len(a["occurrences"]),
                a["code"],
            )
        )

        top_units = [
            {
                "unit": u,
                "count": cnt,
                "cross_count": unit_cross_counter[u],
            }
            for u, cnt in unit_counter.most_common(60)
            if len(u) >= 2
        ]

        # Update document summary counts
        for doc in documents:
            mid = doc["id"]
            doc["figures_count"] = sum(
                1 for f in figures if f["manifest_id"] == mid
            )
            doc["toc_count"] = sum(
                1 for t in toc_all if t["manifest_id"] == mid and t["level"] <= 3
            )
            doc["artifacts_count"] = sum(
                1 for a in cross_artifacts if mid in a["manifest_ids"]
            )

        return {
            "documents": documents,
            "manifest_meta": manifest_meta,
            "canvases_by_id": canvases_by_id,
            "toc": toc_all,
            "figures": figures,
            "sub_figures_flat": sub_figures_flat,
            "figures_by_canvas": figures_by_canvas,
            "cross_artifacts": cross_artifacts,
            "top_units": top_units,
        }


def make_server(library: Path, host: str = "127.0.0.1", port: int = 8000):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Review service currently supports loopback only")
    library = library.resolve()
    engine = init_db("sqlite:///" + (library / "library.db").as_posix())
    base_url = f"http://127.0.0.1:{port}/iiif"
    cache_lock = threading.Lock()
    index_cache: dict | None = None

    def get_index() -> dict:
        nonlocal index_cache
        with cache_lock:
            if index_cache is None:
                index_cache = _build_index_cache(engine)
            return index_cache

    def invalidate_index():
        nonlocal index_cache
        with cache_lock:
            index_cache = None

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
            except Exception as error:
                self.send({"error": f"Internal error: {type(error).__name__}"}, 500)

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

            idx = get_index()

            with get_session(engine) as session:
                builder = IiifBuilder(session, base_url)

                if path == "/api/documents":
                    return self.send(
                        [
                            {
                                **doc,
                                "manifest": f"{base_url}/manifest/{doc['id']}/manifest.json",
                            }
                            for doc in idx["documents"]
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
                            select(AltoLine)
                            .where(AltoLine.canvas_id == cid)
                            .order_by(AltoLine.line_index)
                        )
                    ]
                    canvas_figs = idx["figures_by_canvas"].get(cid, [])
                    result["figures"] = [
                        {
                            k: v
                            for k, v in f.items()
                            if not k.startswith("_")
                        }
                        for f in canvas_figs
                    ]
                    return self.send(result)

                if path == "/api/toc":
                    mid = query.get("manifest", ["all"])[0]
                    max_level = int(query.get("max_level", ["3"])[0])
                    q_str = query.get("q", [""])[0].strip()
                    tokens = _norm_tokens(q_str)

                    items = []
                    for item in idx["toc"]:
                        if mid and mid != "all" and item["manifest_id"] != mid:
                            continue
                        # If searching by keyword, search across all levels; otherwise respect max_level
                        if not tokens and max_level > 0 and item["level"] > max_level:
                            continue
                        if tokens and not _matches(item["_norm"], tokens):
                            continue
                        items.append(
                            {k: v for k, v in item.items() if not k.startswith("_")}
                        )
                    return self.send(items)

                if path == "/api/figures":
                    mid = query.get("manifest", ["all"])[0]
                    q_fig = query.get("q", [""])[0].strip()
                    fig_type = query.get("type", ["all"])[0]
                    page = max(1, int(query.get("page", ["1"])[0]))
                    limit = max(1, min(120, int(query.get("limit", ["48"])[0])))
                    offset = (page - 1) * limit
                    tokens = _norm_tokens(q_fig)

                    source_list = (
                        idx["sub_figures_flat"]
                        if fig_type == "sub_plate"
                        else idx["figures"]
                    )
                    filtered = []
                    vol_counts = Counter()

                    for f in source_list:
                        if fig_type == "line_drawing" and f["category"] != "line_drawing":
                            continue
                        if fig_type == "plate" and f["category"] != "plate":
                            continue
                        if tokens and not _matches(f["_norm"], tokens):
                            continue
                        vol_counts[f["manifest_id"]] += 1
                        if mid and mid != "all" and f["manifest_id"] != mid:
                            continue
                        filtered.append(f)

                    total = len(filtered)
                    page_items = [
                        {
                            k: (
                                [
                                    {sk: sv for sk, sv in sf.items() if not sk.startswith("_")}
                                    for sf in v
                                ]
                                if k == "sub_figures"
                                else v
                            )
                            for k, v in f.items()
                            if not k.startswith("_")
                        }
                        for f in filtered[offset : offset + limit]
                    ]
                    return self.send(
                        {
                            "total": total,
                            "page": page,
                            "limit": limit,
                            "volume_counts": dict(vol_counts),
                            "items": page_items,
                        }
                    )

                if path == "/api/cross_index":
                    mid = query.get("manifest", ["all"])[0]
                    unit_filter = query.get("unit", [""])[0].strip().upper()
                    q_str = query.get("q", [""])[0].strip()
                    cross_only = query.get("cross_only", ["0"])[0] in {"1", "true"}
                    page = max(1, int(query.get("page", ["1"])[0]))
                    limit = max(1, min(100, int(query.get("limit", ["40"])[0])))
                    offset = (page - 1) * limit
                    tokens = _norm_tokens(q_str)

                    matched_artifacts = []
                    vol_art_counts = Counter()
                    for art in idx["cross_artifacts"]:
                        if cross_only and art["volume_count"] < 2:
                            continue
                        if unit_filter and art["unit"] != unit_filter:
                            continue
                        if tokens and not _matches(art["_norm"], tokens):
                            continue
                        for m_id in art["manifest_ids"]:
                            vol_art_counts[m_id] += 1
                        if mid and mid != "all" and mid not in art["manifest_ids"]:
                            continue
                        matched_artifacts.append(art)

                    total_artifacts = len(matched_artifacts)
                    art_slice = [
                        {k: v for k, v in a.items() if not k.startswith("_")}
                        for a in matched_artifacts[offset : offset + limit]
                    ]

                    # Also match TOC & Figures if query or unit is provided
                    search_tokens = tokens or (
                        [_norm_text(unit_filter)] if unit_filter else []
                    )
                    matched_toc = []
                    matched_figs = []
                    if search_tokens:
                        for t_item in idx["toc"]:
                            if mid and mid != "all" and t_item["manifest_id"] != mid:
                                continue
                            if _matches(t_item["_norm"], search_tokens):
                                matched_toc.append(
                                    {
                                        k: v
                                        for k, v in t_item.items()
                                        if not k.startswith("_")
                                    }
                                )
                                if len(matched_toc) >= 60:
                                    break
                        for f_item in idx["figures"]:
                            if mid and mid != "all" and f_item["manifest_id"] != mid:
                                continue
                            if _matches(f_item["_norm"], search_tokens):
                                matched_figs.append(
                                    {
                                        k: v
                                        for k, v in f_item.items()
                                        if not k.startswith("_") and k != "sub_figures"
                                    }
                                )
                                if len(matched_figs) >= 40:
                                    break

                    return self.send(
                        {
                            "total_artifacts": total_artifacts,
                            "cross_linked_total": sum(
                                1 for a in idx["cross_artifacts"] if a["volume_count"] > 1
                            ),
                            "page": page,
                            "limit": limit,
                            "volume_counts": dict(vol_art_counts),
                            "top_units": idx["top_units"],
                            "artifacts": art_slice,
                            "toc_hits": matched_toc,
                            "figure_hits": matched_figs,
                        }
                    )

                if path == "/api/search":
                    term = query.get("q", [""])[0].strip()
                    if not term or len(term) > 200:
                        raise ValueError("Enter a query of 1-200 characters")
                    mid = query.get("manifest", [""])[0]
                    tokens = _norm_tokens(term)

                    # Colon-normalized SQL column expression
                    norm_col = func.lower(
                        func.replace(
                            func.replace(
                                func.replace(AltoLine.content_text, "∶", ":"),
                                "︰",
                                ":",
                            ),
                            "：",
                            ":",
                        )
                    )
                    stmt = (
                        select(AltoLine, Canvas, Manifest, Work)
                        .join(Canvas, AltoLine.canvas_id == Canvas.id)
                        .join(Manifest, Canvas.manifest_id == Manifest.id)
                        .join(Work, Manifest.work_id == Work.id)
                    )
                    for tok in tokens:
                        stmt = stmt.where(norm_col.contains(tok))
                    if mid and mid != "all":
                        stmt = stmt.where(Manifest.id == mid)
                    stmt = stmt.order_by(
                        Work.title, Canvas.scan_index, AltoLine.line_index
                    )

                    all_rows = session.execute(stmt).all()
                    vol_counts = Counter()
                    hits = []
                    for line, canvas, manifest, work in all_rows:
                        vol_counts[manifest.id] += 1
                        if len(hits) < 400:
                            c_uri = f"{base_url}/manifest/{manifest.id}/canvas/{canvas.id}"
                            hits.append(
                                {
                                    "id": f"{c_uri}/search-hit/{line.id}",
                                    "type": "Annotation",
                                    "motivation": "supplementing",
                                    "manifest_id": manifest.id,
                                    "work_title": work.title,
                                    "short_title": _short_vol_title(work.title),
                                    "canvas_id": canvas.id,
                                    "scan_index": canvas.scan_index,
                                    "printed_page": canvas.printed_page_label,
                                    "page_domain": canvas.page_domain,
                                    "target": f"{c_uri}#xywh={line.xywh}",
                                    "xywh": line.xywh,
                                    "body": {
                                        "type": "TextualBody",
                                        "value": line.content_text,
                                        "format": "text/plain",
                                    },
                                    "confidence": line.line_confidence,
                                }
                            )

                    # Also gather matching TOC, Figures, and Cross-Volume Artifacts for unified search view
                    toc_hits = [
                        {k: v for k, v in t.items() if not k.startswith("_")}
                        for t in idx["toc"]
                        if (not mid or mid == "all" or t["manifest_id"] == mid)
                        and _matches(t["_norm"], tokens)
                    ][:40]
                    fig_hits = [
                        {
                            k: v
                            for k, v in f.items()
                            if not k.startswith("_") and k != "sub_figures"
                        }
                        for f in idx["figures"]
                        if (not mid or mid == "all" or f["manifest_id"] == mid)
                        and _matches(f["_norm"], tokens)
                    ][:30]
                    art_hits = [
                        {k: v for k, v in a.items() if not k.startswith("_")}
                        for a in idx["cross_artifacts"]
                        if (not mid or mid == "all" or mid in a["manifest_ids"])
                        and _matches(a["_norm"], tokens)
                    ][:30]

                    return self.send(
                        {
                            "@context": "http://iiif.io/api/presentation/3/context.json",
                            "id": f"{base_url}/manifest/{mid or 'all'}/search?q={term}",
                            "type": "AnnotationPage",
                            "query": term,
                            "total": len(all_rows),
                            "volume_counts": dict(vol_counts),
                            "items": hits,
                            "toc_hits": toc_hits,
                            "figure_hits": fig_hits,
                            "artifact_hits": art_hits,
                        }
                    )

                if path == "/api/figure_image":
                    fid = query.get("id", [""])[0]
                    custom_xywh = query.get("xywh", [""])[0].strip()
                    pf = session.get(PlateFigure, fid)
                    if not pf:
                        raise LookupError()
                    canvas = session.get(Canvas, pf.canvas_id)
                    if not canvas or not canvas.image_storage_path:
                        raise LookupError()

                    # If no custom xywh override, check pre-cropped ImageAsset file on disk
                    if not custom_xywh:
                        occ = session.scalar(
                            select(ImageOccurrence).where(
                                ImageOccurrence.plate_figure_id == fid
                            )
                        )
                        if occ and occ.image_asset and occ.image_asset.uri:
                            p_asset = Path(occ.image_asset.uri)
                            if p_asset.exists() and p_asset.is_file():
                                return self.send(
                                    p_asset.read_bytes(), mime="image/jpeg"
                                )

                    img_path = Path(canvas.image_storage_path).resolve()
                    if not img_path.is_relative_to(library) or not img_path.exists():
                        raise LookupError()
                    from PIL import Image

                    with Image.open(img_path) as source:
                        img = source.convert("RGB")
                        cw, ch = canvas.width, canvas.height
                        if img.width != cw or img.height != ch:
                            img = img.resize((cw, ch))

                        if custom_xywh and re.fullmatch(
                            r"\d+,\d+,\d+,\d+", custom_xywh
                        ):
                            x, y, w, h = map(int, custom_xywh.split(","))
                        elif pf.crop_w and pf.crop_h and pf.crop_w > 100 and pf.crop_h > 80:
                            x, y, w, h = pf.crop_x, pf.crop_y, pf.crop_w, pf.crop_h
                        else:
                            # Plate page fallback: crop main print space instead of tiny 32px header
                            x = int(cw * 0.10)
                            y = int(ch * 0.09)
                            w = int(cw * 0.80)
                            h = int(ch * 0.83)

                        x = max(0, min(x, cw - 1))
                        y = max(0, min(y, ch - 1))
                        w = max(1, min(w, cw - x))
                        h = max(1, min(h, ch - y))
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
                    r"/iiif/manifest/([^/]+)/canvas/([^/]+)(/annotations/ocr.json)?",
                    path,
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
                    r"/iiif/image/([^/]+)/(\d+)/(info.json|[^/]+/[^/]+/0/default.jpg)",
                    path,
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
                                "sizes": [
                                    {"width": canvas.width, "height": canvas.height}
                                ],
                            }
                        )
                    image_path = Path(canvas.image_storage_path).resolve()
                    if not image_path.is_relative_to(library):
                        raise LookupError()
                    from PIL import Image

                    with Image.open(image_path) as source:
                        image = source.convert("RGB").resize(
                            (canvas.width, canvas.height)
                        )
                    region, size, _, _ = match[3].split("/")
                    if size != "max":
                        raise ValueError("Only max size is supported")
                    if region != "full":
                        if not re.fullmatch(r"\d+,\d+,\d+,\d+", region):
                            raise ValueError("Invalid region")
                        x, y, w, h = map(int, region.split(","))
                        if (
                            w < 1
                            or h < 1
                            or x + w > canvas.width
                            or y + h > canvas.height
                        ):
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
                invalidate_index()
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
