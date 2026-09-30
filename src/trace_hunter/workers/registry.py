"""Repository-owned package locks. Uploaded operation keys are never commands."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..content import ContentCorruption
from ..invocations.service import operation_definition
from ..resources import digest_json

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class InstalledOperation:
    definition: dict
    entrypoint: Path
    code_digest: str

    @property
    def ref(self):
        return {'operation_id': self.definition['operation_id'], 'version': self.definition['version'],
                'digest': digest_json(self.definition)}

    def verified_code(self):
        raw = self.entrypoint.read_bytes()
        if 'sha256:'+hashlib.sha256(raw).hexdigest() != self.code_digest:
            raise ContentCorruption('Installed operation code does not match its package lock')
        return raw


class WorkerRegistry:
    def __init__(self, root=ROOT):
        # The registry belongs to a reviewed release, never to imported trace data.
        self.root = Path(root).resolve()
        self.installed = []
        lock = json.loads((self.root/'plugins/worker/registry.json').read_text())
        if lock['schema_version'] != 'trace-hunter/worker-registry/1':
            raise ValueError('Unsupported worker registry')
        for entry in lock['packages']:
            folder = (self.root/'plugins/worker'/entry['directory']).resolve()
            if not folder.is_relative_to(self.root/'plugins/worker'):
                raise ValueError('Package directory must be inside the worker release')
            package = json.loads((folder/'package.json').read_text())
            if digest_json(package) != entry['digest']:
                raise ContentCorruption('Worker package does not match the release lock')
            if package['schema_version'] != 'trace-hunter/worker-package/1' or package['runtime'] != {'language':'python','dependencies':[]}:
                raise ValueError('This worker supports self-contained Python packages only')
            for operation in package['operations']:
                filename = operation['entrypoint']
                path = (folder/filename).resolve()
                if not path.is_relative_to(folder) or path.suffix != '.py':
                    raise ValueError('Entrypoint must be a package Python file')
                definition = {**operation['definition'], 'implementation': {
                    'host':'worker', 'key':operation['key'], 'package_digest':entry['digest']}}
                definition = operation_definition(definition)
                installed = InstalledOperation(definition, path, package['files'][filename])
                installed.verified_code()
                self.installed.append(installed)
        references = [(item.ref['operation_id'], item.ref['version']) for item in self.installed]
        if len(set(references)) != len(references) or len(references)>100:
            raise ValueError('Registry requires unique operation versions, at most 100')

    def register(self, service, project_id):
        return [service.register_operation(project_id, item.definition)['operation'] for item in self.installed]

    def resolve(self, reference):
        for item in self.installed:
            if item.ref == reference:
                return item
        raise KeyError('Operation is not installed at this exact definition')
