#!/usr/bin/env python3
"""Prepare/apply an audited normalized-run replacement during a maintenance window.

Passwords come from DATABASE_URL in the process environment; neither credentials
nor trace payloads are printed. The outer operator pauses services and takes a
full PostgreSQL backup before apply. This command never restarts services.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from trace_hunter.storage import Store
from trace_hunter.run_replacement import plan,apply,save_plan,read_plan


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='action',required=True)
    prepare=commands.add_parser('plan')
    prepare.add_argument('--bundle',type=Path,required=True)
    prepare.add_argument('--collection',required=True)
    prepare.add_argument('--old-version',required=True)
    prepare.add_argument('--new-version',required=True)
    prepare.add_argument('--expected-count',type=int,default=50)
    prepare.add_argument('--plan-file',type=Path,required=True)
    execute=commands.add_parser('apply')
    execute.add_argument('--plan-file',type=Path,required=True)
    execute.add_argument('--backup-dir',type=Path,required=True)
    args=parser.parse_args()
    location=os.environ.get('DATABASE_URL')
    if not location:raise SystemExit('DATABASE_URL is required in the environment')
    store=None
    try:
        store=Store(location)
        if args.action=='plan':
            value=plan(store,args.bundle,args.collection,expected_old_version=args.old_version,
                       expected_new_version=args.new_version,expected_count=args.expected_count)
            save_plan(value,args.plan_file);result={'status':'planned','plan_file':str(args.plan_file),**value.summary()}
        else:result=apply(store,read_plan(args.plan_file),args.backup_dir)
    except (ValueError,KeyError) as error:
        # Domain errors deliberately contain only identity/scope diagnostics.
        raise SystemExit('Replacement refused: '+str(error)) from None
    except Exception:
        raise SystemExit('Replacement failed; inspect private backup/plan. No database transaction was partially committed.') from None
    finally:
        if store is not None:store.close()
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
