"""主动上下文压缩、完整证据存储与来源引用。"""

from app.context.compaction import ActiveContextCompactor
from app.context.evidence_store import EvidenceStore
from app.context.sources import SourceReference, build_source_references

__all__ = ["ActiveContextCompactor", "EvidenceStore", "SourceReference", "build_source_references"]
