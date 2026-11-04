"""Register private sync bindings before GitHub renders derived plan values."""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from typing import TextIO

_IDENTITY = re.compile(r'account|project|provider|service_name|hostname|chat_id|secret_name', re.I)
_ACCOUNT = re.compile(r'(?i)\b(?:DU|U)\d{5,}\b')
_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+')
_RESOURCE = re.compile(r'(?:projects/|[a-z][a-z0-9+.-]*://)[^\s"\'<> ,;]+', re.I)


def mask_identifiers(value: object, output: TextIO = sys.stdout) -> None:
    """Collect fully before printing; malformed structured bindings fail closed.

    Whole JSON secrets do not mask extracted scalars. Register those scalars and
    resource components too, including their JSON-escaped representation.
    """
    values: set[str] = set()

    def add(raw: str) -> None:
        if raw:
            values.add(raw)
            values.add(json.dumps(raw, ensure_ascii=True)[1:-1])

    def visit(item: object, key: str = '', identity: bool = False) -> None:
        identity = identity or bool(_IDENTITY.search(key))
        if isinstance(item, Mapping):
            for name, child in item.items():
                visit(child, str(name), identity)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key, identity)
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            text = str(item)
            if not text:
                return
            if key.upper().endswith('_JSON') or text.lstrip().startswith(('{', '[')):
                visit(json.loads(text), identity=identity)
                return
            if identity:
                add(text)
            for pattern in (_ACCOUNT, _EMAIL, _RESOURCE):
                for match in pattern.finditer(text):
                    add(match.group())
            for email in _EMAIL.findall(text):
                domain = email.split('@', 1)[1]
                if domain.endswith('.iam.gserviceaccount.com'):
                    add(domain.removesuffix('.iam.gserviceaccount.com'))
            # Mask independently printed project numbers, pool IDs and provider
            # IDs, not only the complete WIF resource path or email address.
            if 'projects/' in text or '://' in text or '@' in text:
                for component in re.split(r'[/@:]', text):
                    if component and component not in {
                        'projects', 'locations', 'global', 'workloadIdentityPools',
                        'providers', 'https', 'http', 'gs',
                    }:
                        add(component)

    visit(value)
    for text in sorted(values, key=lambda item: (-len(item), item)):
        escaped = text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::add-mask::' + escaped, file=output, flush=True)


if __name__ == '__main__':
    try:
        mask_identifiers(dict(os.environ))
    except (ValueError, TypeError):
        raise SystemExit('sync_identifier_mask_invalid') from None
