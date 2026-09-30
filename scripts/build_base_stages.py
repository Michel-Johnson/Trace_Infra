"""Build the immutable first Base stage classifier package."""
import hashlib
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from trace_hunter.catalog import canonical

def build(version='1.0.0'):
    if version not in ('1.0.0','1.0.1','1.1.0','1.2.0','1.3.0'): raise ValueError('Unsupported package version')
    directory='base-stages' if version=='1.0.0' else 'base-stages-'+version
    path = f'plugins/extensions/{directory}/classify.py'
    files = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()}
    contribution = {'id': 'classify', 'title': 'Base 阶段分类', 'kind': 'slicer', 'mode': 'classify',
        'implementation': {'host': 'server', 'ref': 'classify.py'}, 'scopes': ['run'],
        'consumes': ['trace-hunter/1.0', 'trace-hunter/1.1'],
        'config_schema': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False},
        'default_config': {}, 'trigger': 'explicit', 'output_kind': 'facets', 'permissions': ['trace.read', 'facets.submit']}
    return {'schema_version': 'trace-hunter/plugin/2.0', 'plugin_id': 'official.base-stages', 'version': version,
        'title': 'Base 阶段分类', 'description': '识别 Plan、Spec、Table、Flow 等生成阶段；保留来源、未知分类与原始调用顺序。',
        'package_digest': canonical(files)[1], 'contributes': [contribution],
        'extensions': {'trace_hunter.classification_targets': {'classify': ['tool', 'model', 'wait', 'agent']}}}, {'files': files}

if __name__ == '__main__':
    # Published 1.0.0 stays intact so historical jobs can still be retried.
    for name, value in zip(('manifest.json', 'package.json'), build('1.3.0')):
        (ROOT / 'plugins/extensions/base-stages-1.3.0' / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
