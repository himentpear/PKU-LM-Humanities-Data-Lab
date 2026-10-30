"""Dynamic Programming (Viterbi) Page Sequence Alignment & Multi-Domain Page Modeling.

Solves:
1. Multi-domain page numbering (cover, Roman front_matter, toc, Arabic body, foldout insert, plate)
2. Missing page gap detection & duplicate scan detection via global sequence DP
3. Insert/foldout handling (remembers last active body ordinal across unnumbered inserts)
4. Domain-safe continuous page range merging for archaeological indexes
5. Cross-page running header negative rule filtering
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AltoBlock, Canvas, ReviewTask, Revision

# Roman numerals (ASCII and Unicode full-width)
ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
UNICODE_ROMAN_MAP = {
    "Ⅰ": "I",
    "Ⅱ": "II",
    "Ⅲ": "III",
    "Ⅳ": "IV",
    "Ⅴ": "V",
    "Ⅵ": "VI",
    "Ⅶ": "VII",
    "Ⅷ": "VIII",
    "Ⅸ": "IX",
    "Ⅹ": "X",
    "Ⅺ": "XI",
    "Ⅻ": "XII",
}

CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
CHINESE_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100}

DOMAIN_ORDER = {
    "cover": 0,
    "front_matter": 1,
    "toc": 2,
    "body": 3,
    "insert": 3,  # Can interleave inside body
    "appendix": 4,
    "plate": 5,
}


@dataclass(frozen=True)
class ParsedPageLabel:
    """Structured representation of a printed page label."""

    page_domain: str
    page_ordinal: Optional[int]
    original_page_label: str


@dataclass
class PageObservation:
    """Multi-modal evidence collected for a single physical scan page."""

    scan_index: int  # 1-based physical scan index
    pdf_page_index: Optional[int] = None  # 0-based PDF index
    source_page_label: Optional[str] = None
    ocr_page_label_raw: Optional[str] = None
    ocr_confidence: float = 0.0
    # Optional anchor from TOC or body text matching: (domain, ordinal, score)
    text_anchor: Optional[tuple[str, int, float]] = None
    layout_hint: Optional[str] = None  # 'cover' | 'toc' | 'body' | 'plate' | 'insert'
    visual_similarity_to_prev: float = 0.0  # High (>=0.92) suggests duplicate scan
    is_foldout: bool = False


@dataclass
class AlignmentAnomaly:
    """Anomaly detected during global DP page alignment (queued for human review)."""

    scan_index: int
    risk_type: str  # 'missing_page_gap' | 'duplicate_scan' | 'low_mapping_confidence' | 'label_conflict'
    priority_score: float
    description: str
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AlignedPageResult:
    """Resolved multi-domain identity and provenance signals for one scan page."""

    scan_index: int
    pdf_page_index: Optional[int]
    source_page_label: Optional[str]
    page_domain: str
    page_ordinal: Optional[int]
    original_page_label: str
    mapping_confidence: float
    mapping_status: str  # 'auto_accepted' | 'needs_review'
    signals: dict[str, float]
    candidates: list[dict[str, Any]]


def _roman_to_int(s: str) -> Optional[int]:
    for uni, asc in UNICODE_ROMAN_MAP.items():
        s = s.replace(uni, asc)
    s = s.upper().strip()
    if not s or not all(ch in ROMAN_VALUES for ch in s):
        return None
    total = 0
    prev = 0
    for ch in reversed(s):
        val = ROMAN_VALUES[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total if 1 <= total <= 200 else None


def _chinese_to_int(s: str) -> Optional[int]:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    # Pure digit sequence like "一〇八"
    if all(ch in CHINESE_DIGITS for ch in s):
        val = 0
        for ch in s:
            val = val * 10 + CHINESE_DIGITS[ch]
        return val if val > 0 else None

    total = 0
    current = 0
    for ch in s:
        if ch in CHINESE_DIGITS:
            current = CHINESE_DIGITS[ch]
        elif ch in CHINESE_UNITS:
            unit = CHINESE_UNITS[ch]
            total += (current if current > 0 else 1) * unit
            current = 0
        else:
            return None
    total += current
    return total if total > 0 else None


def _int_to_chinese_plate(n: int, prefix: str = "图版") -> str:
    """Format plate ordinal into standard Chinese label or Arabic label."""
    cn_map = {
        1: "一", 2: "二", 3: "三", 4: "四", 5: "五",
        6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
    }
    if n in cn_map:
        return f"{prefix}{cn_map[n]}"
    if 11 <= n <= 19:
        return f"{prefix}十{cn_map[n - 10]}"
    if 20 <= n <= 99:
        tens, rem = divmod(n, 10)
        return f"{prefix}{cn_map[tens]}十{cn_map.get(rem, '')}"
    return f"{prefix}{n}"


def parse_printed_page_label(
    raw_text: Optional[str], default_domain: str = "body"
) -> Optional[ParsedPageLabel]:
    """Parse raw OCR/PDF page number strings into (page_domain, page_ordinal, original_page_label)."""
    if not raw_text:
        return None
    cleaned = re.sub(r"^[\s\-—·\[\]【】()（）第页]+|[\s\-—·\[\]【】()（）第页]+$", "", raw_text.strip())
    if not cleaned:
        return None

    # 0. Inline running-header page numbers like "· 357 ·第二部分·第二章..." or "· 354 ·"
    m_inline = re.match(r"^[·•\-\s]*(\d{1,4})\s*[·•\-]+", cleaned)
    if m_inline:
        val = int(m_inline.group(1))
        return ParsedPageLabel(
            page_domain=default_domain,
            page_ordinal=val,
            original_page_label=str(val),
        )

    # 1. Plate patterns: 图版八, 彩版叁, 图版 12, 图版 2.1.1
    m_plate = re.match(
        r"^(彩版|图版)\s*([一二三四五六七八九十百零〇壹贰叁肆伍陆柒捌玖拾0-9]+(?:\.[0-9]+)*)$",
        cleaned,
    )
    if m_plate:
        prefix, num_str = m_plate.group(1), m_plate.group(2)
        ord_val = _chinese_to_int(num_str.split(".")[-1]) if "." in num_str else _chinese_to_int(num_str)
        return ParsedPageLabel(
            page_domain="plate",
            page_ordinal=ord_val,
            original_page_label=f"{prefix}{num_str}",
        )

    # 2. Appendix patterns: 附表一, 附录二
    m_app = re.match(r"^(附表|附录)\s*([一二三四五六七八九十百零〇0-9]+)$", cleaned)
    if m_app:
        prefix, num_str = m_app.group(1), m_app.group(2)
        ord_val = _chinese_to_int(num_str)
        return ParsedPageLabel(
            page_domain="appendix",
            page_ordinal=ord_val,
            original_page_label=f"{prefix}{num_str}",
        )

    # 3. Insert / Foldout patterns
    if cleaned in ("插页", "折页", "附图"):
        return ParsedPageLabel(page_domain="insert", page_ordinal=None, original_page_label=cleaned)

    # 4. Pure Arabic numeral
    if cleaned.isdigit():
        val = int(cleaned)
        if 1 <= val <= 5000:
            return ParsedPageLabel(
                page_domain=default_domain,
                page_ordinal=val,
                original_page_label=str(val),
            )

    # 5. Roman numeral (front matter / preface / TOC)
    roman_val = _roman_to_int(cleaned)
    if roman_val is not None:
        return ParsedPageLabel(
            page_domain="toc" if default_domain == "toc" else "front_matter",
            page_ordinal=roman_val,
            original_page_label=cleaned.upper(),
        )

    return None


class PageSequenceAligner:
    """Global Dynamic Programming (Viterbi) aligner for multi-domain archaeological report pages."""

    def __init__(
        self,
        w_page_no: float = 0.45,
        w_text: float = 0.25,
        w_layout: float = 0.15,
        w_visual: float = 0.05,
        w_meta: float = 0.10,
        auto_accept_threshold: float = 0.80,
    ) -> None:
        self.w_page_no = w_page_no
        self.w_text = w_text
        self.w_layout = w_layout
        self.w_visual = w_visual
        self.w_meta = w_meta
        self.auto_accept_threshold = auto_accept_threshold

    def align_sequence(
        self, observations: list[PageObservation]
    ) -> tuple[list[AlignedPageResult], list[AlignmentAnomaly]]:
        """Run Viterbi sequence alignment over physical scan observations."""
        if not observations:
            return [], []

        n_pages = len(observations)

        # Build candidate states (domain, ordinal, label_hint) for each scan position i
        candidates_per_pos: list[list[ParsedPageLabel]] = []
        for idx, obs in enumerate(observations):
            cands = self._generate_candidates_for_page(idx, obs, observations)
            candidates_per_pos.append(cands)

        # DP tables: dp_score[i][c_idx], backptr[i][c_idx], last_body_ord[i][c_idx]
        dp_scores: list[list[float]] = []
        backptrs: list[list[int]] = []
        active_body_ords: list[list[Optional[int]]] = []

        # Initialize i = 0
        first_scores: list[float] = []
        first_body_ords: list[Optional[int]] = []
        for cand in candidates_per_pos[0]:
            u_score, _ = self._unary_score(observations[0], cand)
            first_scores.append(u_score)
            first_body_ords.append(cand.page_ordinal if cand.page_domain == "body" else None)

        dp_scores.append(first_scores)
        backptrs.append([-1] * len(candidates_per_pos[0]))
        active_body_ords.append(first_body_ords)

        # Forward Viterbi pass
        for i in range(1, n_pages):
            obs = observations[i]
            prev_cands = candidates_per_pos[i - 1]
            curr_cands = candidates_per_pos[i]

            curr_scores: list[float] = []
            curr_ptrs: list[int] = []
            curr_body_ords: list[Optional[int]] = []

            for c_idx, curr_cand in enumerate(curr_cands):
                u_score, _ = self._unary_score(obs, curr_cand)
                best_val = -1e9
                best_prev = 0
                best_body_ord: Optional[int] = None

                for p_idx, prev_cand in enumerate(prev_cands):
                    prev_body_ord = active_body_ords[i - 1][p_idx]
                    penalty = self._transition_penalty(
                        prev_cand, curr_cand, prev_body_ord, obs
                    )
                    candidate_total = dp_scores[i - 1][p_idx] + u_score - penalty
                    if candidate_total > best_val:
                        best_val = candidate_total
                        best_prev = p_idx
                        if curr_cand.page_domain == "body":
                            best_body_ord = curr_cand.page_ordinal
                        else:
                            best_body_ord = prev_body_ord

                curr_scores.append(best_val)
                curr_ptrs.append(best_prev)
                curr_body_ords.append(best_body_ord)

            dp_scores.append(curr_scores)
            backptrs.append(curr_ptrs)
            active_body_ords.append(curr_body_ords)

        # Backtrack optimal path
        last_scores = dp_scores[-1]
        best_last_idx = max(range(len(last_scores)), key=lambda k: last_scores[k])
        chosen_indices = [0] * n_pages
        chosen_indices[-1] = best_last_idx
        for i in range(n_pages - 1, 0, -1):
            chosen_indices[i - 1] = backptrs[i][chosen_indices[i]]

        # Format results & detect sequence anomalies
        results: list[AlignedPageResult] = []
        anomalies: list[AlignmentAnomaly] = []
        last_resolved_by_domain: dict[str, int] = {}

        for i, obs in enumerate(observations):
            chosen = candidates_per_pos[i][chosen_indices[i]]
            u_score, sig_breakdown = self._unary_score(obs, chosen)

            # Collect all competing candidates for transparency
            competing: list[dict[str, Any]] = []
            for cand in candidates_per_pos[i]:
                sc, _ = self._unary_score(obs, cand)
                competing.append(
                    {
                        "page_domain": cand.page_domain,
                        "page_ordinal": cand.page_ordinal,
                        "original_page_label": cand.original_page_label,
                        "score": round(sc, 4),
                    }
                )
            competing.sort(key=lambda x: x["score"], reverse=True)

            # Sequence consistency bonus when matching expected increment
            prev_ord_in_dom = last_resolved_by_domain.get(chosen.page_domain)
            seq_consistency = 1.0
            if prev_ord_in_dom is not None and chosen.page_ordinal is not None:
                delta = chosen.page_ordinal - prev_ord_in_dom
                if delta == 1:
                    seq_consistency = 1.0
                elif delta == 0:
                    seq_consistency = 0.45
                    anomalies.append(
                        AlignmentAnomaly(
                            scan_index=obs.scan_index,
                            risk_type="duplicate_scan" if obs.visual_similarity_to_prev >= 0.92 else "label_conflict",
                            priority_score=0.88,
                            description=(
                                f"扫描页 #{obs.scan_index} 与前一页同属 '{chosen.original_page_label}'，"
                                "疑似重复扫描页。"
                            ),
                            candidates=competing[:3],
                        )
                    )
                elif delta > 1:
                    seq_consistency = max(0.2, 1.0 - 0.15 * delta)
                    missing_labels = [
                        str(p) for p in range(prev_ord_in_dom + 1, chosen.page_ordinal)
                    ]
                    anomalies.append(
                        AlignmentAnomaly(
                            scan_index=obs.scan_index,
                            risk_type="missing_page_gap",
                            priority_score=0.95,
                            description=(
                                f"扫描页 #{obs.scan_index} 页码从 {prev_ord_in_dom} 跳至 "
                                f"{chosen.page_ordinal}，疑似漏扫原始页码: {', '.join(missing_labels)}。"
                            ),
                            candidates=competing[:3],
                        )
                    )

            if chosen.page_ordinal is not None:
                last_resolved_by_domain[chosen.page_domain] = chosen.page_ordinal

            sig_breakdown["sequence_consistency"] = round(seq_consistency, 4)
            final_conf = round(min(1.0, max(0.1, 0.65 * u_score + 0.35 * seq_consistency)), 4)
            status = (
                "auto_accepted"
                if final_conf >= self.auto_accept_threshold and seq_consistency >= 0.8
                else "needs_review"
            )

            if status == "needs_review" and seq_consistency >= 0.8:
                anomalies.append(
                    AlignmentAnomaly(
                        scan_index=obs.scan_index,
                        risk_type="low_mapping_confidence",
                        priority_score=round(1.0 - final_conf, 4),
                        description=(
                            f"扫描页 #{obs.scan_index} 推断页码 '{chosen.original_page_label}' "
                            f"综合置信度较低 ({final_conf})，需人工复核。"
                        ),
                        candidates=competing[:3],
                    )
                )

            results.append(
                AlignedPageResult(
                    scan_index=obs.scan_index,
                    pdf_page_index=obs.pdf_page_index,
                    source_page_label=obs.source_page_label,
                    page_domain=chosen.page_domain,
                    page_ordinal=chosen.page_ordinal,
                    original_page_label=chosen.original_page_label,
                    mapping_confidence=final_conf,
                    mapping_status=status,
                    signals=sig_breakdown,
                    candidates=competing[:4],
                )
            )

        return results, anomalies

    def _generate_candidates_for_page(
        self, idx: int, obs: PageObservation, all_obs: list[PageObservation]
    ) -> list[ParsedPageLabel]:
        cands: dict[tuple[str, Optional[int], str], ParsedPageLabel] = {}

        def add_cand(p: ParsedPageLabel) -> None:
            cands[(p.page_domain, p.page_ordinal, p.original_page_label)] = p

        # 1. From OCR page number
        parsed_ocr = parse_printed_page_label(
            obs.ocr_page_label_raw, default_domain=obs.layout_hint or "body"
        )
        if parsed_ocr:
            add_cand(parsed_ocr)

        # 2. From PDF source label (only when PDF defines a non-trivial label != scan_index)
        if obs.source_page_label and obs.source_page_label != str(obs.scan_index):
            parsed_src = parse_printed_page_label(
                obs.source_page_label, default_domain=obs.layout_hint or "body"
            )
            if parsed_src:
                add_cand(parsed_src)

        # 3. From text anchor (e.g., TOC or manual text alignment)
        if obs.text_anchor:
            dom, ord_val, _ = obs.text_anchor
            lbl = _int_to_chinese_plate(ord_val) if dom == "plate" else str(ord_val)
            add_cand(ParsedPageLabel(dom, ord_val, lbl))

        # 4. Foldout / Insert candidate
        if obs.is_foldout or obs.layout_hint == "insert":
            add_cand(ParsedPageLabel("insert", None, "插页"))

        # 5. Cover / Front-matter / TOC opening candidates for unnumbered pages
        if idx == 0 or obs.layout_hint == "cover":
            add_cand(ParsedPageLabel("cover", 1, "封面"))
        elif obs.layout_hint == "front_matter" and not obs.ocr_page_label_raw:
            add_cand(ParsedPageLabel("front_matter", idx, f"扉页-{idx}"))
        elif obs.layout_hint == "toc" and not obs.ocr_page_label_raw:
            add_cand(ParsedPageLabel("toc", None, "未识别页码"))

        # 6. Neighborhood extrapolated candidates within the same domain
        for offset in range(-3, 4):
            if offset == 0:
                continue
            neighbor_idx = idx + offset
            if 0 <= neighbor_idx < len(all_obs):
                n_obs = all_obs[neighbor_idx]
                if obs.layout_hint and n_obs.layout_hint and obs.layout_hint != n_obs.layout_hint:
                    continue
                n_parsed = parse_printed_page_label(
                    n_obs.ocr_page_label_raw, default_domain=n_obs.layout_hint or "body"
                )
                if (
                    parsed_ocr
                    and n_parsed
                    and parsed_ocr.page_domain != n_parsed.page_domain
                    and obs.ocr_confidence >= 0.85
                ):
                    continue
                if n_parsed and n_parsed.page_ordinal is not None:
                    for shift in (-offset, -offset - 1, -offset + 1):
                        inferred_ord = n_parsed.page_ordinal + shift
                        if inferred_ord >= 1:
                            lbl = (
                                _int_to_chinese_plate(inferred_ord)
                                if n_parsed.page_domain == "plate"
                                else str(inferred_ord)
                            )
                            add_cand(ParsedPageLabel(n_parsed.page_domain, inferred_ord, lbl))

        if not cands:
            dom = obs.layout_hint or "body"
            add_cand(ParsedPageLabel(dom, idx + 1, str(idx + 1)))

        return list(cands.values())

    def _unary_score(
        self, obs: PageObservation, cand: ParsedPageLabel
    ) -> tuple[float, dict[str, float]]:
        # P: Printed page number OCR match
        p_score = 0.1
        parsed_ocr = parse_printed_page_label(
            obs.ocr_page_label_raw, default_domain=obs.layout_hint or "body"
        )
        if parsed_ocr:
            if (
                parsed_ocr.page_domain == cand.page_domain
                and parsed_ocr.page_ordinal == cand.page_ordinal
            ):
                p_score = max(0.5, obs.ocr_confidence)
            else:
                p_score = 0.05
        elif cand.page_domain in ("insert", "cover") and not obs.ocr_page_label_raw:
            p_score = 0.85

        # T: Text anchor similarity
        t_score = 0.5
        if obs.text_anchor:
            t_dom, t_ord, t_conf = obs.text_anchor
            if cand.page_domain == t_dom and cand.page_ordinal == t_ord:
                t_score = t_conf
            else:
                t_score = 0.1

        # L: Layout hint match
        l_score = 0.6
        if obs.layout_hint:
            l_score = 0.95 if obs.layout_hint == cand.page_domain else 0.15
        elif obs.is_foldout:
            l_score = 0.98 if cand.page_domain == "insert" else 0.10

        # V: Visual similarity cue
        v_score = 0.8

        # M: PDF metadata / PageLabel match
        m_score = 0.5
        parsed_src = parse_printed_page_label(obs.source_page_label)
        if parsed_src and parsed_src == cand:
            m_score = 0.85

        total = (
            self.w_page_no * p_score
            + self.w_text * t_score
            + self.w_layout * l_score
            + self.w_visual * v_score
            + self.w_meta * m_score
        )
        signals = {
            "printed_page_no": round(p_score, 4),
            "text_similarity": round(t_score, 4),
            "layout_similarity": round(l_score, 4),
            "visual_similarity": round(v_score, 4),
            "metadata_score": round(m_score, 4),
        }
        return total, signals

    @staticmethod
    def _transition_penalty(
        prev_cand: ParsedPageLabel,
        curr_cand: ParsedPageLabel,
        prev_active_body_ord: Optional[int],
        curr_obs: PageObservation,
    ) -> float:
        # Case 1: Transition into an unnumbered insert/foldout inside body
        if curr_cand.page_domain == "insert":
            return 0.05 if (curr_obs.is_foldout or curr_obs.layout_hint == "insert") else 0.45

        # Case 2: Resuming body immediately after an insert page
        if prev_cand.page_domain == "insert" and curr_cand.page_domain == "body":
            if prev_active_body_ord is not None and curr_cand.page_ordinal is not None:
                delta = curr_cand.page_ordinal - prev_active_body_ord
                if delta == 1:
                    return 0.0
                if delta > 1:
                    return 0.25 + 0.12 * (delta - 1)
            return 0.5

        # Case 3: Same domain transition
        if prev_cand.page_domain == curr_cand.page_domain:
            if prev_cand.page_ordinal is None or curr_cand.page_ordinal is None:
                return 0.1
            delta = curr_cand.page_ordinal - prev_cand.page_ordinal
            if delta == 1:
                return 0.0  # Ideal consecutive page
            if delta == 0:
                # Duplicate scan allowed if high visual similarity or same OCR number
                return 0.12 if curr_obs.visual_similarity_to_prev >= 0.85 else 0.28
            if 1 < delta <= 8:
                # Missing scan pages gap penalty (small enough so strong OCR overrides broken sequence)
                return 0.22 + 0.08 * (delta - 1)
            return 3.5  # Backward jump or huge jump within same domain strongly penalized

        # Case 4: Domain switch (e.g. front_matter -> body, body -> plate)
        prev_rank = DOMAIN_ORDER.get(prev_cand.page_domain, 3)
        curr_rank = DOMAIN_ORDER.get(curr_cand.page_domain, 3)
        if curr_rank > prev_rank:
            # New domain should normally start at ordinal 1
            if curr_cand.page_ordinal in (1, None):
                return 0.08
            return 0.65
        return 2.5  # Backward domain switch penalized

    def apply_alignment_to_db(
        self,
        session: Session,
        manifest_id: str,
        results: list[AlignedPageResult],
        anomalies: list[AlignmentAnomaly],
    ) -> int:
        """Persist aligned page domains, labels, confidence scores, and review tasks into DB."""
        canvases_by_scan = {
            c.scan_index: c
            for c in session.scalars(select(Canvas).where(Canvas.manifest_id == manifest_id))
        }
        updated = 0
        for res in results:
            canvas = canvases_by_scan.get(res.scan_index)
            if canvas is None:
                continue
            before_snapshot = {
                "printed_page_label": canvas.printed_page_label,
                "page_domain": canvas.page_domain,
                "page_ordinal": canvas.page_ordinal,
                "mapping_status": canvas.mapping_status,
            }
            canvas.printed_page_label = res.original_page_label
            canvas.page_domain = res.page_domain
            canvas.page_ordinal = res.page_ordinal
            canvas.mapping_confidence = res.mapping_confidence
            canvas.mapping_status = res.mapping_status
            canvas.alignment_signals = res.signals
            canvas.field_candidates = res.candidates
            canvas.revision_no += 1

            session.add(
                Revision(
                    entity_type="canvas",
                    entity_id=canvas.id,
                    revision_no=canvas.revision_no,
                    actor="sequence_dp_v2",
                    reason="global_viterbi_page_alignment",
                    before_state=before_snapshot,
                    after_state={
                        "printed_page_label": canvas.printed_page_label,
                        "page_domain": canvas.page_domain,
                        "page_ordinal": canvas.page_ordinal,
                        "mapping_confidence": canvas.mapping_confidence,
                    },
                )
            )
            updated += 1

        for anom in anomalies:
            canvas = canvases_by_scan.get(anom.scan_index)
            session.add(
                ReviewTask(
                    manifest_id=manifest_id,
                    canvas_id=canvas.id if canvas else None,
                    target_type="page_mapping",
                    target_id=canvas.id if canvas else None,
                    risk_type=anom.risk_type,
                    priority_score=anom.priority_score,
                    status="pending",
                    confidence_before=canvas.mapping_confidence if canvas else None,
                    description=anom.description,
                    candidates_snapshot=anom.candidates,
                )
            )

        session.flush()
        return updated


def merge_domain_page_ranges(
    occurrences: list[tuple[str, Optional[int], str]],
) -> list[dict[str, Any]]:
    """Merge continuous page numbers strictly within the same page_domain.

    Args:
        occurrences: List of (page_domain, page_ordinal, original_page_label) tuples.

    Returns:
        List of domain-aware merged page ranges, never merging across domains
        (e.g. body 39, plate 图版一, body 40 will NOT swallow 图版一).
    """
    if not occurrences:
        return []

    # Group by domain in canonical domain order
    by_domain: dict[str, dict[Optional[int], str]] = {}
    unnumbered_labels: dict[str, list[str]] = {}

    for dom, ord_val, label in occurrences:
        if ord_val is None:
            unnumbered_labels.setdefault(dom, [])
            if label not in unnumbered_labels[dom]:
                unnumbered_labels[dom].append(label)
        else:
            by_domain.setdefault(dom, {})[ord_val] = label

    merged_output: list[dict[str, Any]] = []
    sorted_domains = sorted(
        set(list(by_domain.keys()) + list(unnumbered_labels.keys())),
        key=lambda d: DOMAIN_ORDER.get(d, 99),
    )

    for dom in sorted_domains:
        ord_map = by_domain.get(dom, {})
        if ord_map:
            sorted_ords = sorted(ord_map.keys())
            start_ord = prev_ord = sorted_ords[0]

            def format_span(s: int, e: int) -> str:
                if s == e:
                    return ord_map[s]
                if dom == "body":
                    return f"{ord_map[s]}–{ord_map[e]}"
                return f"{ord_map[s]}至{ord_map[e]}"

            for curr_ord in sorted_ords[1:]:
                if curr_ord == prev_ord + 1:
                    prev_ord = curr_ord
                else:
                    merged_output.append(
                        {
                            "page_domain": dom,
                            "start_ordinal": start_ord,
                            "end_ordinal": prev_ord,
                            "display": format_span(start_ord, prev_ord),
                        }
                    )
                    start_ord = prev_ord = curr_ord

            merged_output.append(
                {
                    "page_domain": dom,
                    "start_ordinal": start_ord,
                    "end_ordinal": prev_ord,
                    "display": format_span(start_ord, prev_ord),
                }
            )

        for lbl in unnumbered_labels.get(dom, []):
            merged_output.append(
                {
                    "page_domain": dom,
                    "start_ordinal": None,
                    "end_ordinal": None,
                    "display": lbl,
                }
            )

    return merged_output


def detect_running_headers(
    blocks: list[AltoBlock],
    max_canonical_y: int = 1200,
    min_repeat_pages: int = 3,
    max_text_length: int = 40,
) -> list[AltoBlock]:
    """Identify cross-page repeating header blocks and reclassify them as 'running_header'
    in 'margin_top' so they never pollute the automatic Table of Contents.
    """
    candidates: list[tuple[AltoBlock, str]] = []
    for blk in blocks:
        text = (blk.resolve_display_text() or "").strip()
        if not text or len(text) > max_text_length:
            continue
        top_y = blk.canonical_polygon[0][1] if blk.canonical_polygon else blk.y
        if top_y <= max_canonical_y:
            norm_key = re.sub(r"[\s0-9一二三四五六七八九十·\-—]+", "", text)
            if len(norm_key) >= 2:
                candidates.append((blk, norm_key))

    page_sets_by_key: dict[str, set[str]] = {}
    for blk, key in candidates:
        page_sets_by_key.setdefault(key, set()).add(blk.canvas_id)

    repeating_keys = {
        k for k, canvas_ids in page_sets_by_key.items() if len(canvas_ids) >= min_repeat_pages
    }

    flagged: list[AltoBlock] = []
    for blk, key in candidates:
        if key in repeating_keys:
            blk.page_zone = "margin_top"
            blk.block_type = "running_header"
            flagged.append(blk)

    return flagged
