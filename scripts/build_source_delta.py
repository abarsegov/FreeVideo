"""Generate a bounded, adjacent-release source delta from a staged source tree.

Use the source-base.json emitted by the SAME platform's PyInstaller build.
No signing seed is needed here; sign_update_manifest.py is a separate step.
"""
import argparse
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import freevideo_source_bootstrap as source


def inventory(tree):
    files = {}
    for path in sorted(tree.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlinks are not source files')
        if path.is_file():
            name = source.safe_name(path.relative_to(tree).as_posix())
            files[name] = {'bytes': path.stat().st_size, 'sha256': source.digest(path)}
    return files


def manifest_name(value):
    return 'source-%s-%s.json' % (value['build']['product_version'],
                                value['build'].get('target', 'windows-x86_64'))


def generate(previous, tree, current, output):
    current = source.manifest(dict(current))
    if inventory(tree) != current['files']:
        raise ValueError('Current tree differs from its build inventory')
    output.mkdir(parents=True, exist_ok=True)
    value = {k: v for k, v in current.items() if k not in ('delta', 'signature')}
    if previous is not None:
        previous = source.manifest(previous)
        changed = sorted(n for n, row in value['files'].items() if previous['files'].get(n) != row)
        deleted = sorted(set(previous['files']) - set(value['files']))
        launcher_files = set(current.get('launcher_files', [])) | set(previous.get('launcher_files', []))
        value['launcher_files_changed'] = bool(launcher_files.intersection(changed + deleted))
        name = 'delta-%s-to-%s.zip' % (previous['identity'][:16], value['identity'][:16])
        archive = output / name
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for relative in changed:
                info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zipped.writestr(info, (tree / relative).read_bytes())
        value['delta'] = {'from': previous['identity'], 'files': changed, 'deleted': deleted,
                          'name': name, 'bytes': archive.stat().st_size, 'sha256': source.digest(archive)}
    source.manifest(value)
    target = output / manifest_name(value)
    data = source.canonical(value)
    if len(data) > source.MAX_MANIFEST:
        raise ValueError('Source manifest is too large')
    target.write_bytes(data)
    target.with_suffix('.json.sha256').write_text(source.sha(data) + '\n', encoding='ascii')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--previous', type=Path, help='Previous release source manifest; omit for initial baseline')
    parser.add_argument('--tree', type=Path, required=True, help='The build output engine-source directory')
    parser.add_argument('--current', type=Path, required=True, help='The build output source-base.json')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    target = generate(source.read_manifest(args.previous) if args.previous else None,
                      args.tree, source.read_manifest(args.current), args.out)
    print(json.dumps({'manifest': str(target)}))


if __name__ == '__main__':
    main()
