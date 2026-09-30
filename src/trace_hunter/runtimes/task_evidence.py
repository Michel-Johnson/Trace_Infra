"""Bounded task input reads, with verified source bytes as the authority."""
import base64
import json
from ..artifacts import Artifacts
from ..catalog import canonical
from ..content import ContentCorruption, ContentLimitExceeded, ContentRef
from ..resources import resource_ref
from ..selections import SelectionSnapshots
from ..traces.service import TraceRevisions, MAX_REVISION

MAX_CHUNK_BYTES = 256 * 1024
MAX_RECORD_PAGE_BYTES = 256 * 1024
MAX_JSON_READ_BYTES = 64 * 1024 * 1024


def page_range(limit, after):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be 1 to 100')
    if after is not None and (type(after) is not int or not 0 <= after < MAX_REVISION):
        raise ValueError('after must be a non-negative source position')
    return 0 if after is None else after + 1


class TaskEvidence:
    def __init__(self, access, content):
        self.access = access
        self.content = content
        self.revisions = TraceRevisions(access.repository, content)
        self.artifacts = Artifacts(access.repository, content)
        self.selections = SelectionSnapshots(access.repository, content)

    def _resolve(self, project, ref):
        if ref['kind'] == 'trace_revision':
            descriptor = self.revisions.get(project, ref['id'], ref['revision'])
            digest = descriptor['content']['digest']
        elif ref['kind'] == 'artifact':
            descriptor = self.artifacts.get(project, ref['id'])
            digest = descriptor['descriptor_digest']
        else:
            descriptor = self.selections.get(project, ref['id'])
            digest = descriptor['manifest']['digest']
        if digest != ref['digest']:
            raise ContentCorruption('Task input does not match its fixed reference')
        return descriptor

    def _json(self, content):
        ref = ContentRef(**content)
        if ref.size_bytes > MAX_JSON_READ_BYTES:
            raise ContentLimitExceeded('Structured input exceeds 64 MiB; use content chunks')
        try:
            with self.content.open_verified(ref) as stream:
                return json.load(stream)
        except (ValueError, UnicodeError, RecursionError):
            raise ContentCorruption('Input is not a valid JSON document') from None

    def _manifest(self, project, descriptor):
        manifest = self._json(descriptor['manifest'])
        try:
            if (manifest['schema_version'] != 'trace-hunter/selection-manifest/1' or manifest['project_id'] != project
                    or manifest['selection_id'] != descriptor['selection_id'] or manifest['query_digest'] != descriptor['query_digest']
                    or len(manifest['members']) != descriptor['member_count']):
                raise ValueError()
            return manifest['members']
        except (TypeError, KeyError, ValueError):
            raise ContentCorruption('Selection manifest does not match the fixed task input') from None

    @staticmethod
    def _member(members, position):
        if type(position) is not int or not 0 <= position < len(members):
            raise KeyError('Selection member position not found')
        row = members[position]
        try:
            if type(row['position']) is not int or row['position'] != position:
                raise ValueError()
            return resource_ref({'kind': 'trace_revision', 'id': row['run_id'], 'revision': row['revision'], 'digest': row['content_digest']})
        except (TypeError, KeyError, ValueError):
            raise ContentCorruption('Invalid member in the fixed manifest') from None

    def _input(self, principal, position, member_position=None):
        session = self.access.session(principal)
        bindings = session['invocation']['inputs']
        if type(position) is not int or not 0 <= position < len(bindings):
            raise KeyError('Task input position not found')
        ref = bindings[position]['ref']
        project = session['invocation']['project_id']
        descriptor = self._resolve(project, ref)
        if member_position is not None:
            if ref['kind'] != 'selection_snapshot':
                raise ValueError('member_position is only valid for a selection input')
            ref = self._member(self._manifest(project, descriptor), member_position)
            descriptor = self._resolve(project, ref)
        return project, ref, descriptor

    def descriptor(self, principal, position, *, member_position=None):
        _, ref, descriptor = self._input(principal, position, member_position)
        return {'ref': ref, 'descriptor': descriptor}

    def members(self, principal, position, *, limit=50, after=None):
        start = page_range(limit, after)
        project, ref, descriptor = self._input(principal, position)
        if ref['kind'] != 'selection_snapshot':
            raise ValueError('Only selection inputs have members')
        members = self._manifest(project, descriptor)
        items = [{'position': index, 'ref': self._member(members, index)} for index in range(start, min(start+limit, len(members)))]
        return {'ref': ref, 'member_count': len(members), 'items': items,
                'next_after': items[-1]['position'] if items and start+len(items) < len(members) else None,
                'authority': 'verified_manifest'}

    def chunk(self, principal, position, *, member_position=None, offset=0, limit=65536):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_CHUNK_BYTES:
            raise ValueError('offset must be non-negative and limit must be 1 to 262144 bytes')
        _, ref, descriptor = self._input(principal, position, member_position)
        content = descriptor['manifest'] if ref['kind'] == 'selection_snapshot' else descriptor['content']
        if offset > content['size_bytes']:
            raise ValueError('offset is past the end of the content')
        with self.content.open_verified(ContentRef(**content)) as stream:
            stream.seek(offset)
            raw = stream.read(limit)
        return {'ref': ref, 'content': content, 'offset': offset, 'length': len(raw),
                'data_base64': base64.b64encode(raw).decode('ascii'),
                'next_offset': offset+len(raw) if offset+len(raw) < content['size_bytes'] else None}

    def records(self, principal, position, *, member_position=None, limit=50, after=None):
        start = page_range(limit, after)
        _, ref, descriptor = self._input(principal, position, member_position)
        if ref['kind'] != 'trace_revision':
            raise ValueError('Record paging requires a trace input or a selection member')
        document = self._json(descriptor['content'])
        if not isinstance(document, dict) or not isinstance(document.get('spans'), list):
            raise ContentCorruption('Trace source has no spans array')
        spans = document['spans']
        items, size = [], 2
        for index in range(start, min(start+limit, len(spans))):
            item = {'position': index, 'delivery': 'inline', 'value': spans[index]}
            cost = len(canonical(item)[0].encode()) + 1
            if cost > MAX_RECORD_PAGE_BYTES - 2:
                item = {'position': index, 'delivery': 'content_only', 'value': None}
                cost = len(canonical(item)[0].encode()) + 1
            if size + cost > MAX_RECORD_PAGE_BYTES:
                break
            items.append(item)
            size += cost
        return {'ref': ref, 'array_path': '/spans', 'record_count': len(spans), 'items': items,
                'next_after': items[-1]['position'] if items and start+len(items) < len(spans) else None,
                'order': 'source_array', 'max_items_bytes': MAX_RECORD_PAGE_BYTES}
