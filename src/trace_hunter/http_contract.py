"""Transport-neutral HTTP input rules shared by the API and legacy demo."""
import json

MAX_BYTES = 16 * 1024 * 1024

def phase_selection(query):
    selected = json.loads(query['phases'][0]) if 'phases' in query else None
    if selected is not None and (not isinstance(selected, list) or any(not isinstance(x, str) for x in selected)):
        raise ValueError('phases 必须是字符串数组')
    return selected
