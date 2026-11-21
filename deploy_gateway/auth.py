"""Authentication against the existing version 1 gateway key store."""
import hashlib
import hmac
import json
from pathlib import Path
import re


def authorized(key_store, value):
    data = json.loads(Path(key_store).read_text())
    if data.get('version') != 1 or not isinstance(data.get('records'), dict):
        raise ValueError('Unsupported key store')
    digest = hashlib.sha256(value.encode()).hexdigest()
    accepted = False
    for record in data['records'].values():
        expected = record['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or not isinstance(record['active'], bool):
            raise ValueError('Invalid key record')
        matches = hmac.compare_digest(digest, expected)
        accepted = accepted or (matches and record['active'])
    return accepted
