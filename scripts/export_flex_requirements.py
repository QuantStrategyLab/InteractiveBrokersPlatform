"""Export the collector's existing dependency closure from uv.lock, with hashes."""
from pathlib import Path
import tomllib


def main():
    root = Path(__file__).resolve().parents[1]
    packages = tomllib.loads((root / 'uv.lock').read_text())['package']
    selected = {}
    pending = ['requests', 'google-cloud-storage']
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        candidates = [p for p in packages if p['name'] == name]
        if len(candidates) != 1 or 'registry' not in candidates[0]['source']:
            raise ValueError('Ambiguous or non-registry dependency: ' + name)
        package = candidates[0]
        selected[name] = package
        pending.extend(d['name'] for d in package.get('dependencies', []))
    lines = ['# Generated from uv.lock by scripts/export_flex_requirements.py.']
    for name, package in sorted(selected.items()):
        archives = package.get('wheels', []) + ([package['sdist']] if 'sdist' in package else [])
        hashes = sorted({a['hash'] for a in archives})
        if not hashes:
            raise ValueError('Missing hashes: ' + name)
        lines.append(name + '==' + package['version'] + ' \\\n' + ' \\\n'.join('    --hash=' + value for value in hashes))
    (root / 'containers/flex-daily/requirements.txt').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
