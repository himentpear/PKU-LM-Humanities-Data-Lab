"""Multi-modal page sequence alignment (DP/Viterbi), running-header detection,
and domain-aware continuous page range merging.
"""

from src.alignment.sequence_dp import (
    AlignedPageResult,
    AlignmentAnomaly,
    PageObservation,
    PageSequenceAligner,
    ParsedPageLabel,
    detect_running_headers,
    merge_domain_page_ranges,
    parse_printed_page_label,
)

__all__ = [
    "ParsedPageLabel",
    "PageObservation",
    "AlignedPageResult",
    "AlignmentAnomaly",
    "PageSequenceAligner",
    "parse_printed_page_label",
    "merge_domain_page_ranges",
    "detect_running_headers",
]
