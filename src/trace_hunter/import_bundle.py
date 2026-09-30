"""Reusable, offline verification and packaging for standard trace import bundles."""
import hashlib
import re
from pathlib import Path
import tarfile
import tempfile
from contextlib import ExitStack

from .storage import Store
from .corpus_import import decode

MAX_IMPORT_BYTES = 16 * 1024 * 1024


def file_digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def safe_file(root, name):
    root, relative = Path(root).resolve(), Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Bundle paths must stay relative to their root')
    path = root / relative
    if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
        raise ValueError('Bundle sources cannot be symbolic links')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('Missing regular bundle file: ' + name)
    return path


def pointer_value(document, pointer):
    if pointer == '':
        return document
    if not pointer.startswith('/'):
        raise ValueError('JSON source pointer must start with /')
    for component in pointer[1:].split('/'):
        if re.search(r'~(?![01])', component):
            raise ValueError('Invalid JSON pointer escape')
        key = component.replace('~1', '/').replace('~0', '~')
        if isinstance(document, list):
            if not key.isascii() or not key.isdigit() or (len(key)>1 and key.startswith('0')):
                raise ValueError('Invalid source array index')
            document = document[int(key)]
        else:
            document = document[key]
    return document


def verify_imports(root, files, chronological=False, progress=None):
    """Use the platform's actual import/read path in a disposable database."""
    root = Path(root).resolve()
    file_names, source_hashes, shared_json = set(), {}, {}
    run_count = catalog_count = record_count = pointers = 0
    first_trace = None
    with tempfile.TemporaryDirectory(prefix='trace-import-verification-') as temp, ExitStack() as resources:
        store = Store(Path(temp) / 'verification.sqlite')
        if hasattr(store, 'close'):
            resources.callback(store.close)
        for entry in files:
            name = entry['path']
            if name in file_names:
                raise ValueError('Duplicate import file')
            file_names.add(name)
            path = safe_file(root, name)
            if path.stat().st_size > MAX_IMPORT_BYTES:
                raise ValueError('Import file exceeds 16 MiB')
            raw = path.read_bytes()
            if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Import file hash or size mismatch: ' + name)
            value = decode(raw)
            if value.get('schema_version') == 'trace-hunter/catalog/1.0':
                store.import_catalog(value)
                catalog_count += 1
                continue
            result = store.import_trace(value)
            if not result['created']:
                raise ValueError('Duplicate run ID in bundle')
            if entry.get('run_id') not in (None, result['id']):
                raise ValueError('Manifest run ID does not match input')
            if entry.get('query_id') not in (None, result['query_id']):
                raise ValueError('Manifest query ID does not match input')
            documents = {}
            for source in value['sources']:
                source_path = safe_file(path.parent, source['name'])
                if source_path not in source_hashes:
                    source_raw = None
                    source_hashes[source_path] = file_digest(source_path)
                    if source_path.suffix == '.json':
                        source_raw = source_path.read_bytes()
                        documents[source['id']] = decode(source_raw)
                        if source_path.name == 'catalog.json':
                            shared_json[source_path] = documents[source['id']]
                elif source_path in shared_json:
                    documents[source['id']] = shared_json[source_path]
                elif source_path.suffix == '.json':
                    documents[source['id']] = decode(source_path.read_bytes())
                if source_hashes[source_path] != source['sha256']:
                    raise ValueError('Raw source hash mismatch: ' + source['name'])
            for item in value['spans'] + value['evidence']:
                ref = item['source']
                if ref['source_id'] in documents:
                    try:
                        pointer_value(documents[ref['source_id']], ref['pointer'])
                    except (ValueError, TypeError, KeyError, IndexError) as error:
                        raise ValueError('Unresolved JSON source pointer: ' + item['id']) from error
                    pointers += 1
            bundle = store.get(result['id'])
            rows = bundle['view']['rows']
            # Default task view excludes setup/export phases; validate ordering
            # across the complete trace as well as the visible task rows.
            tools = sorted((s for s in value['spans'] if s['kind'] == 'tool'),
                           key=lambda s: s.get('sequence', 0))
            if chronological:
                known = [s['start_ms'] for s in tools if s['start_ms'] is not None]
                if known != sorted(known):
                    raise ValueError('Known tool timestamps are not in chronological order')
                phase_times = [p['start_ms'] for p in value['phases'] if p['start_ms'] is not None]
                if phase_times != sorted(phase_times):
                    raise ValueError('Known phase timestamps are not in chronological order')
            assert len(rows) <= len(tools)
            record_count += len(tools)
            run_count += 1
            if first_trace is None:
                first_trace = value
            if progress and run_count % 250 == 0:
                progress(run_count)
        if first_trace is not None and store.import_trace(first_trace)['created']:
            raise ValueError('Repeated import must be idempotent')
        if hasattr(store, 'repository'):
            analysis_count = store.repository.rows('SELECT count(*) AS n FROM analyses')[0]['n']
        else:
            with store.connect() as db:
                analysis_count = db.execute('SELECT count(*) FROM analyses').fetchone()[0]
        if analysis_count:
            raise ValueError('Browsing or import unexpectedly triggered analysis')
        for collection in store.list_collections():
            store.get_collection(collection['id'])
    return {'runs_imported_to_temporary_db': run_count, 'catalogs_imported': catalog_count,
            'records_checked': record_count, 'source_hashes_checked': True,
            'source_files_checked': len(source_hashes), 'json_source_pointers_checked': pointers,
            'chronological_order_checked': chronological,
            'analysis_records_created': 0, 'live_platform_modified': False}


def verify_bundle(root, progress=None):
    root = Path(root).resolve()
    if (root / 'INCOMPLETE').exists():
        raise ValueError('Bundle preparation did not finish')
    manifest = decode(safe_file(root, 'manifest.json').read_bytes())
    if manifest.get('state') != 'ready':
        raise ValueError('Bundle is not marked ready')
    from .bundle_integrity import verify_metadata
    metadata=verify_metadata(root,manifest)
    result = verify_imports(root, manifest['import_files'],
                            chronological=bool(manifest.get('ordering_policy')), progress=progress)
    expected_runs = manifest.get('runs', manifest.get('conversations'))
    if result['runs_imported_to_temporary_db'] != expected_runs:
        raise ValueError('Manifest session count mismatch')
    return {**result,**metadata}


def pack_bundle(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Archive already exists; use a new destination')
    if destination.is_relative_to(root):
        raise ValueError('Place the archive outside the bundle directory')
    verification = verify_bundle(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open('xb') as raw:
            created = True
            destination.chmod(0o600)
            with tarfile.open(fileobj=raw, mode='w:gz', compresslevel=6) as archive:
                for path in sorted(root.rglob('*')):
                    if path.is_symlink():
                        raise ValueError('Bundle contains a symbolic link')
                    if path.is_file():
                        archive.add(path, arcname=str(Path(root.name) / path.relative_to(root)), recursive=False)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return {'archive': str(destination), 'bytes': destination.stat().st_size,
            'sha256': file_digest(destination), 'verification': verification}
