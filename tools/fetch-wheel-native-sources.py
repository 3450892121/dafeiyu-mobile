"""Fetch fixed corresponding sources for libraries inside public Python wheels.

No archive or build recipe is executed. After the normal Linux source ZIP has
been generated, --append-zip adds these sources, recipes and notices atomically.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time
import urllib.parse
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'runtime/wheel-native-sources.lock.json'
CACHE = ROOT / '.tools/wheel-native-sources'
WHEELS = ROOT / '.tools/offline-bundle/wheels'
SOURCE_ZIP_LIMIT = 2 * 1024 * 1024 * 1024 - 1024 * 1024


def digest(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def project_path(value):
    root = Path(os.path.abspath(ROOT))
    path = Path(os.path.abspath(value))
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('Corresponding sources must stay in this project') from None
    current = root
    for part in ('', *relative.parts):
        if part:
            current /= part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Corresponding sources cannot follow filesystem links')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Corresponding-source path escaped this project')
    return path


def child(base, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative or
            PurePosixPath(relative).is_absolute() or
            any(part in ('', '.', '..') for part in relative.split('/'))):
        raise ValueError('Unsafe corresponding-source path')
    base = project_path(base)
    target = project_path(base.joinpath(*relative.split('/')))
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError('Corresponding-source path escaped its directory')
    return target


def load_lock():
    lock = json.loads(project_path(LOCK).read_text(encoding='utf-8'))
    if lock.get('schemaVersion') != 1 or not lock.get('files') or not lock.get('components'):
        raise ValueError('Missing wheel native corresponding-source identities')
    seen = set()
    for entry in lock['files']:
        relative = entry['path']
        child(CACHE, relative)
        url = urllib.parse.urlsplit(entry['url'])
        if (relative in seen or url.scheme != 'https' or not url.hostname or
                url.username or url.password or url.query or url.fragment or
                not isinstance(entry['size'], int) or not 0 < entry['size'] < SOURCE_ZIP_LIMIT or
                len(entry['sha256']) != 64 or any(c not in '0123456789abcdef' for c in entry['sha256'])):
            raise ValueError('Invalid fixed source metadata')
        seen.add(relative)
    for component in lock['components']:
        if not component.get('sourceFiles') or not set(component['sourceFiles']).issubset(seen):
            raise ValueError('A bundled library has no fixed corresponding source')
    return lock


def verified(file, entry):
    return file.is_file() and file.stat().st_size == entry['size'] and digest(file) == entry['sha256']


def fetch(entry, *, check=False):
    target = child(CACHE, entry['path'])
    if verified(target, entry):
        return target
    if check:
        raise ValueError('Missing or changed wheel native source: ' + entry['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = project_path(target.with_name(target.name + '.part'))
    try:
        request = urllib.request.Request(entry['url'], headers={'User-Agent': 'Dafeiyu-Public-Source/1'})
        started, count = time.monotonic(), 0
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open('wb') as output:
            if urllib.parse.urlsplit(response.geturl()).scheme != 'https':
                raise ValueError('Source redirected to non-HTTPS')
            while block := response.read1(256 * 1024):
                count += len(block)
                if count > entry['size'] or time.monotonic() - started > 900:
                    raise ValueError('Source download exceeded its fixed size or time budget')
                output.write(block)
        if not verified(temporary, entry):
            raise ValueError('Wheel native source size or SHA256 mismatch: ' + entry['path'])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def check_wheel_bindings(lock):
    for component in lock['components']:
        wheel = child(WHEELS, component['wheel'])
        if not wheel.is_file() or digest(wheel) != component['wheelSha256']:
            raise ValueError('Corresponding source is bound to a different wheel: ' + component['name'])
        with zipfile.ZipFile(wheel) as archive:
            for member in component['members']:
                data = archive.read(member['path'])
                if len(data) != member['size'] or hashlib.sha256(data).hexdigest() != member['sha256']:
                    raise ValueError('Bundled library differs from the source binding: ' + component['name'])


def source_payload(lock):
    payload = {'wheel-native-sources.lock.json': project_path(LOCK),
               'wheel-native/BUILDING.txt': project_path(ROOT / 'runtime/wheel-native-BUILDING.txt'),
               'wheel-native/THIRD-PARTY-NOTICES.md': project_path(ROOT / 'THIRD-PARTY-NOTICES.md')}
    for entry in lock['files']:
        payload['wheel-native/' + entry['path']] = fetch(entry, check=True)
    for relative in lock.get('noticeFiles', []):
        file = child(ROOT / 'runtime/licenses', relative)
        if not file.is_file():
            raise ValueError('Missing redistributed library notice: ' + relative)
        expected = next((entry for entry in lock.get('noticeDigests', []) if entry['path'] == relative), None)
        if expected is None or not verified(file, expected):
            raise ValueError('Redistributed library notice differs from its fixed copy: ' + relative)
        payload['wheel-native/licenses/' + relative] = file
    if not all(file.is_file() for file in payload.values()):
        raise ValueError('Missing wheel corresponding-source build instructions or notices')
    return payload


def append_zip(lock, target):
    target = project_path(target)
    if (not target.resolve().is_relative_to(project_path(ROOT / 'dist').resolve()) or
            target.suffix != '.zip' or not target.is_file()):
        raise ValueError('Append only to an existing project dist source ZIP')
    check_wheel_bindings(lock)
    payload = source_payload(lock)
    with zipfile.ZipFile(target) as existing:
        names = set(existing.namelist())
        duplicates = names.intersection(payload)
        if duplicates:
            if duplicates != set(payload) or any(
                    hashlib.sha256(existing.read(name)).hexdigest() != digest(file)
                    for name, file in payload.items()):
                raise ValueError('Stale wheel source section; regenerate the base Linux source ZIP')
            return target
        if not {'linux-sources.lock.json', 'BUILDING.txt'}.issubset(names):
            raise ValueError('Not the generated Linux corresponding-source ZIP')
    if target.stat().st_size + sum(file.stat().st_size for file in payload.values()) + 1024 * 1024 > SOURCE_ZIP_LIMIT:
        raise ValueError('Combined corresponding-source attachment would exceed the public upload size limit')
    baseline = target.stat()
    temporary = project_path(target.with_name(target.name + '.wheel-sources.part'))
    try:
        shutil.copyfile(target, temporary)
        with zipfile.ZipFile(temporary, 'a', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for relative, file in sorted(payload.items()):
                archive.write(file, relative)
        with zipfile.ZipFile(temporary) as archive:
            for relative, file in payload.items():
                with archive.open(relative) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != digest(file):
                        raise ValueError('Appended corresponding source differs from its verified input')
        current = target.stat()
        if (current.st_size, current.st_mtime_ns) != (baseline.st_size, baseline.st_mtime_ns):
            raise ValueError('Base source ZIP changed while adding wheel sources')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Only verify fixed cached sources; do not download')
    parser.add_argument('--append-zip', type=Path, help='Atomically append to the existing Linux source ZIP')
    args = parser.parse_args()
    lock = load_lock()
    check_wheel_bindings(lock)
    for entry in lock['files']:
        file = fetch(entry, check=args.check)
        print('wheel corresponding source verified:', entry['path'], file.stat().st_size, flush=True)
    if args.append_zip:
        target = append_zip(lock, args.append_zip)
        print('combined corresponding source ZIP:', target.name, target.stat().st_size, digest(target), flush=True)


if __name__ == '__main__':
    main()
