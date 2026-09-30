"""Agent-facing offline reference CLI. All normal/error output is structured JSON."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from trace_hunter.interop.common import IngestError, issue, MAX_BYTES, reject
from trace_hunter.interop.service import prepare, describe, check_bundle


class Parser(argparse.ArgumentParser):
    def error(self, message):
        reject("ARGUMENT_INVALID", "/arguments", "命令参数无效。", "运行 describe 获取支持的动作和格式，或使用 --help。")


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest="action", required=True)
        commands.add_parser("describe")
        prep = commands.add_parser("prepare")
        prep.add_argument("input")
        prep.add_argument("--output", required=True)
        prep.add_argument("--format")
        for key in ("run-id", "query-id", "env-id"):
            prep.add_argument("--" + key)
        check = commands.add_parser("check")
        check.add_argument("bundle")
        check.add_argument("--require", choices=describe()["requirements"])
        check.add_argument("--full", action="store_true", help="Return the full mapping/issue report instead of bounded agent output")
        args = parser.parse_args(argv)
        if args.action == "describe":
            result = describe()
        elif args.action == "prepare":
            with Path(args.input).open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            binding = {key: getattr(args, key) for key in ("run_id", "query_id", "env_id") if getattr(args, key) is not None}
            result = prepare(raw, args.output, binding, args.format)
        else:
            result = check_bundle(args.bundle, args.require)
            if not args.full:
                result["issue_count"] = len(result["issues"])
                result["issues"] = result["issues"][:6]
                result["issues_truncated"] = result["issue_count"] > len(result["issues"])
                result["mapping"] = {k: v for k, v in result["mapping"].items() if k != "source_span_ids"}
                result["full_report_file"] = str((Path(args.bundle) / "report.json").resolve())
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except IngestError as error:
        print(json.dumps({"ok": False, "retryable": False, "issues": error.issues}, ensure_ascii=False))
        return 2
    except OSError:
        print(json.dumps({"ok": False, "retryable": False, "issues": [issue("IO_ERROR", "", "文件操作失败，未覆盖已有数据。", "核对路径、权限和可用空间。", "error")]}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
