"""Asynchronous evidence export and exact, sharded metrics over fixed snapshots."""

import io
import json
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .catalog import canonical
from .content import ContentRef
from .traces.index import PROJECTOR_VERSION


class BatchOperations:
    def __init__(self, query, tasks, content, repository, *, workers=2):
        self.query, self.tasks, self.content, self.repository = query, tasks, content, repository
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trace-batch")

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=False)

    def _create(self, project_id, *, request_key, title, source):
        steps = [{"id": key, "label": label, "state": "pending", "started_at": None,
                  "completed_at": None, "details": {}} for key, label in (
            ("freeze", "固定数据快照"), ("shard", "分片取数"),
            ("merge", "合并结果"), ("publish", "发布结果"))]
        task, created = self.tasks.create(project_id, request_key=request_key, kind="analysis",
                                          title=title, steps=steps, source=source, total=None)
        if created:
            self.pool.submit(self._run, project_id, task["task_id"])
        return task

    def evidence(self, project_id, body):
        request_key = body["request_key"]
        return self._create(project_id, request_key=request_key, title=body.get("title", "批量证据导出"),
                            source={"type": "evidence_export", "request": body})

    def analysis(self, project_id, body):
        request_key = body["request_key"]
        return self._create(project_id, request_key=request_key, title=body.get("title", "批量分析"),
                            source={"type": "analysis_batch", "request": body})

    def retry(self, project_id, task_id):
        task = self.tasks.retry(project_id, task_id)
        if task["source"].get("type") not in ("evidence_export", "analysis_batch"):
            raise ValueError("This task type cannot be retried by the batch executor")
        self.pool.submit(self._run, project_id, task_id)
        return task

    def read_evidence(self, project_id, task_id):
        task = self.tasks.get(project_id, task_id)
        if task["source"].get("type") != "evidence_export" or task["state"] != "succeeded":
            raise ValueError("Evidence export is not ready")
        ref = ContentRef(**task["result"]["content"])
        return self.content.read_bytes(ref), ref

    def _alive(self, project_id, task_id):
        return self.tasks.get(project_id, task_id)["state"] != "cancelled"

    def _update(self, project_id, task_id, **values):
        if self._alive(project_id, task_id):
            return self.tasks.update(project_id, task_id, **values)
        return self.tasks.get(project_id, task_id)

    def _documents(self, project_id, items):
        if not items:
            return {}
        grouped = {}
        for item in items:
            grouped.setdefault((item["run_id"], item["revision"]), []).append(item["object_id"])
        result = {}
        for (run_id, revision), object_ids in grouped.items():
            params = {"project": project_id, "run": run_id, "revision": revision,
                      "projector": PROJECTOR_VERSION}
            names = []
            for index, object_id in enumerate(object_ids):
                params[f"id_{index}"] = object_id; names.append(f":id_{index}")
            rows = self.repository.rows("SELECT object_id,field,text,text_state,source_refs FROM trace_search_documents "
                "WHERE project_id=:project AND run_id=:run AND revision=:revision AND projector_version=:projector "
                "AND object_id IN (" + ",".join(names) + ") ORDER BY document_ordinal", params)
            for row in rows:
                result.setdefault((run_id, revision, row["object_id"]), []).append({
                    "field": row["field"], "text": row["text"], "text_state": row["text_state"],
                    "source_refs": json.loads(row["source_refs"])})
        return result

    def _export(self, project_id, task_id, body):
        query = dict(body.get("query", {}))
        fields = body.get("fields") or ["run_id", "revision", "object_kind", "object_id", "span_id",
                                               "kind", "name", "status", "start_at", "end_at",
                                               "duration_ms", "attributes", "source_refs", "payload"]
        query.update(fields=fields, limit=min(body.get("page_size", 500), 1000), cursor=None)
        count, page = 0, 0
        with tempfile.TemporaryFile() as output:
            while True:
                if not self._alive(project_id, task_id): return
                result = self.query.query(project_id, query)
                if page == 0:
                    query["snapshot"] = result["snapshot"]
                    output.write((canonical({"type": "manifest", "version": "evidence-export/1",
                                             "snapshot": result["snapshot"], "query_digest": result["query_digest"]})[0] + "\n").encode())
                documents = self._documents(project_id, result["items"]) if body.get("include_documents", True) else {}
                for item in result["items"]:
                    record = {"type": "object", "object": item,
                              "documents": documents.get((item["run_id"], item["revision"], item["object_id"]), [])}
                    output.write((canonical(record)[0] + "\n").encode()); count += 1
                page += 1
                self._update(project_id, task_id, state="running", current_stage="shard",
                             processed=count, total=None, progress=min(0.9, 0.05 + page * 0.01),
                             message=f"已导出 {count} 个对象")
                if not result["next_cursor"]: break
                query["cursor"] = result["next_cursor"]
            output.write((canonical({"type": "summary", "objects": count})[0] + "\n").encode())
            self._update(project_id, task_id, state="running", current_stage="publish", progress=0.95)
            output.seek(0)
            ref = self.content.put(output, media_type="application/x-ndjson")
        self._update(project_id, task_id, state="succeeded", current_stage="publish", processed=count,
                     result={"objects": count, "content": ref.as_dict()},
                     artifacts=[{"kind": "evidence", "name": "evidence.ndjson", "ref": ref.as_dict()}],
                     message="证据导出完成")

    def _analysis(self, project_id, task_id, body):
        request = body.get("metrics", {})
        run_ids, snapshot, _ = self.query.run_ids(project_id, request.get("query", {}))
        self._update(project_id, task_id, state="running", current_stage="shard", total=len(run_ids),
                     progress=0.05, message=f"固定 {len(run_ids)} 条 Trace")
        shard_size = body.get("shard_size", 50)
        checkpoint = self.tasks.get_checkpoint(project_id, task_id)
        if checkpoint.get("snapshot") not in (None, snapshot):
            checkpoint = {}
        completed = dict(checkpoint.get("shards", {}))
        _, _, scope, options = self.query.metric_rows(project_id, {**request,
            "query": {**request.get("query", {}), "snapshot": snapshot}}, run_ids=[])
        for offset in range(0, len(run_ids), shard_size):
            if not self._alive(project_id, task_id): return
            shard = run_ids[offset:offset + shard_size]
            shard_id = str(offset // shard_size)
            if shard_id not in completed:
                rows, _, _, _ = self.query.metric_rows(project_id, {**request,
                    "query": {**request.get("query", {}), "snapshot": snapshot}}, run_ids=shard)
                payload = io.BytesIO()
                for row in rows:
                    value = {key: (item.isoformat() if isinstance(item, datetime) else item)
                             for key, item in dict(row).items()}
                    payload.write((canonical(value)[0] + "\n").encode())
                ref = self.content.put(io.BytesIO(payload.getvalue()), media_type="application/x-ndjson")
                completed[shard_id] = {"run_ids": shard, "content": ref.as_dict()}
                self.tasks.checkpoint(project_id, task_id, {"snapshot": snapshot, "shards": completed})
            done = sum(len(item["run_ids"]) for item in completed.values())
            self._update(project_id, task_id, state="running", current_stage="shard",
                         processed=done, total=len(run_ids), progress=0.05 + 0.65 * done / max(1, len(run_ids)),
                         message=f"已完成分片 {done}/{len(run_ids)}")
        self._update(project_id, task_id, state="running", current_stage="merge", progress=0.75)

        def merged_rows():
            for shard_id in sorted(completed, key=int):
                ref = ContentRef(**completed[shard_id]["content"])
                with self.content.open_verified(ref) as source:
                    for line in source:
                        if line.strip():
                            yield json.loads(line)

        result = self.query.metrics_from_rows(merged_rows(), snapshot, scope, options)
        self._update(project_id, task_id, state="succeeded", current_stage="publish", progress=1.0,
                     processed=len(run_ids), total=len(run_ids), result=result,
                     message="批量分析完成")

    def _run(self, project_id, task_id):
        task = self.tasks.get(project_id, task_id)
        try:
            self._update(project_id, task_id, state="running", current_stage="freeze", progress=0.01,
                         message="开始固定数据快照")
            source, body = task["source"]["type"], task["source"]["request"]
            if source == "evidence_export": self._export(project_id, task_id, body)
            elif source == "analysis_batch": self._analysis(project_id, task_id, body)
            else: raise ValueError("Unsupported batch operation")
        except Exception as error:
            if self.tasks.get(project_id, task_id)["state"] not in ("cancelled", "succeeded"):
                self.tasks.update(project_id, task_id, state="failed",
                                  error={"code": "BATCH_FAILED", "message": str(error), "issues": []},
                                  message="批量任务失败")
