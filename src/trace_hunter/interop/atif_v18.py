"""ATIF v1.8 adapter with explicit context-compaction and external-ref mapping."""
from .atif import convert_version
from .common import reject

VERSION = "1.0.0"


def convert(raw, source, binding):
    if source.get("schema_version") != "ATIF-v1.8":
        reject("SOURCE_VERSION_MISMATCH", "/schema_version", "该 Adapter 只接受 ATIF-v1.8。", "选择与来源声明完全一致的 Adapter。")
    return convert_version(raw, source, binding, "ATIF-v1.8", context_management=True, adapter_version=VERSION)
