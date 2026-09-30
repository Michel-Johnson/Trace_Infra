"""Read-only display labels; source query IDs remain the comparison identity."""
import re


def first_sentence(value):
    text = str(value or '').strip()
    boundary = re.search(r'[。！？!?]|\.(?=\s|$)|\r?\n', text)
    return text[:boundary.end()].strip() if boundary else text


def case_presentation(definition, runs, ordinal):
    description = str(definition.get('description') or '').strip()
    if not description:
        turns = definition.get('conversation', {}).get('turns', [])
        prompt = turns[0]['prompt'] if turns else next((r['query'] for r in runs if r.get('query')), '')
        description = first_sentence(prompt or definition.get('title'))
    description = re.sub(r'\s+', ' ', description).strip()
    # Collector provenance stays in the complete input, rather than every list subtitle.
    description = re.sub(r'^【来源摘要[^】]*】\s*', '', description)
    return {'display_id': f'category {ordinal:03d}',
            'display_description': description if len(description) <= 240 else description[:239] + '…'}
