"""Prepare public, hash-checked ARM64 install archives; never read a phone instance.

Run stages on the build computer. The phone only consumes the frozen manifest.
Ubuntu metadata keeps upstream signatures; corresponding source is a separate ZIP.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.parser import BytesParser
from functools import cmp_to_key
import argparse
import hashlib
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# The private Windows embeddable Python intentionally omits the script directory
# from sys.path. Only add this project's own tools for the verification helper.
if str(ROOT / 'tools') not in sys.path:
    sys.path.insert(0, str(ROOT / 'tools'))
BUNDLE = ROOT / '.tools/offline-bundle'
WORK = ROOT / '.tools/offline-work'
SOURCES = ROOT / '.tools/offline-sources'
UBUNTU = 'https://ports.ubuntu.com/ubuntu-ports/'
SUITES = ('noble', 'noble-updates', 'noble-security')
COMPONENTS = ('main', 'universe')
APT_ROOTS = ('python3 python3-venv python3-pip ca-certificates curl unzip xz-utils '
             'xvfb xauth ffmpeg procps dbus libnss3 libgtk-3-0t64 libasound2t64 '
             'libgbm1 libxss1 libxtst6 libatk-bridge2.0-0t64 libdrm2 libxcomposite1 '
             'libxrandr2 libxdamage1 libnotify4 libsecret-1-0 xdg-utils '
             'libgssapi-krb5-2 fonts-wqy-microhei tzdata').split()


def digest(file):
    with Path(file).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_child(base, relative):
    part = PurePosixPath(relative)
    if not relative or '\\' in relative or part.is_absolute() or any(p in ('', '.', '..') for p in relative.split('/')):
        raise ValueError('Unsafe public archive path')
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError('Archive path outside this project')
    return target


def download(url, target, expected=None, size=None, refresh=False):
    target = Path(target)
    if not target.resolve().is_relative_to(ROOT):
        raise ValueError('Downloads must stay inside this project')
    if not url.startswith('https://'):
        raise ValueError('Public build inputs require HTTPS')
    if target.is_file() and not refresh and (size is None or target.stat().st_size == size):
        if expected is None or digest(target) == expected:
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.part')
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Dafeiyu-Mobile-Bundle/1'})
            total = 0
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=45) as response, temporary.open('wb') as output:
                if not response.geturl().startswith('https://'):
                    raise ValueError('Public download redirected to non-HTTPS')
                while block := response.read1(256 * 1024):
                    if time.monotonic() - started > 900:
                        raise TimeoutError('Public download exceeded bounded total time')
                    total += len(block)
                    if total > (size if size is not None else 600 * 1024 * 1024):
                        raise ValueError('Public archive exceeds expected size')
                    output.write(block)
                    if total // (16 * 1024 * 1024) != (total - len(block)) // (16 * 1024 * 1024):
                        output.flush()
                        print('download progress:', target.name, total, flush=True)
            if size is not None and total != size:
                raise ValueError('Public archive size mismatch: ' + target.name)
            if expected is not None and digest(temporary) != expected:
                raise ValueError('Public archive hash mismatch: ' + target.name)
            os.replace(temporary, target)
            print('verified download:', target.name, total, flush=True)
            return target
        except (OSError, urllib.error.URLError, ValueError):
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(1)


def paragraphs(text):
    item = {}
    key = None
    for line in text.splitlines() + ['']:
        if not line:
            if item:
                yield item
            item, key = {}, None
        elif line[0].isspace() and key:
            item[key] += '\n' + line[1:]
        elif ':' in line:
            key, value = line.split(':', 1)
            item[key] = value.lstrip()


def version_compare(left, right):
    """Debian Policy version ordering: epochs, tilde, letters, numeric runs."""
    def split(value):
        epoch, sep, rest = value.partition(':')
        if not sep:
            epoch, rest = '0', value
        upstream, sep, revision = rest.rpartition('-')
        return int(epoch), upstream if sep else rest, revision if sep else '0'

    def order(ch):
        if ch == '~':
            return -1
        if not ch:
            return 0
        return ord(ch) if ch.isalpha() else ord(ch) + 256

    def part(a, b):
        while a or b:
            while (a and not a[0].isdigit()) or (b and not b[0].isdigit()):
                ac = a[0] if a and not a[0].isdigit() else ''
                bc = b[0] if b and not b[0].isdigit() else ''
                if order(ac) != order(bc):
                    return -1 if order(ac) < order(bc) else 1
                a, b = a[bool(ac):], b[bool(bc):]
            am = re.match(r'\d*', a)[0]
            bm = re.match(r'\d*', b)[0]
            ai, bi = am.lstrip('0'), bm.lstrip('0')
            if len(ai) != len(bi):
                return -1 if len(ai) < len(bi) else 1
            if ai != bi:
                return -1 if ai < bi else 1
            a, b = a[len(am):], b[len(bm):]
        return 0

    a, b = split(left), split(right)
    if a[0] != b[0]:
        return -1 if a[0] < b[0] else 1
    return part(a[1], b[1]) or part(a[2], b[2])


def dependency(term):
    match = re.fullmatch(r'\s*([a-z0-9+.-]+)(?::(?:any|native|arm64))?\s*(?:\((<<|<=|=|>=|>>)\s*([^()]+)\))?\s*', term)
    if not match:
        raise ValueError('Unsupported binary dependency syntax: ' + term)
    return match.groups()


def satisfies(version, op, required):
    if not op:
        return True
    result = version_compare(version, required)
    return {'<<': result < 0, '<=': result <= 0, '=': result == 0, '>=': result >= 0, '>>': result > 0}[op]


def source_identity(item):
    match = re.fullmatch(r'([^ ()]+)(?: \(([^()]+)\))?', item.get('Source') or item['Package'])
    if not match:
        raise ValueError('Invalid source metadata')
    return match[1], match[2] or item['Version']


def base_records():
    lock = json.loads((ROOT / 'runtime/components.lock.json').read_text(encoding='utf-8'))
    base = next(c for c in lock['components'] if c['name'] == 'ubuntu-base-arm64')
    original = ROOT / '.tools/runtime-downloads/base-arm64.tar.gz'
    download(base['source'], original, base['sha256'], base['size'])
    with tarfile.open(original, 'r:gz') as archive:
        wanted = {}
        for member in archive:
            name = member.name.removeprefix('./')
            if name in ('var/lib/dpkg/status', 'usr/share/keyrings/ubuntu-archive-keyring.gpg'):
                wanted[name] = archive.extractfile(member).read()
        if len(wanted) != 2:
            raise ValueError('Ubuntu base misses package status or archive keyring')
    WORK.mkdir(parents=True, exist_ok=True)
    keyring = WORK / 'ubuntu-archive-keyring.gpg'
    keyring.write_bytes(wanted['usr/share/keyrings/ubuntu-archive-keyring.gpg'])
    return base, list(paragraphs(wanted['var/lib/dpkg/status'].decode())), keyring


def verified_index(suite, component, kind, keyring):
    release_path = 'apt/dists/' + suite + '/InRelease'
    release = download(UBUNTU + 'dists/' + suite + '/InRelease', safe_child(BUNDLE, release_path))
    gpg = Path('C:/Program Files/Git/usr/bin/gpg.exe') if os.name == 'nt' else Path(shutil.which('gpgv') or '')
    if not gpg.is_file():
        raise ValueError('GPG verification tool required; do not skip official signatures')
    home = WORK / 'gnupg'
    home.mkdir(exist_ok=True)
    if os.name == 'nt':
        command = [str(gpg), '--batch', '--no-tty', '--no-options', '--homedir', './' + home.relative_to(ROOT).as_posix(), '--no-default-keyring',
                   '--keyring', './' + keyring.relative_to(ROOT).as_posix(), '--no-auto-key-retrieve', '--no-auto-check-trustdb',
                   '--lock-never', '--decrypt', './' + release.relative_to(ROOT).as_posix()]
    else:
        command = [str(gpg), '--keyring', str(keyring), '--output', '-', str(release)]
    if not release.read_bytes().startswith(b'-----BEGIN PGP SIGNED MESSAGE-----\n'):
        raise ValueError('Ubuntu signed metadata has an unsigned prefix or invalid armor')
    checked = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=30)
    if checked.returncode:
        raise ValueError('Official Ubuntu InRelease signature check failed: ' + suite)
    rel = component + ('/binary-arm64/Packages.xz' if kind == 'binary' else '/source/Sources.xz')
    signed_text = checked.stdout.decode('utf-8')
    line = next((line.split() for line in signed_text.splitlines()
                 if re.fullmatch(r' [a-f0-9]{64}\s+\d+\s+' + re.escape(rel), line)), None)
    if not line:
        raise ValueError('Signed Ubuntu metadata misses ' + rel)
    target = safe_child(BUNDLE, 'apt/dists/' + suite + '/' + rel) if kind == 'binary' else safe_child(WORK, 'source-index/' + suite + '/' + rel)
    download(UBUNTU + 'dists/' + suite + '/' + rel, target, line[0], int(line[1]))
    return list(paragraphs(lzma.decompress(target.read_bytes()).decode('utf-8')))


def resolve_packages(records, installed, roots):
    choices, providers = {}, {}
    current = {x['Package']: x for x in installed if x.get('Status') == 'install ok installed'}
    unique = {}
    for record in list(records) + list(current.values()):
        if record.get('Architecture') not in ('arm64', 'all'):
            continue
        unique.setdefault((record['Package'], record['Version']), record)
    for record in unique.values():
        choices.setdefault(record['Package'], []).append(record)
        for provided in record.get('Provides', '').split(','):
            if provided.strip():
                name, op, value = dependency(provided)
                providers.setdefault(name, []).append((record, value if op == '=' else None))
    for name in choices:
        choices[name].sort(key=cmp_to_key(lambda a, b: version_compare(b['Version'], a['Version'])))

    def matches(item, name, op, value):
        if item['Package'] == name:
            return satisfies(item['Version'], op, value)
        for provided in item.get('Provides', '').split(','):
            if not provided.strip():
                continue
            pn, po, pv = dependency(provided)
            if pn == name and (not op or (po == '=' and satisfies(pv, op, value))):
                return True
        return False

    def candidates(group):
        options = [dependency(term) for term in group.split('|')]
        result, seen = [], set()
        for name, op, value in options:
            options_for_name = choices.get(name, []) + [item for item, version in providers.get(name, [])]
            base = current.get(name)
            if base:
                options_for_name = [base] + options_for_name
            for item in options_for_name:
                if not matches(item, name, op, value):
                    continue
                identity = item['Package'], item['Version']
                if identity not in seen:
                    result.append(item)
                    seen.add(identity)
        return result

    def conflict(item, selected):
        for other in selected.values():
            if item['Package'] == other['Package']:
                continue
            for first, second in ((item, other), (other, item)):
                for field in ('Conflicts', 'Breaks'):
                    if any(matches(second, *dependency(term)) for term in first.get(field, '').split(',') if term.strip()):
                        return True
        return False

    visited = 0
    failure = ['']
    def search(selected, pending):
        nonlocal visited
        while pending:
            group, *rest = pending
            if any(matches(item, *dependency(term)) for term in group.split('|') for item in selected.values()):
                pending = rest
            else:
                break
        if not pending:
            return selected
        visited += 1
        if visited > 100000:
            raise ValueError('APT closure search exceeded bounded budget')
        group, *rest = pending
        failure[0] = group
        for item in candidates(group):
            if item['Package'] in selected or conflict(item, selected):
                continue
            updated = {**selected, item['Package']: item}
            dependencies = [part.strip() for field in ('Pre-Depends', 'Depends')
                            for part in item.get(field, '').split(',') if part.strip()]
            result = search(updated, dependencies + rest)
            if result is not None:
                return result
        return None

    # apt install names chooses the highest repository/installed candidate. Keep
    # every base package present, and validate reverse dependencies as well.
    requirements = []
    for name in roots:
        if name not in choices:
            raise ValueError('Missing ARM64 root package: ' + name)
        requirements.append(name + ' (= ' + choices[name][0]['Version'] + ')')
    requirements += sorted(current)
    final = search({}, requirements)
    if final is None:
        raise ValueError('ARM64 dependency closure cannot satisfy: ' + failure[0])
    return sorted([item for name, item in final.items() if 'Filename' in item and
                   (name not in current or current[name]['Version'] != item['Version'])], key=lambda x: x['Package'])


def parallel_download(items):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(download, *item) for item in items]
        for job in as_completed(jobs):
            job.result()


def source_descriptor_path(name, version, filename):
    return safe_child(WORK, 'old-source-descriptors/' + name + '/' + version.replace(':', '_') + '/' + filename)


def source_pool_file_urls(record):
    """Map exact signed source identities to the official Ubuntu pool only."""
    directory = record['Directory']
    if not directory.startswith('pool/'):
        raise ValueError('Signed source index has a non-pool source directory')
    safe_child(SOURCES, directory)
    urls = {}
    for line in record['Checksums-Sha256'].strip().splitlines():
        parts = line.split()
        if (len(parts) != 3 or not re.fullmatch(r'[a-f0-9]{64}', parts[0]) or
                not re.fullmatch(r'[0-9]+', parts[1]) or int(parts[1]) <= 0 or
                PurePosixPath(parts[2]).name != parts[2]):
            raise ValueError('Signed source index has an invalid source file identity')
        sha, size, filename = parts
        safe_child(SOURCES, directory + '/' + filename)
        urls[(sha, int(size), filename)] = UBUNTU + directory + '/' + filename
    return urls


def validate_bundle_paths(actual, expected):
    if set(actual) != set(expected):
        raise ValueError('Bundle files differ from resolved public inputs; missing=' + str(sorted(set(expected) - set(actual))) + '; extra=' + str(sorted(set(actual) - set(expected))))


def write_json(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def apt_stage():
    base, installed, keyring = base_records()
    records = []
    for suite in SUITES:
        for component in COMPONENTS:
            records += verified_index(suite, component, 'binary', keyring)
    packages = resolve_packages(records, installed, APT_ROOTS)
    print('ARM64 closure:', len(packages), 'deb archives;', sum(int(p['Size']) for p in packages), 'bytes', flush=True)
    write_json(WORK / 'apt-plan.json', {'roots': APT_ROOTS, 'basePackages': installed, 'packages': packages})
    parallel_download([(UBUNTU + p['Filename'], safe_child(BUNDLE, 'apt/' + p['Filename']), p['SHA256'], int(p['Size'])) for p in packages])
    target = BUNDLE / 'base/base-arm64.tar.gz'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / '.tools/runtime-downloads/base-arm64.tar.gz', target)
    (BUNDLE / 'apt/sources.list').write_text(''.join('deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] file:/opt/offline/apt ' + suite + ' main universe\n' for suite in SUITES), encoding='utf-8', newline='\n')


def wheel_records(directory):
    from packaging.tags import cpython_tags, compatible_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    platforms = ['manylinux_2_%d_aarch64' % n for n in range(39, 16, -1)] + ['manylinux2014_aarch64', 'linux_aarch64']
    target_tags = set(cpython_tags((3, 12), abis=['cp312'], platforms=platforms))
    target_tags.update(compatible_tags((3, 12), interpreter='cp312', platforms=platforms))
    records = {}
    for file in directory.glob('*.whl'):
        filename_name, filename_version, _, tags = parse_wheel_filename(file.name)
        if not tags.intersection(target_tags):
            raise ValueError('Wheel is incompatible with Linux ARM64 CPython3.12: ' + file.name)
        with zipfile.ZipFile(file) as archive:
            name = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))
            metadata = BytesParser().parsebytes(archive.read(name))
        key = canonicalize_name(metadata['Name'])
        if key != filename_name or str(filename_version) != metadata['Version']:
            raise ValueError('Wheel filename and distribution metadata differ')
        if key in records:
            raise ValueError('Multiple wheel versions for ' + key)
        records[key] = {'file': file, 'name': metadata['Name'], 'version': metadata['Version'],
                        'requires': metadata.get_all('Requires-Dist') or [],
                        'license': metadata.get('License-Expression') or (metadata.get('License') or 'See preserved wheel license files')[:300]}
    return records


def linux_requirements(records, roots):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    environment = dict(os_name='posix', sys_platform='linux', platform_machine='aarch64',
                       platform_system='Linux', python_version='3.12', python_full_version='3.12.3',
                       implementation_name='cpython', implementation_version='3.12.3',
                       platform_python_implementation='CPython', platform_release='', platform_version='')
    extras, pending, missing, visited = {}, [Requirement(r) for r in roots], [], set()
    while pending:
        req = pending.pop()
        key = canonicalize_name(req.name)
        extra = extras.setdefault(key, set())
        extra.update(req.extras)
        item = records.get(key)
        if item is None or item['version'] not in req.specifier:
            missing.append(str(req))
            continue
        state = (key, tuple(sorted(extra)))
        if state in visited:
            continue
        visited.add(state)
        for raw in item['requires']:
            child = Requirement(raw)
            if child.marker is not None and any(name in str(child.marker) for name in ('platform_release', 'platform_version')):
                raise ValueError('Dependency requires an unknown phone kernel; cannot infer it from the build computer')
            if child.marker is None or any(child.marker.evaluate({**environment, 'extra': value}) for value in ({''} | extra)):
                child.marker = None
                pending.append(child)
    return sorted(set(missing))


def wheels_stage():
    wheels = BUNDLE / 'wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    roots = [line.strip() for line in (ROOT / 'runtime/requirements.txt').read_text().splitlines() if line.strip() and not line.startswith('#')]
    roots += ['setuptools', 'wheel']
    # This tiny upstream dependency only publishes a pure-Python sdist. Build a
    # universal wheel on the build computer, never compile/download on a phone.
    pure_info_path = WORK / 'qrcode-terminal-source.json'
    if not pure_info_path.is_file():
        info = json.loads(urllib.request.urlopen('https://pypi.org/pypi/qrcode-terminal/json', timeout=30).read())
        release = next(x for x in info['urls'] if x['packagetype'] == 'sdist')
        pure_info = {'name': 'qrcode-terminal', 'version': info['info']['version'], 'file': release['filename'],
                     'url': release['url'], 'sha256': release['digests']['sha256']}
        write_json(pure_info_path, pure_info)
    pure_info = json.loads(pure_info_path.read_text(encoding='utf-8'))
    pure_source = download(pure_info['url'], safe_child(WORK, pure_info['file']), pure_info['sha256'])
    previous_built = list(wheels.glob('qrcode_terminal-*.whl'))
    if not previous_built:
        temporary = WORK / 'pip-temp'
        temporary.mkdir(exist_ok=True)
        environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--disable-pip-version-check', '--cache-dir', str(ROOT / '.tools/pip-cache'), '--no-deps',
                        '--no-build-isolation', '--wheel-dir', str(wheels), str(pure_source)],
                       cwd=ROOT, env=environment, check=True)
    command = [sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check', '--cache-dir', str(ROOT / '.tools/pip-cache'), '--dest', str(wheels),
               '--find-links', str(wheels), '--only-binary=:all:', '--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312']
    for platform in ['manylinux_2_%d_aarch64' % n for n in range(39, 16, -1)] + ['manylinux2014_aarch64', 'linux_aarch64']:
        command += ['--platform', platform]
    subprocess.run(command + roots, cwd=ROOT, check=True)
    for _ in range(8):
        records = wheel_records(wheels)
        missing = linux_requirements(records, roots)
        if not missing:
            break
        print('Completing Linux target marker dependencies:', ', '.join(missing), flush=True)
        constraints = WORK / 'wheel-constraints.txt'
        constraints.parent.mkdir(parents=True, exist_ok=True)
        constraints.write_text(''.join(p['name'] + '==' + p['version'] + '\n' for p in records.values()), encoding='utf-8')
        subprocess.run(command + ['--constraint', str(constraints)] + missing, cwd=ROOT, check=True)
    else:
        raise ValueError('Linux target wheel dependency closure did not converge')
    entries = []
    for record in wheel_records(wheels).values():
        meta_file = safe_child(WORK, 'pypi/' + record['name'] + '/' + record['version'] + '.json')
        download('https://pypi.org/pypi/' + urllib.parse.quote(record['name']) + '/' + urllib.parse.quote(record['version']) + '/json', meta_file)
        info = json.loads(meta_file.read_text(encoding='utf-8'))
        release = next((x for x in info['urls'] if x['filename'] == record['file'].name), None)
        locally_built = record['name'].replace('_', '-').lower() == pure_info['name'] and record['version'] == pure_info['version']
        if not locally_built and (release is None or digest(record['file']) != release['digests']['sha256']):
            raise ValueError('Wheel differs from official PyPI digest')
        entries.append({'path': 'wheels/' + record['file'].name, 'name': record['name'], 'version': record['version'],
                        'source': pure_info['url'] if locally_built else release['url'], 'license': record['license'],
                        **({'generated': True, 'sourceSha256': pure_info['sha256'], 'buildRecipe': 'pip wheel --no-deps --no-build-isolation; pure Python universal wheel'} if locally_built else {})})
    write_json(WORK / 'wheels-plan.json', {'roots': roots, 'files': entries})
    print('Linux ARM64 cp312 wheel closure verified:', len(entries), flush=True)


def public_archives_stage():
    lock = json.loads((ROOT / 'runtime/qq-components.lock.json').read_text(encoding='utf-8'))['napcat']
    target = BUNDLE / 'archives/napcat.zip'
    cached = ROOT / '.tools/downloads/napcat-complete.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    if cached.is_file() and digest(cached) == lock['sha256']:
        shutil.copyfile(cached, target)
    else:
        download(lock['url'], target, lock['sha256'], lock['size'])
    model = json.loads((ROOT / 'runtime/asr-model.lock.json').read_text(encoding='utf-8'))
    download(model['url'], BUNDLE / 'models/sensevoice.tar.bz2', model['sha256'], model['size'])


def sources_stage():
    plan = json.loads((WORK / 'apt-plan.json').read_text(encoding='utf-8'))
    _, _, keyring = base_records()
    candidates, pool_files = {}, {}
    for suite in SUITES:
        for component in COMPONENTS:
            for record in verified_index(suite, component, 'source', keyring):
                candidates[(record['Package'], record['Version'])] = record
                for identity, url in source_pool_file_urls(record).items():
                    pool_files.setdefault(identity, url)
    needed = sorted({source_identity(p) for p in plan['basePackages'] + plan['packages']})
    source_files, packages = {}, []
    for name, version in needed:
        descriptor_url = None
        record = candidates.get((name, version))
        if record is None:
            # Official base images can contain an older source version no longer
            # advertised by current Packages/Sources; Launchpad retains that build.
            filename = name + '_' + version.split(':', 1)[-1] + '.dsc'
            descriptor_url = ('https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/' +
                   urllib.parse.quote(name) + '/' + urllib.parse.quote(version, safe='') + '/' + urllib.parse.quote(filename))
            dsc = download(descriptor_url, source_descriptor_path(name, version, filename))
            from offline_source_verify import verify_source_descriptor
            record = verify_source_descriptor(dsc, name, version, WORK)
            record['Directory'] = descriptor_url.rsplit('/', 1)[0]
        package_paths = []
        for line in record['Checksums-Sha256'].strip().splitlines():
            sha, size, filename = line.split()
            directory = record['Directory']
            url = (directory.rstrip('/') + '/' + filename) if directory.startswith('https://') else UBUNTU + directory + '/' + filename
            if descriptor_url is not None:
                # Historical DSCs often share their unmodified orig archive
                # with newer signed Sources entries. Reuse only an exact
                # digest+size+filename match; versioned patches stay unchanged.
                url = pool_files.get((sha, int(size), filename), url)
            relative = 'sources/' + name + '/' + version.replace(':', '_') + '/' + filename
            source_files[relative] = {'path': relative, 'source': url, 'size': int(size), 'sha256': sha}
            package_paths.append(relative)
        # Source indices include the .dsc; a fallback descriptor lists only its
        # payloads, so preserve that signed descriptor as a separate source file.
        if descriptor_url is not None:
            relative = 'sources/' + name + '/' + version.replace(':', '_') + '/' + dsc.name
            source_files[relative] = {'path': relative, 'source': descriptor_url,
                                      'size': dsc.stat().st_size, 'sha256': digest(dsc)}
            safe_child(SOURCES, relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dsc, safe_child(SOURCES, relative))
            package_paths.append(relative)
        packages.append({'name': name, 'version': version, 'files': package_paths})
    binaries = []
    for origin, entries in (('base', plan['basePackages']), ('deb', plan['packages'])):
        for item in entries:
            source_name, source_version = source_identity(item)
            binaries.append({'name': item['Package'], 'version': item['Version'], 'architecture': item.get('Architecture'),
                             'sourceName': source_name, 'sourceVersion': source_version, 'origin': origin})
    write_json(ROOT / 'runtime/linux-sources.lock.json', {'schemaVersion': 1, 'binaries': binaries, 'packages': packages, 'files': sorted(source_files.values(), key=lambda x: x['path'])})
    print('Corresponding Linux sources:', len(packages), 'packages,', len(source_files), 'files,', sum(x['size'] for x in source_files.values()), 'bytes', flush=True)
    parallel_download([(e['source'], safe_child(SOURCES, e['path']), e['sha256'], e['size']) for e in source_files.values()])


def freeze_stage():
    plan = json.loads((WORK / 'apt-plan.json').read_text(encoding='utf-8'))
    wheels = json.loads((WORK / 'wheels-plan.json').read_text(encoding='utf-8'))
    base = next(c for c in json.loads((ROOT / 'runtime/components.lock.json').read_text(encoding='utf-8'))['components'] if c['name'] == 'ubuntu-base-arm64')
    qq = json.loads((ROOT / 'runtime/qq-components.lock.json').read_text(encoding='utf-8'))
    model = json.loads((ROOT / 'runtime/asr-model.lock.json').read_text(encoding='utf-8'))
    meta = {x['path']: x for x in wheels['files']}
    meta['base/base-arm64.tar.gz'] = {'source': base['source'], 'license': 'Ubuntu package-specific licenses; corresponding sources separately supplied'}
    meta['archives/napcat.zip'] = {'source': qq['napcat']['url'], 'license': 'NapCat Limited Redistribution License; preserved complete official distribution'}
    meta['models/sensevoice.tar.bz2'] = {'source': model['url'], 'license': model['license']}
    for package in plan['packages']:
        meta['apt/' + package['Filename']] = {'source': UBUNTU + package['Filename'], 'package': package['Package'], 'version': package['Version'],
                                              'sourcePackage': source_identity(package)[0], 'license': 'Preserved /usr/share/doc package copyright; corresponding sources separately supplied'}
    for suite in SUITES:
        meta['apt/dists/' + suite + '/InRelease'] = {'source': UBUNTU + 'dists/' + suite + '/InRelease', 'license': 'Ubuntu signed archive metadata'}
        for component in COMPONENTS:
            rel = 'dists/' + suite + '/' + component + '/binary-arm64/Packages.xz'
            meta['apt/' + rel] = {'source': UBUNTU + rel, 'license': 'Ubuntu signed archive metadata'}
    meta['apt/sources.list'] = {'source': UBUNTU, 'generated': True, 'license': 'Project-generated local-only APT source configuration'}
    actual = {p.relative_to(BUNDLE).as_posix() for p in BUNDLE.rglob('*') if p.is_file() and p.name != 'manifest.json'}
    validate_bundle_paths(actual, meta)
    if b'\r' in (BUNDLE / 'apt/sources.list').read_bytes():
        raise ValueError('Generated Linux APT source configuration must use LF newlines')
    entries = []
    for relative in sorted(meta):
        file = safe_child(BUNDLE, relative)
        if relative.endswith('.part'):
            raise ValueError('Unfinished bundle download')
        extra = dict(meta[relative])
        extra.pop('path', None)
        if relative == 'apt/sources.list':
            extra = {'source': UBUNTU, 'generated': True, 'license': 'Project-generated local-only APT source configuration'}
        entries.append({'path': relative, 'size': file.stat().st_size, 'sha256': digest(file), **extra})
    entries.sort(key=lambda x: x['path'])
    manifest = {'schemaVersion': 1, 'architecture': 'aarch64', 'ubuntuRelease': 'noble', 'python': '3.12',
                'qqDownloadRequired': True, 'aptRoots': plan['roots'], 'totalBytes': sum(e['size'] for e in entries),
                'files': entries, 'sha256': hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}
    write_json(BUNDLE / 'manifest.json', manifest)
    write_json(ROOT / 'runtime/offline-manifest.json', manifest)
    print('Frozen offline bundle:', len(entries), 'files,', manifest['totalBytes'], 'bytes', flush=True)


def source_zip_stage():
    lock = json.loads((ROOT / 'runtime/linux-sources.lock.json').read_text(encoding='utf-8'))
    version = dict(line.split('=', 1) for line in (ROOT / 'version.properties').read_text().splitlines() if '=' in line)['versionName']
    target = ROOT / 'dist' / ('dafeiyu-mobile-' + version + '-linux-sources.zip')
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.write(ROOT / 'runtime/linux-sources.lock.json', 'linux-sources.lock.json')
        archive.writestr('BUILDING.txt', 'Unmodified Ubuntu source packages corresponding to the embedded base and additional ARM64 deb archives.\nSee linux-sources.lock.json for exact source and binary identities.\nExtract each .dsc with dpkg-source -x, install its declared build dependencies in Ubuntu noble, then run dpkg-buildpackage.\nOriginal upstream archives, Debian/Ubuntu patches and build rules are retained. This archive is not required on the phone.\n')
        for entry in lock['files']:
            file = safe_child(SOURCES, entry['path'])
            if not file.is_file() or file.stat().st_size != entry['size'] or digest(file) != entry['sha256']:
                raise ValueError('Missing corresponding source: ' + entry['path'])
            archive.write(file, entry['path'])
        pure = json.loads((WORK / 'qrcode-terminal-source.json').read_text(encoding='utf-8'))
        pure_file = safe_child(WORK, pure['file'])
        if digest(pure_file) != pure['sha256']:
            raise ValueError('Locally built pure wheel source hash mismatch')
        archive.write(pure_file, 'python-source/' + pure['file'])
        archive.writestr('python-source/BUILDING.txt', 'qrcode-terminal==' + pure['version'] + '\nOriginal source: ' + pure['url'] + '\nSHA256: ' + pure['sha256'] + '\nBuild a pure-Python universal wheel on the build computer with: python -m pip wheel --no-deps --no-build-isolation --wheel-dir wheels ' + pure['file'] + '\nThe phone installs only the resulting wheel, never the sdist. Original author header and MIT declaration are preserved; see runtime/licenses/qrcode-terminal-0.8-MIT-NOTICE.txt in the project source.\n')
    print('Corresponding Linux sources ZIP:', target.name, target.stat().st_size, digest(target), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('apt', 'wheels', 'archives', 'sources', 'freeze', 'source-zip', 'all'), default='all')
    args = parser.parse_args()
    BUNDLE.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    stages = {'apt': apt_stage, 'wheels': wheels_stage, 'archives': public_archives_stage,
              'sources': sources_stage, 'freeze': freeze_stage, 'source-zip': source_zip_stage}
    if args.stage == 'all':
        for stage in stages.values():
            stage()
    else:
        stages[args.stage]()


if __name__ == '__main__':
    main()
