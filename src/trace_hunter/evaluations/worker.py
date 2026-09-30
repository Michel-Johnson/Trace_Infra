"""Separate worker. Executes only shipped, hash-verified official Python files."""
import hashlib
import json
from .service import ROOT
from ..database import Conflict
from ..processes import ProcessFailure, run_python


def run_once(service):
    service.expire()
    pending=service.repo.rows("SELECT j.id FROM evaluation_jobs j JOIN evaluation_batches b ON b.id=j.batch_id JOIN plugin_versions p ON p.plugin_id=b.plugin_id AND p.version=b.plugin_version WHERE j.state='queued' AND p.execution_mode='builtin' ORDER BY j.created_at,j.position LIMIT 1")
    if not pending:return False
    jid=pending[0]['id']
    try:lease=service.claim(jid,'official-worker',builtin=True)
    except Conflict:return True
    attempt,token=lease['attempt'],lease['lease_token']
    try:
        context=service.context(jid,attempt,token)
        manifest=context['plugin'];implementation=None
        for path in (ROOT/'plugins/official').glob('*/manifest.json'):
            if json.loads(path.read_text())==manifest:implementation=path.parent/'evaluator.py';break
        code = implementation.read_bytes() if implementation is not None else None
        if code is None or hashlib.sha256(code).hexdigest()!=manifest['package_digest']:
            raise ValueError('插件实现与固定版本不匹配')
        if context['preflight']['blocked']:
            score={'status':'insufficient_data','metrics':[{'key':m['key'],'value':None,'status':'insufficient_data','reason':'输入缺少插件必需的数据。','evidence':[]} for m in manifest['metrics']], 'findings':[], 'usage':{'input_tokens':0,'output_tokens':0,'cost':0,'currency':None}}
        else:
            raw = run_python(code, json.dumps(context,ensure_ascii=False).encode(), max_output_bytes=1024*1024)
            score = json.loads(raw)
        service.submit(jid,attempt,token,score)
    except Conflict:
        pass  # Cancellation / expired lease wins over a late result.
    except Exception as error:
        reason = ('官方插件执行超时' if isinstance(error,ProcessFailure) and error.code=='timeout' else
                  '官方插件执行失败：'+('CalledProcessError' if isinstance(error,ProcessFailure) and error.code=='process_failed' else
                  'ValueError' if isinstance(error,ProcessFailure) else type(error).__name__))
        try:service.fail(jid,attempt,token,reason)
        except (Conflict,PermissionError):pass
    return True
