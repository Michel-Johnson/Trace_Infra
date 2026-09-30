"""Shared pure validation helpers for draft and stable extension contracts."""

def unique(values, message):
    if len(values) != len(set(values)):
        raise ValueError(message)


def check_local_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('$ref', '$dynamicRef') and (not isinstance(child, str) or not child.startswith('#')):
                raise ValueError('配置 Schema 只能使用本地引用')
            check_local_refs(child)
    elif isinstance(value, list):
        for child in value:
            check_local_refs(child)


def entity_key(ref):
    return (ref['document_id'], ref['document_digest'], ref['entity_kind'], ref['entity_id'])
