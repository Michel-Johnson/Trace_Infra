"""Verify a frozen release's source and static assets before running its code."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re


MANIFEST_NAME = 'release-manifest.json'
MANIFEST_VERSION = 'trace-hunter/release-manifest/1'
# Test logs and runtime data are not release source. The source manifest still
# verifies every listed document and example, as well as executable directories.
SOURCE_ROOTS = ('src', 'apps/api', 'apps/web', 'plugins', 'contracts', 'schemas',
                'db', 'scripts', 'deploy', 'tests', 'dist')
REQUIRED_FILES = ('scripts/deploy_release.py', 'scripts/release_manifest.py',
                  'scripts/invocation_worker.py', 'apps/api/run.py',
                  'deploy/Caddyfile', 'deploy/trace-hunter-worker.service',
                  'deploy/trace-hunter-invocation-worker.service', 'dist/index.html')
GENERATED_ROOT_FILES = {MANIFEST_NAME, 'verified.json', 'release-tests.log',
                        'release-test-result.json', 'stage-api.log', 'release.json'}
ALLOWED_LINKS = {'schemas':'contracts/schemas',
                 'docs/api/openapi.json':'../../contracts/openapi.json'}


def verify_manifest(root, *, commit=None):
    root = Path(root).resolve()
    raw = (root / MANIFEST_NAME).read_bytes()
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or
            manifest.get('schema_version') != MANIFEST_VERSION or
            not isinstance(manifest.get('commit'), str) or
            not re.fullmatch(r'[0-9a-f]{40}', manifest['commit']) or
            not isinstance(manifest.get('files'), dict)):
        raise RuntimeError('Invalid frozen release manifest')
    if commit is not None and manifest['commit'] != commit:
        raise RuntimeError('Release manifest does not match the requested commit')
    files = manifest['files']
    links = manifest.get('links',{})
    if not isinstance(links,dict):raise RuntimeError('Invalid release links')
    for name,target in links.items():
        path=root/name
        if (ALLOWED_LINKS.get(name)!=target or name in files or not path.is_symlink() or
                os.readlink(path)!=target or not path.resolve().is_relative_to(root) or not path.exists()):
            raise RuntimeError('Invalid release link: '+name)
    for name in ALLOWED_LINKS:
        if (root/name).is_symlink() and name not in links:
            raise RuntimeError('Unlisted release link: '+name)
    if not set(REQUIRED_FILES).issubset(files):
        raise RuntimeError('Release manifest is missing a required source or asset')
    for name, expected in files.items():
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or '..' in path.parts or
                str(path) != name or name == MANIFEST_NAME or
                not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)):
            raise RuntimeError('Invalid release manifest file entry')
        target = root / name
        if (target.is_symlink() or not target.resolve().is_relative_to(root) or
                not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected):
            raise RuntimeError('Frozen release file mismatch: ' + name)
    for directory in SOURCE_ROOTS:
        if directory in links:
            # The exact directory alias is verified above; its canonical target
            # contracts/schemas is already scanned and hashed as ordinary files.
            continue
        for path in (root / directory).rglob('*'):
            if '__pycache__' in path.parts:
                continue
            if path.is_symlink():
                if path.relative_to(root).as_posix() in links:continue
                raise RuntimeError('Unexpected link in frozen release: ' + path.relative_to(root).as_posix())
            if path.is_file() and path.relative_to(root).as_posix() not in files:
                raise RuntimeError('Unlisted release source or asset: ' + path.relative_to(root).as_posix())
    # Root-level Python files can shadow imports through the working directory.
    for path in root.iterdir():
        if path.is_file() and path.name not in files and path.name not in links and path.name not in GENERATED_ROOT_FILES:
            raise RuntimeError('Unlisted release file: ' + path.name)
    return {'commit': manifest['commit'], 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'file_count': len(files), 'link_count':len(links),'index_sha256': files['dist/index.html']}


def create_manifest(root,commit):
    """Freeze an exported commit plus its built dist, never the working checkout."""
    root=Path(root).resolve();files={};links={}
    for directory,children,names in os.walk(root,followlinks=False):
        folder=Path(directory)
        for name in list(children):
            path=folder/name;relative=path.relative_to(root).as_posix()
            if name in {'.git','.venv','node_modules','__pycache__'} or relative in {'var','apps/trace-lab','apps/web/dist'}:
                children.remove(name)
            elif path.is_symlink():
                children.remove(name);links[relative]=os.readlink(path)
        for name in names:
            path=folder/name;relative=path.relative_to(root).as_posix()
            if folder==root and name in GENERATED_ROOT_FILES:continue
            if path.is_symlink():links[relative]=os.readlink(path)
            else:files[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    (root/MANIFEST_NAME).write_text(json.dumps({'schema_version':MANIFEST_VERSION,'commit':commit,
        'files':dict(sorted(files.items())),'links':dict(sorted(links.items()))},indent=2)+'\n')
    return verify_manifest(root,commit=commit)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('create','verify'))
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--commit',required=True)
    args=parser.parse_args()
    result=create_manifest(args.root,args.commit) if args.command=='create' else verify_manifest(args.root,commit=args.commit)
    print(json.dumps(result))
