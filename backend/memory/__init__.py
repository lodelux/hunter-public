"""
Agent Memory System — per-website self-learning for job application agents.

Stores procedural memories about website navigation, form interactions,
and application strategies. Memories are keyed by website domain and
ATS platform for efficient retrieval.

Usage:
    from memory import MemoryStore, extract_end_of_run_memories

    store = MemoryStore()
    memories = store.get_domain_memories("https://www.linkedin.com/jobs/view/123")
    context = store.format_for_prompt(memories)
"""

from .store import MemoryStore, CATEGORIES, ATS_DOMAINS
from .extractors import (
    MemoryExtractionResult,
    extract_end_of_run_memories,
    store_extracted_memories,
)

__all__ = [
    "MemoryStore",
    "CATEGORIES",
    "ATS_DOMAINS",
    "MemoryExtractionResult",
    "extract_end_of_run_memories",
    "store_extracted_memories",
]
