"""Execute shipped contributions; external implementations are never loaded here."""
import hashlib
import json
from .contracts import ROOT
from ..catalog import canonical
from ..database import Conflict
from ..processes import ProcessFailure, run_python

def run_once(service):
    service.expire()
    rows=service.repo.rows(f"SELECT j.id FROM {service.tables.jobs} j JOIN {service.tables.batches} b ON b.id=j.batch_id JOIN {service.tables.plugins} p ON p.plugin_id=b.plugin_id AND p.version=b.plugin_version WHERE j.state='queued' AND p.execution_mode='builtin' ORDER BY j.created_at,j.position LIMIT 1")
    if not rows:return False
    jid=rows[0]['id']
    try:lease=service.claim(jid,'official-contribution-worker',builtin=True)
    except Conflict:return True
    args=(jid,lease['attempt'],lease['lease_token'])
    try:
        context=service.context(*args);definition=context['plugin'];implementation=None
        for path in (ROOT/'plugins/extensions').glob('*/manifest.json'):
            manifest=json.loads(path.read_text())
            if manifest['plugin_id']!=definition['plugin_id'] or manifest['version']!=definition['version']:continue
            if canonical(manifest)[1]!=definition['manifest_digest']:raise ValueError('插件声明摘要不匹配')
            package=json.loads((path.parent/'package.json').read_text())
            if canonical(package['files'])[1]!=definition['package_digest']:raise ValueError('包摘要不匹配')
            implementation=path.parent/definition['contribution']['implementation']['ref']
            relative=str(implementation.relative_to(ROOT))
            code = implementation.read_bytes()
            if implementation.name!='classify.py' or relative not in package['files'] or hashlib.sha256(code).hexdigest()!=package['files'][relative]:raise ValueError('未核验的实现')
            break
        if implementation is None:raise ValueError('未找到已发布实现')
        raw = run_python(code, json.dumps(context,ensure_ascii=False).encode(), max_output_bytes=1024*1024)
        service.submit(*args,json.loads(raw))
    except (Conflict,PermissionError):pass
    except Exception as error:
        kind = ('TimeoutExpired' if error.code=='timeout' else 'CalledProcessError' if error.code=='process_failed' else 'ValueError') if isinstance(error,ProcessFailure) else type(error).__name__
        try:service.fail(*args,'贡献点执行失败：'+kind)
        except (Conflict,PermissionError):pass
    return True
