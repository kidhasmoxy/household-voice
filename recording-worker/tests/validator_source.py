"""Test-only canonical validator lookup; never imported by worker runtime."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys


def find_validator(root):
    explicit=os.environ.get('HOUSEHOLD_VOICE_RECORDING_VALIDATOR')
    if explicit:
        path=Path(explicit)
        if not path.is_file():raise FileNotFoundError('Explicit test validator source does not exist')
        return path
    candidates=(root/'tests/fixtures/recording_session.py',
        root.parent/'custom_components/household_voice/recording_session.py',
        root.parent/'ha-integration/custom_components/household_voice/recording_session.py')
    for path in candidates:
        if path.is_file():return path
    raise FileNotFoundError('Provide the public canonical recording validator or export the reviewed test fixture')


def load_validator(root):
    path=find_validator(root)
    if path==root/'tests/fixtures/recording_session.py':
        provenance=json.loads((path.parent/'source.json').read_text(encoding='utf-8'))
        if hashlib.sha256(path.read_bytes()).hexdigest()!=provenance['sha256']:
            raise ValueError('Test fixture differs from its reviewed canonical source')
    name='recording_validator_for_worker'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module;spec.loader.exec_module(module)
    return module
