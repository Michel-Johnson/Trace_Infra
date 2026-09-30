"""User workflows over the existing platform; test fixtures never enter its core."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import time
import wave
import zlib

from fastapi.testclient import TestClient
from sqlalchemy import text
from trace_hunter.content import ContentRef, LocalContentStore
from trace_hunter.storage import Store
from trace_hunter.workers import OfficialWorker, WorkerRegistry
from trace_hunter_api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'examples/platform-acceptance'
PREFIX = '/api/v1/projects/acceptance-cases'
PROJECT = 'acceptance-cases'


def encoded(raw, media='application/json'):
    return {'media_type': media, 'encoding': 'base64', 'data': base64.b64encode(raw).decode()}


class WorkflowScenarios:
    def __init__(self, flow):
        self.f = flow
        self.template = json.loads((DATA / 'traces/case-001.json').read_text())
        registry = WorkerRegistry(DATA / 'runtime')
        self.test_worker = OfficialWorker(flow.store, registry, worker_id='scenario-test-worker', timeout_seconds=1)
        for installed in registry.installed:
            response = flow.operator.post(PREFIX + '/operations', json=installed.definition)
            assert response.status_code == 201, response.text
            flow.operations[installed.ref['operation_id']] = response.json()['operation']['ref']

    def seed(self, name, *, query=None, document=None, **run_fields):
        doc = copy.deepcopy(document or self.template)
        doc['run'].update(id=name, query_id=query or name, **run_fields)
        raw = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()
        accepted = self.f.import_trace(raw, name)
        self.f.check('seed_index_complete', accepted['index']['state'], 'complete')
        rev = accepted['revision']
        return doc, raw, {'kind': 'trace_revision', 'id': name, 'revision': rev['revision'], 'digest': rev['content']['digest']}

    def publish(self, name, raw, *, media='application/json', inputs=None, producer='acceptance', config=None, version='1.0.0'):
        body = {'content': encoded(raw, media), 'artifact_type': 'acceptance.material/1',
                'inputs': inputs or [], 'producer_claim': {'name': producer, 'version': version},
                'config': config or {}, 'metadata': {'synthetic': True}}
        artifact = self.f.request('material', 'POST', PREFIX + '/artifacts', 201, key=name, json=body).json()['artifact']
        returned = self.f.request('material', 'GET', PREFIX + '/artifacts/' + artifact['artifact_id'] + '/content')
        self.f.check('material_byte_roundtrip', returned.content == raw, True)
        self.f.check('material_type', returned.headers['content-type'], media)
        return artifact, body

    def submit(self, name, ref, *, operation='official.record-counts', inputs=None, config=None):
        body = {'operation': self.f.operations[operation],
                'inputs': inputs or [{'role': 'source', 'ref': ref}], 'config': config or {}}
        return self.f.request('submit', 'POST', PREFIX + '/invocations', 201, key=name, json=body).json()['invocation']

    def run(self, invocation, expected='succeeded', *, worker=None):
        start = time.perf_counter()
        result = (worker or self.f.worker).run_once(PROJECT, invocation_id=invocation['invocation_id'])
        if self.f.capture:
            self.f.capture.worker(invocation, result, start, expected)
        self.f.current['executions'].append({'operation': invocation['operation']['operation_id'],
            'invocation_id': invocation['invocation_id'], 'state': result['state'],
            'elapsed_ms': round((time.perf_counter() - start) * 1000, 3)})
        self.f.check('worker_state', result['state'], expected)
        return result

    def content(self, ref):
        return self.f.request('evidence', 'GET', PREFIX + '/artifacts/' + ref['id'] + '/content').json()

    def trace_path(self, ref):
        return PREFIX + '/traces/' + ref['id'] + '/revisions/' + str(ref['revision'])

    def claim(self, invocation):
        path = PREFIX + '/invocations/' + invocation['invocation_id']
        attempt = self.f.request('execution', 'POST', path + '/claim', 201,
            key=invocation['invocation_id'] + '-claim', json={'worker_id': 'case-executor', 'lease_seconds': 60}).json()['attempt']
        return path, {key: attempt[key] for key in ('attempt', 'lease_id')}

    def compare_harnesses(self):
        _, _, a = self.seed('compare-a', query='same-task', model='model-a', harness='Codex', env_id='fixed-env')
        alternative = copy.deepcopy(self.template)
        alternative['spans'][-1]['status'] = 'error'
        self.seed('compare-b', query='same-task', document=alternative, model='model-b', harness='Claude Code', env_id='fixed-env')
        value = self.f.request('compare', 'POST', PREFIX + '/traces/aggregate',
            json={'filters': {'query_id': ['same-task']}, 'group_by': 'model'}).json()
        self.f.check('models_not_merged', value['group_count'], 2)
        self.f.check('same_question_different_outcomes',
            {g['value']: g['metrics']['record_status']['error'] for g in value['groups']}, {'model-a': 0, 'model-b': 1})
        selection = self.f.freeze({'query_id': ['same-task']}, 'compare-selection')
        self.f.check('all_harnesses_selected', len(self.f.members(selection)), 2)
        self.f.execute('official.record-counts', 'source', a, 'compare-counts')

    def environments(self):
        for isolation in ('sandbox', 'non_sandbox', 'unknown'):
            doc = copy.deepcopy(self.template)
            doc['environment']['isolation'] = isolation
            _, _, ref = self.seed('env-' + isolation, query='environment-case', document=doc, env_id=isolation)
            got = self.f.request('environment', 'GET', self.trace_path(ref)).json()['revision']['metadata']['environment']
            self.f.check('isolation_preserved', got['isolation'], isolation)
        value = self.f.request('environment', 'POST', PREFIX + '/traces/aggregate',
            json={'filters': {'query_id': ['environment-case']}, 'group_by': 'env_id'}).json()
        self.f.check('environments_are_separate', sorted(g['value'] for g in value['groups']), ['non_sandbox', 'sandbox', 'unknown'])

    def labels(self):
        _, raw, ref = self.seed('label-source')
        refs = []
        for name, label, version in [('business.a', 'risky', '1.0.0'), ('business.b', 'acceptable', '1.0.0'), ('business.a', 'review', '2.0.0')]:
            artifact, _ = self.publish(name + version, json.dumps({'label': label}).encode(),
                inputs=[{'role': 'source', 'ref': ref}], producer=name, version=version)
            refs.append(artifact['ref'])
        found = self.f.request('labels', 'POST', PREFIX + '/artifacts/query', json={'filters': {'input_ref': ref}}).json()
        self.f.check('independent_labels_coexist', len(found['items']), 3)
        selected = self.f.request('labels', 'POST', PREFIX + '/artifacts/query',
            json={'filters': {'input_ref': ref, 'producer_name': 'business.a', 'producer_version': '1.0.0'}}).json()
        self.f.check('label_version_filter', [item['artifact_id'] for item in selected['items']], [refs[0]['id']])
        self.f.check('original_not_relabelled', self.f.request('evidence', 'GET', self.trace_path(ref) + '/content').content == raw, True)

    def evidence_chain(self):
        _, _, ref = self.seed('evidence-source')
        selected = self.f.freeze({'run_id': [ref['id']]}, 'evidence-selection')
        sref = {'kind': 'selection_snapshot', 'id': selected['selection_id'], 'revision': None, 'digest': selected['manifest']['digest']}
        expert, _ = self.publish('expert-reference', b'{"label":"needs-review","expert":"synthetic-expert"}',
                                inputs=[{'role': 'trace', 'ref': ref}])
        validation, body = self.publish('validation-evidence', b'{"matches_reference":true,"sample_size":1}', inputs=[
            {'role': 'dataset', 'ref': sref}, {'role': 'expert', 'ref': expert['ref']}], producer='acceptance.verifier')
        self.f.check('declared_evidence_is_not_platform_certification', validation['provenance'], 'declared')
        repeat = self.f.request('evidence', 'POST', PREFIX + '/artifacts', key='validation-evidence', json=body).json()
        self.f.check('evidence_retry_no_duplicate', repeat['created'], False)
        changed = copy.deepcopy(body); changed['content'] = encoded(b'{"matches_reference":false}')
        self.f.request('evidence', 'POST', PREFIX + '/artifacts', 409, key='validation-evidence', json=changed)
        self.f.check('original_evidence_survives_conflict', self.content(validation['ref'])['matches_reference'], True)

    def attachments(self):
        _, _, ref = self.seed('media-source')
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
        png += chunk(b'IDAT', zlib.compress(b'\0\0\0\0')) + chunk(b'IEND', b'')
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as stream:
            stream.setparams((1, 2, 8000, 80, 'NONE', 'not compressed')); stream.writeframes(b'\0\0' * 80)
        for name, media, data in [('camera', 'image/png', png), ('voice', 'audio/wav', audio.getvalue())]:
            first, _ = self.publish(name, data, media=media, inputs=[{'role': 'source', 'ref': ref}])
            second, _ = self.publish(name + '-independent', data, media=media, inputs=[{'role': 'source', 'ref': ref}])
            self.f.check('same_bytes_same_content_digest', first['content']['digest'], second['content']['digest'])
            self.f.check('same_bytes_distinct_artifact_identity', first['artifact_id'] != second['artifact_id'], True)
        self.f.current['coverage_note'] = 'Tests attached Artifact bytes and trace lineage; native multimodal bundle ingestion/rendering is not covered.'

    def long_trace(self):
        doc = copy.deepcopy(self.template)
        span = doc['spans'][1]
        doc['spans'] = [{**copy.deepcopy(span), 'id': 'tool-' + str(i), 'status': 'error' if i % 128 == 0 else 'ok'} for i in range(4096)]
        doc['links'] = []; doc['evidence'] = []
        doc['spans'][0]['output'] = {'large_body_marker': 'PRIVATE-LARGE-BODY', 'value': 'x' * (2 * 1024 * 1024)}
        _, raw, ref = self.seed('long-trace', document=doc)
        page = self.f.request('search', 'POST', PREFIX + '/traces/query', json={'filters': {'run_id': [ref['id']]}})
        self.f.check('long_record_count', page.json()['items'][0]['record_count'], 4096)
        self.f.check('search_preview_stays_small', len(page.content) < 4096, True)
        self.f.check('search_does_not_embed_body', 'PRIVATE-LARGE-BODY' in page.text, False)
        counts, content = self.f.execute('official.record-counts', 'source', ref, 'long-counts')
        self.f.check('long_plugin_count', content['records'], 4096)
        self.f.check('long_plugin_errors', content['statuses']['error'], 32)
        self.f.check('large_original_roundtrip', self.f.request('read', 'GET', self.trace_path(ref) + '/content').content == raw, True)
        self.f.current['coverage_note'] = '4096 records and a 2 MiB body; runtime not a production capacity benchmark. Record-level paging is not tested.'

    def many_cases(self):
        doc = copy.deepcopy(self.template); doc['spans'] = []; doc['links'] = []; doc['evidence'] = []
        for index in range(105):
            self.seed(f'page-{index:03}', query='paged-batch', document=doc)
        items, cursor = [], None
        while True:
            body = {'filters': {'query_id': ['paged-batch']}, 'limit': 40}
            if cursor: body['cursor'] = cursor
            page = self.f.request('search', 'POST', PREFIX + '/traces/query', json=body).json()
            items.extend(page['items']); cursor = page['next_cursor']
            if cursor is None: break
        self.f.check('all_pages_complete', len(items), 105)
        self.f.check('no_duplicate_runs', len({item['run_id'] for item in items}), 105)
        selected = self.f.freeze({'query_id': ['paged-batch']}, 'paged-selection')
        self.f.check('selection_matches_all_pages', [m['run_id'] for m in self.f.members(selected, 40)], [m['run_id'] for m in items])

    def conflicting_import(self):
        doc, raw, ref = self.seed('conflict-source')
        doc['run']['title'] = 'changed'
        self.f.import_trace(json.dumps(doc).encode(), 'conflict-source', status=409)
        self.f.import_trace(json.dumps(doc).encode(), 'stale-revision', status=409)
        self.f.check('conflicting_upload_preserves_bytes', self.f.request('read', 'GET', self.trace_path(ref) + '/content').content == raw, True)
        self.f.execute('official.record-counts', 'source', ref, 'after-upload-conflict')

    def invalid_import(self):
        before = self.f.work_counts()
        invalid = copy.deepcopy(self.template); invalid['run']['id'] = 'invalid-document'; invalid['spans'][0]['status'] = 'invented'
        self.f.import_trace(json.dumps(invalid).encode(), 'invalid-document', status=422)
        self.f.import_trace(b'{incomplete', 'broken-json', status=422)
        page = self.f.request('search', 'POST', PREFIX + '/traces/query', json={'filters': {'run_id': ['invalid-document']}}).json()
        self.f.check('rejected_document_not_discoverable', page['items'], [])
        self.f.check('rejection_creates_no_analysis', self.f.work_counts(), before)
        _, _, ref = self.seed('valid-after-invalid')
        self.f.execute('official.record-counts', 'source', ref, 'after-invalid')

    def retry_worker(self):
        _, raw, ref = self.seed('retry-worker-source')
        invocation = self.submit('retry-worker', ref)
        descriptor = self.f.store.revisions.get(PROJECT, ref['id'], 1)
        path = self.f.store.content._path(ContentRef(**descriptor['content']))
        self.f.current['fault_injection'] = 'Remove only this synthetic input blob; restore exact bytes before retry.'
        path.unlink()
        try:
            failed = self.run(invocation, 'failed')
            self.f.check('input_failure_has_no_output', failed['result']['outputs'], [])
            self.f.check('input_failure_reason', failed['result']['error']['code'], 'worker.content_unavailable')
        finally:
            path.write_bytes(raw)
        url = PREFIX + '/invocations/' + invocation['invocation_id']
        self.f.request('retry', 'POST', url + '/retry', json={'expected_attempt': 1})
        success = self.run(invocation)
        self.f.check('retry_new_attempt', success['result']['attempt'], 2)
        self.f.check('old_failure_stays_readable', self.f.request('evidence', 'GET', url + '/results/1').json(), failed['result'])

    def cancellation(self):
        _, _, ref = self.seed('cancel-source')
        invocation = self.submit('cancel-task', ref)
        path, fence = self.claim(invocation)
        self.f.request('cancel', 'POST', path + '/cancel', json={'expected_attempt': 1})
        output = {'role': 'counts', 'content': encoded(b'{}'), 'metadata': {}}
        self.f.request('cancel', 'POST', path + '/complete', 409, json={**fence, 'outputs': [output]})
        self.f.check('cancelled_not_succeeded', self.f.request('read', 'GET', path).json()['status'], 'cancelled')
        self.f.request('read', 'GET', path + '/results/1', 404)

    def expired_lease(self):
        _, _, ref = self.seed('expired-source')
        invocation = self.submit('expired-task', ref)
        path, fence = self.claim(invocation)
        self.f.current['fault_injection'] = 'Expire this synthetic attempt in the test database; no real clock wait.'
        with self.f.store.repository.engine.begin() as db:
            db.execute(text('UPDATE invocation_attempts SET lease_expires_ms=0 WHERE project_id=:project AND invocation_id=:id'),
                       {'project': PROJECT, 'id': invocation['invocation_id']})
        recovered = self.f.request('recover', 'POST', PREFIX + '/invocations/recover-expired', json={'limit': 100}).json()
        self.f.check('expired_attempt_observed', any(row['invocation_id'] == invocation['invocation_id'] for row in recovered['items']), True)
        self.f.request('late-callback', 'POST', path + '/heartbeat', 409, json=fence)
        self.f.request('retry', 'POST', path + '/retry', json={'expected_attempt': 1})
        self.f.check('recovered_task_runs', self.run(invocation)['result']['attempt'], 2)

    def faulty_output(self, mode):
        _, _, ref = self.seed('fault-' + mode)
        invocation = self.submit('fault-task-' + mode, ref, operation='acceptance.fault', config={'mode': mode})
        before = self.f.work_counts()['artifacts']
        result = self.run(invocation, 'failed', worker=self.test_worker)
        self.f.check('failure_code', result['result']['error']['code'], 'worker.timeout' if mode == 'timeout' else 'worker.output_invalid')
        self.f.check('no_partial_result', result['result']['outputs'], [])
        self.f.check('failed_plugin_no_artifact', self.f.work_counts()['artifacts'], before)
        self.f.current['fault_injection'] = 'Actual test plugin sleeps past its timeout or returns an undeclared output role.'

    def independent_requests(self):
        _, _, ref = self.seed('independent-source')
        first, _ = self.f.execute('official.record-counts', 'source', ref, 'independent-one')
        second, _ = self.f.execute('official.record-counts', 'source', ref, 'independent-two')
        self.f.check('independent_runs_have_different_artifacts', first['id'] != second['id'], True)
        descriptors = [self.f.request('read', 'GET', PREFIX + '/artifacts/' + r['id']).json() for r in (first, second)]
        self.f.check('equal_bytes_can_share_digest', descriptors[0]['content']['digest'], descriptors[1]['content']['digest'])

    def index_recovery(self):
        _, raw, ref = self.seed('index-recovery')
        descriptor = self.f.store.revisions.get(PROJECT, ref['id'], 1)
        path = self.f.store.content._path(ContentRef(**descriptor['content']))
        self.f.current['fault_injection'] = 'Temporarily corrupt this synthetic content blob, then restore it and explicitly reindex.'
        path.write_bytes(b'corrupted fixture')
        try:
            self.f.request('read', 'GET', self.trace_path(ref) + '/content', 500)
            indexed = self.f.request('index', 'POST', self.trace_path(ref) + '/index').json()['index']
            self.f.check('bad_content_not_valid_index', indexed['state'], 'failed')
            self.f.check('failed_index_count_unknown', indexed['counts']['records'], None)
        finally:
            path.write_bytes(raw)
        indexed = self.f.request('index', 'POST', self.trace_path(ref) + '/index').json()['index']
        self.f.check('index_recovers_from_original', indexed['counts']['records'], 4)

    def business(self, scenario):
        fixture = json.loads((DATA / 'business-scenarios.json').read_text())['scenarios'][scenario]
        refs = []
        for number, output in enumerate(fixture['inputs']):
            doc = copy.deepcopy(self.template); doc['spans'][-1]['output'] = output
            refs.append(self.seed(f'{scenario}-{number}', query=scenario, document=doc)[2])
        selected = self.f.freeze({'query_id': [scenario]}, scenario + '-selection')
        self.f.check('business_selection_complete', len(self.f.members(selected)), len(refs))
        inputs = [{'role': 'selection', 'ref': {'kind': 'selection_snapshot', 'id': selected['selection_id'],
                   'revision': None, 'digest': selected['manifest']['digest']}}]
        inputs += [{'role': 'traces', 'ref': ref} for ref in refs]
        if 'context' in fixture:
            context, _ = self.publish(scenario + '-context', json.dumps(fixture['context']).encode())
            inputs.append({'role': 'context', 'ref': context['ref']})
        config = {'scenario': scenario}
        if 'target' in fixture: config['target'] = fixture['target']
        invocation = self.submit(scenario + '-analysis', None, operation='acceptance.scenario-metrics', inputs=inputs, config=config)
        result = self.run(invocation, worker=self.test_worker)
        ref = result['result']['outputs'][0]['artifact']
        body = self.content(ref)
        for key, value in fixture['expected'].items(): self.f.check(scenario + ':' + key, body[key], value)
        self.f.check('business_evidence_scope', body['input_members'], len(refs))
        self.f.check('business_method_not_certified', body['validation'], 'not_validated_for_business')
        descriptor = self.f.request('evidence', 'GET', PREFIX + '/artifacts/' + ref['id']).json()
        self.f.check('all_business_inputs_linked', descriptor['inputs'], inputs)
        self.f.current['coverage_note'] = 'Reviewed synthetic test plugin; validates input/result lineage and descriptive arithmetic, not business effectiveness or causal validity.'
        self.f.current['result_refs'] = {'report': ref, 'selection_id': selected['selection_id']}

    def reconnect(self):
        _, raw, ref = self.seed('reconnect-source')
        report_ref, expected = self.f.execute('official.record-counts', 'source', ref, 'before-reconnect')
        repository = self.f.store.repository
        location = repository.engine.url.render_as_string(hide_password=False) if repository.postgres else repository.path
        reopened = Store(location, content_store=LocalContentStore(self.f.store.content.root))
        try:
            with TestClient(create_app(reopened, allowed_origins=[]), client=('198.51.100.21', 45001)) as client:
                started = time.perf_counter()
                response = client.get(PREFIX + '/artifacts/' + report_ref['id'] + '/content', headers=self.f.auth)
                if self.f.capture: self.f.capture.http('reconnect', response, started)
                self.f.check('reopened_api_accepts_persisted_identity', response.status_code, 200)
                self.f.check('reopened_api_reads_same_result', response.json(), expected)
                started = time.perf_counter()
                response = client.get(self.trace_path(ref) + '/content', headers=self.f.auth)
                if self.f.capture: self.f.capture.http('reconnect', response, started)
                self.f.check('reopened_api_reads_original', response.content == raw, True)
        finally:
            reopened.close()
        self.f.current['coverage_note'] = 'New API instance and database connections, not an OS process crash/restart test.'

    def actions(self):
        return [
            ('FLOW-009', '同题多 Harness / 模型比较', self.compare_harnesses),
            ('FLOW-010', '沙箱、非沙箱与未知环境', self.environments),
            ('FLOW-011', '冲突标签与方法版本共存', self.labels),
            ('FLOW-012', '专家材料、验证记录与不可变证据', self.evidence_chain),
            ('FLOW-013', '图片、音频附件的保存与来源', self.attachments),
            ('FLOW-014', '4096 条记录与 2 MiB 正文', self.long_trace),
            ('FLOW-015', '105 条轨迹的查询与选集分页', self.many_cases),
            ('FLOW-016', '导入冲突与旧版本保留', self.conflicting_import),
            ('FLOW-017', '无效导入后继续正常使用', self.invalid_import),
            ('FLOW-018', '执行输入缺失、恢复与重试', self.retry_worker),
            ('FLOW-019', '运行取消与迟到回执', self.cancellation),
            ('FLOW-020', '租约过期、恢复与重新执行', self.expired_lease),
            ('FLOW-021', '真实插件输出不符合合同', lambda: self.faulty_output('invalid-output')),
            ('FLOW-022', '真实插件执行超时', lambda: self.faulty_output('timeout')),
            ('FLOW-023', '相同输入的两次独立执行', self.independent_requests),
            ('FLOW-024', '损坏内容与显式重建索引', self.index_recovery),
            ('FLOW-025', '门店补货三策略比较', lambda: self.business('replenishment')),
            ('FLOW-026', '物业巡检调用与计划覆盖', lambda: self.business('inspection')),
            ('FLOW-027', '旅社 GEO 检索与引用', lambda: self.business('geo')),
            ('FLOW-028', '重建 API 实例后继续读取', self.reconnect),
        ]
