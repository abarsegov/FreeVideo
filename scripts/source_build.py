"""Bind source deltas to the actual PyInstaller Analysis, before packaging."""
import importlib.metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

from scripts.build_source_delta import inventory
import freevideo_source_bootstrap as source


def runtime_digest(path):
    if path.name != 'base_library.zip':
        return source.digest(path)
    # PyInstaller iterates a module set when writing this archive. Entry order
    # is not deterministic; every byte of every importable pyc still is hashed.
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate standard-library ZIP entries')
        return source.sha(source.canonical({name: source.sha(archive.read(name)) for name in sorted(names)}))


def finalize(analysis, root, staged, build_file, extra_runtime=None):
    root, staged, build_file = Path(root), Path(staged), Path(build_file)
    build = json.loads(build_file.read_text(encoding='utf-8'))
    # Nightly has mutable tags/identities and intentionally stays on full updates.
    if build.get('track', 'stable') != 'stable':
        return
    runtime, launcher_files = {}, set()
    # Source resources must follow __file__ when the package is loaded from disk.
    for name, filename, kind in list(analysis.datas):
        name = name.replace('\\', '/')
        if name.startswith('freevideo_engine/') and Path(name).suffix in source.EXTENSIONS:
            output = staged / name
            if not output.exists():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(filename, output)
            launcher_files.add(name)
    files = inventory(staged)
    for group in ('pure', 'binaries', 'datas', 'scripts'):
        for name, filename, kind in getattr(analysis, group):
            normalized = name.replace('\\', '/')
            if normalized.startswith('engine-source/') or normalized in ('launcher-build.json', 'source-base.json'):
                continue
            if group == 'pure' and Path(filename).is_relative_to(root):
                local = Path(filename).relative_to(root).as_posix()
                if local in files and source.digest(Path(filename)) != files[local]['sha256']:
                    raise ValueError('Source changed during PyInstaller analysis; rebuild from a clean snapshot')
            if group == 'pure' and (name == 'freevideo_engine' or name.startswith('freevideo_engine.')):
                relative = Path(filename).relative_to(root).as_posix()
                if relative not in files:
                    raise ValueError('Frozen application module missing from engine-source')
                launcher_files.add(relative)
                continue
            if group == 'datas' and normalized in files:
                continue
            path = Path(filename)
            if path.is_file():
                runtime[group + ':' + normalized] = runtime_digest(path)
            else:
                # Symlink TOCs contain their link target, not a source file.
                runtime[group + ':' + normalized] = str(filename)
    for name, filename in (extra_runtime or {}).items():
        runtime['packaging:' + name] = source.digest(Path(filename))
    # Include the PyInstaller bootloader and build policy, not just package versions.
    import PyInstaller
    for path in sorted((Path(PyInstaller.__file__).parent / 'bootloader').rglob('*')):
        if path.is_file():
            runtime['bootloader:' + path.relative_to(Path(PyInstaller.__file__).parent).as_posix()] = source.digest(path)
    for name in ('scripts/source_build.py', 'scripts/build_windows.py', 'scripts/build_macos.py', 'scripts/build_linux.py'):
        runtime[name] = source.digest(root / name)
    for name in ('scripts/prune_qt_bundle.py', 'scripts/deduplicate_bundle_data.py', 'scripts/pyi_rth_bundle_data.py'):
        if (root / name).is_file():
            runtime[name] = source.digest(root / name)
    descriptor = {'python': sys.version, 'platform': platform.system(), 'machine': platform.machine(),
                  'target': build.get('target', 'windows-x86_64'), 'packaging': build.get('packaging'),
                  'pyinstaller': importlib.metadata.version('pyinstaller'),
                  'optimization': getattr(analysis, 'optimize', 0), 'files': runtime}
    base = source.sha(source.canonical(descriptor))
    value = {'schema': 1, 'base': base, 'build': build, 'files': files, 'gui': True,
             'launcher_files': sorted(launcher_files), 'launcher_files_changed': False}
    value['identity'] = source.source_identity(value)
    source.manifest(value)
    output = build_file.with_name('source-base.json')
    output.write_bytes(source.canonical(value))
    build_file.with_name('source-runtime.json').write_bytes(source.canonical(descriptor))
    analysis.datas.append(('source-base.json', str(output), 'DATA'))
    present = {name.replace('\\', '/') for name, _, _ in analysis.datas}
    for name in files:
        destination = 'engine-source/' + name
        if destination not in present:
            analysis.datas.append((destination, str(staged / name), 'DATA'))


def spec_hook(root, staged, build_file, extra_runtime=None):
    return (f'\nimport sys\nsys.path.insert(0, {str(root)!r})\n'
            'from scripts.source_build import finalize as finalize_source\n'
            f'finalize_source(a, {str(root)!r}, {str(staged)!r}, {str(build_file)!r}, {extra_runtime!r})\n')


def run_pyinstaller(command, root, staged, build_file, extra_runtime=None):
    """Keep the established CLI options, inserting a hook after Analysis."""
    from PyInstaller.building.makespec import main as make_spec
    # Let PyInstaller's parser consume exactly the existing CLI arguments.
    from PyInstaller.__main__ import generate_parser
    parser = generate_parser()
    options = vars(parser.parse_args(command[3:]))
    filenames = options.pop('filenames')
    spec_path = Path(make_spec(filenames, **options))
    text = spec_path.read_text(encoding='utf-8')
    marker = '\npyz = PYZ('
    if marker not in text:
        raise ValueError('Unknown PyInstaller spec layout')
    spec_path.write_text(text.replace(marker, spec_hook(root, staged, build_file, extra_runtime) + marker, 1), encoding='utf-8')
    subprocess.run([*command[:3], '--noconfirm', '--distpath', options['distpath'],
                    '--workpath', options['workpath'], str(spec_path)], cwd=root, check=True)
