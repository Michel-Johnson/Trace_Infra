"""Verify all locked dependency releases against the user-required seven-day cutoff."""
import concurrent.futures
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = '2026-09-17T00:00:00Z'
AGENT_NPM = {
    '@anthropic-ai/claude-code': 'sha512-UI8TGoOO0fYT38VSoAjtu9C0EQkOwgwA4+ETFCgQhTO9NZvpgCERANL0UzioEW89b2zbcoL1B3z+7W37QdBxOA==',
    '@anthropic-ai/claude-code-linux-x64': 'sha512-xiC1514UgW1aRvHOtfgUVUACWtOwm42TIeQlllHFAMFLrBYy+cG+MfSTlTYnjUPpu4zmUeEDEs2VdZL/iLIwGQ==',
    '@anthropic-ai/claude-code-linux-x64-musl': 'sha512-uie3ymQ3ZNgRsevP4gWeVK47hL0M4+J8iZA34In7OagGGk2mMPZ07Ft+JDETQrApDwTwqOnhPuO50W4ccUjg9g==',
    '@anthropic-ai/claude-code-linux-arm64': 'sha512-6cCEg5z4awB1i8H1vRMRqLCV9a6rSSzsXXJTpBE2Nte29NMJ2B49PwRcGvdMASYWOmMNgO6ErYQ6ckuQSeSo/g==',
    '@anthropic-ai/claude-code-linux-arm64-musl': 'sha512-WNM6kiyfmVfjY5yi44+8JBJ/5y20QvAnpjVjas9dRm28PEBLEANF2mDxHBeZa9lR9fjdLhuFxZoyYi4F/pT1NQ==',
    '@anthropic-ai/claude-code-win32-x64': 'sha512-D/OLuM2LsRnjTNwFP1LMYqdCa/q6RI1dzaKXCU57NXFOrldkWN9mTdpYxOyo9bl/sR3rHeJ1+ETWQX5DeQlcig==',
    '@anthropic-ai/claude-code-win32-arm64': 'sha512-nhIqku3HdRnxm1AX5C1k8eINa8vuV9rSOmGlo0JEMw5y6mYybSJIEEwOIEGZXIzFFPzm54Y8UASg9gSQm3k8pw==',
    '@anthropic-ai/claude-code-darwin-x64': 'sha512-zFcxR5XfroaBmWy2HX8fZ/e/kpAQ4GPIz+cAQMq55SAHP4UorpxQj/upplfvfDC3lSKFfYBDhhSXBs99fKkjJw==',
    '@anthropic-ai/claude-code-darwin-arm64': 'sha512-/rlrB+1ovRPWuLYNpuOvWY4dGOGyanW008KeHST6ZjOVfEFBFyqqFJiBaRpDNy5LdlnozZZYWUoMWr8W0KXyEg==',
}


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'TraceHunterDependencyAudit/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def main():
    lock = json.loads((ROOT/'apps/web/package-lock.json').read_text())
    npm = {}
    for path, package in lock['packages'].items():
        if not path:
            continue
        if not package.get('resolved', '').startswith('https://registry.npmjs.org/') or not package.get('integrity', '').startswith('sha512-'):
            raise ValueError('Unpinned or non-registry npm source: ' + path)
        name = package.get('name') or path.rsplit('node_modules/', 1)[1]
        npm[(name, package['version'])] = package['integrity']
    for name, integrity in AGENT_NPM.items():
        npm[(name, '2.1.274')] = integrity
    def check_npm(item):
        (name, version), integrity = item
        metadata = fetch('https://registry.npmjs.org/' + urllib.parse.quote(name, safe=''))
        published = metadata['time'][version]
        if published > CUTOFF:
            raise ValueError('npm dependency is too recent: ' + name + '@' + version)
        if metadata['versions'][version]['dist']['integrity'] != integrity:
            raise ValueError('npm integrity mismatch: ' + name + '@' + version)
        return {'ecosystem':'npm','name':name,'version':version,'published_at':published,'integrity':integrity}

    python_versions = {}
    for filename in ('apps/api/requirements.lock','requirements.lock','requirements-capture.lock'):
        content = (ROOT/filename).read_text()
        for match in re.finditer(r'^([a-zA-Z0-9_.-]+)==([^\s;]+)(.*?)(?=^[a-zA-Z0-9_.-]+==|\Z)', content, re.M|re.S):
            python_versions.setdefault((match[1],match[2]),set()).update(re.findall(r'--hash=sha256:([a-f0-9]{64})', match[3]))
    python_packages = [(name,version,hashes) for (name,version),hashes in python_versions.items()]
    def check_python(item):
        name, version, hashes = item
        metadata = fetch('https://pypi.org/pypi/' + name + '/' + version + '/json')
        files = {f['digests']['sha256']:f for f in metadata['urls']}
        if not hashes or not hashes.issubset(files):
            raise ValueError('Missing PyPI artifact hash: ' + name)
        accepted = [files[h] for h in hashes]
        published = max(f['upload_time_iso_8601'] for f in accepted)
        if published > CUTOFF:
            raise ValueError('Locked PyPI artifact is too recent: ' + name + ' ' + published)
        return {'ecosystem':'pypi','name':name,'version':version,'latest_locked_artifact_at':published,'artifact_hashes':len(hashes)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        checked = list(pool.map(check_npm, npm.items())) + list(pool.map(check_python, python_packages))
    report = {'cutoff': CUTOFF, 'minimum_release_age_days': 7, 'install_scripts': 'disabled', 'packages': sorted(checked, key=lambda p:(p['ecosystem'],p['name'],p['version']))}
    output = ROOT/'docs/security/dependency-verification.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(f'Verified {len(npm)} npm versions and {len(python_packages)} Python versions against {CUTOFF}.')

if __name__ == '__main__':
    main()
