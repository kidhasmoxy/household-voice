"""Refresh only previously reviewed allowlisted files after an intentional edit.

New files must first be explicitly added to the manifest's files mapping. This
does not publish, discover files, read credentials, or copy unlisted content.
Review the Git diff before committing the refreshed manifest.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b',
    r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    r'\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b',
)


def update(check=False):
    target = ROOT / 'PUBLIC-RELEASE-MANIFEST.json'
    report = json.loads(target.read_text())
    hashes = {}
    for name in sorted(report['files']):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('Unsafe allowlisted filename')
        path = ROOT / name
        if path.is_symlink() or ROOT.resolve() not in path.resolve().parents:
            raise ValueError('Unsafe allowlisted path')
        raw = path.read_bytes()
        if name == 'custom_components/household_voice/brand/icon.png':
            from brand_icon import icon_png
            if raw != icon_png():
                raise ValueError('Brand icon differs from its original generator')
        else:
            text = raw.decode('utf-8')
            if any(re.search(pattern, text) for pattern in PATTERNS):
                raise ValueError('Credential signature in ' + name)
            if name.endswith('.py'):
                ast.parse(text, filename=name)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    version = json.loads((ROOT / 'custom_components/household_voice/manifest.json').read_text())['version']
    if check:
        if hashes != report['files'] or version != report['version']:
            raise ValueError('Manifest is stale; review changes and refresh it explicitly')
    else:
        report.update(files=hashes, version=version)
        report['maintenance_note'] = 'Allowlisted content refreshed; original export privacy review is historical. Review new content before publishing.'
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    update(parser.parse_args().check)
