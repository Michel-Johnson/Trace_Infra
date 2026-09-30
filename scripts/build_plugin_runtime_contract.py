"""Publish general plugin IO separately from the retained draft documents."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'src')]
from scripts.build_plugin_extension_draft import build_manifest,build_facets,build_selection

def stable(value):
    if isinstance(value,str):return value.replace('-draft.1','')
    if isinstance(value,list):return [stable(v) for v in value]
    if isinstance(value,dict):return {k:stable(v) for k,v in value.items()}
    return value

def main():
    for name,builder in [('plugin-v2',build_manifest),('facets-v1',build_facets),('selection-v1',build_selection)]:
        (ROOT/'contracts/schemas'/(name+'.schema.json')).write_text(json.dumps(stable(builder()),ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':main()
