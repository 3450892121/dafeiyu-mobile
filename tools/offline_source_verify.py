"""Verify public Ubuntu clear-signed metadata without a user's GPG keyring.

Only GPG's successfully verified plaintext is returned.  In particular, callers
must not verify a file and then parse its original, possibly unsigned, prefix.
Historical source descriptors come from their fixed official Launchpad URL;
the Ubuntu public keyserver supplies missing signing keys, not source content.
"""
import base64
import binascii
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
GIT_GPG = Path('C:/Program Files/Git/usr/bin/gpg.exe')
KEYSERVER_LOOKUP = 'https://keyserver.ubuntu.com/pks/lookup'
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_STATUS_BYTES = 256 * 1024
MAX_PUBLIC_KEY_BYTES = 2 * 1024 * 1024
_BAD_STATUS = {'BADSIG', 'REVKEYSIG', 'EXPKEYSIG', 'EXPSIG', 'KEYREVOKED',
               'KEYEXPIRED', 'SIGEXPIRED', 'NODATA', 'BADARMOR', 'UNEXPECTED',
               'DECRYPTION_FAILED', 'ERROR', 'TRUST_NEVER'}


def _project_path(value, *, directory=False):
    """Reject links before resolving them, and never touch another project."""
    path = Path(os.path.abspath(value))
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        raise ValueError('Signature verification paths must stay in this project') from None
    if relative.parts and relative.parts[0] in {'.git', '.local', '.agents', '.codex'}:
        raise ValueError('Signature verification cannot use private project state')
    current = ROOT
    for part in relative.parts:
        current /= part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Signature verification cannot follow filesystem links')
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError('Signature verification path escaped the project')
    if path.exists() and directory is not None and (not path.is_dir() if directory else not path.is_file()):
        raise ValueError('Signature verification path has the wrong file type')
    return path


def _argument(path):
    # Git's MSYS GPG must receive relative POSIX paths, including with a Chinese
    # project directory.  A Windows drive path can be misinterpreted as a URI.
    return './' + path.relative_to(ROOT).as_posix()


def _gpg():
    executable = GIT_GPG if os.name == 'nt' else Path(shutil.which('gpg') or '')
    if not executable.is_file():
        raise ValueError('GPG is required; public metadata signatures cannot be skipped')
    return executable


def _check_home(home):
    """This directory contains only public verification state, never secrets."""
    _project_path(home, directory=True)
    for child in home.iterdir():
        _project_path(child, directory=None)
        if (not child.is_file() and not child.is_dir() and
                not (child.name == 'S.dirmngr' and child.is_socket())):
            raise ValueError('Unexpected special file in isolated GPG home')
    for filename in ('gpg.conf', 'common.conf', 'dirmngr.conf', 'gpg-agent.conf',
                     'scdaemon.conf', 'secring.gpg', 'secring.gpg.lock'):
        if (home / filename).exists():
            raise ValueError('Unexpected configuration or secret state in isolated GPG home')
    for dirname in ('private-keys-v1.d', 'openpgp-revocs.d'):
        folder = home / dirname
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            raise ValueError('Private keys are forbidden in the public verification home')


def _envelope(data):
    """Bound one complete clear-signed message; reject all unsigned garbage."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Clear-signed metadata must be UTF-8 text') from None
    text = text.replace('\r\n', '\n')
    if '\r' in text or '\x00' in text:
        raise ValueError('Invalid characters in clear-signed metadata')
    lines = text.split('\n')
    if not lines or lines[0] != '-----BEGIN PGP SIGNED MESSAGE-----':
        raise ValueError('Clear-signed metadata has an unsigned prefix or no signature')
    for marker in ('-----BEGIN PGP SIGNED MESSAGE-----',
                   '-----BEGIN PGP SIGNATURE-----', '-----END PGP SIGNATURE-----'):
        if lines.count(marker) != 1:
            raise ValueError('Expected exactly one complete clear-signed message')
    start = lines.index('-----BEGIN PGP SIGNATURE-----')
    end = lines.index('-----END PGP SIGNATURE-----')
    if start >= end or any(line for line in lines[end + 1:]):
        raise ValueError('Clear-signed metadata has unsigned trailing content')
    try:
        separator = lines.index('', 1, start)
    except ValueError:
        raise ValueError('Clear-signed metadata has no cleartext boundary') from None
    headers = lines[1:separator]
    if not headers or any(not re.fullmatch(r'Hash: [A-Za-z0-9, -]+', line) for line in headers):
        raise ValueError('Unexpected clear-sign armor headers')
    if separator + 1 >= start:
        raise ValueError('Clear-signed metadata is empty')


def _statuses(file):
    if not file.is_file() or file.stat().st_size > MAX_STATUS_BYTES:
        raise ValueError('GPG did not return bounded signature status')
    try:
        text = file.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        raise ValueError('GPG signature status is malformed') from None
    result = []
    for line in text.splitlines():
        if not line:
            continue
        if not line.startswith('[GNUPG:] '):
            raise ValueError('GPG signature status is malformed')
        fields = line[len('[GNUPG:] '):].split()
        if not fields:
            raise ValueError('GPG signature status is empty')
        result.append(fields)
    return result


def _valid(statuses, returncode):
    if returncode or any(row[0] in _BAD_STATUS | {'ERRSIG', 'NO_PUBKEY', 'FAILURE'} for row in statuses):
        return False
    good = [row for row in statuses if row[0] == 'GOODSIG']
    valid = [row for row in statuses if row[0] == 'VALIDSIG']
    return bool(good and valid and len(good) == len(valid) and all(
        len(row) >= 10 and re.fullmatch(r'(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})', row[1])
        for row in valid))


def _missing_keys(statuses):
    if any(row[0] in _BAD_STATUS for row in statuses):
        return []
    errors = [row for row in statuses if row[0] == 'ERRSIG']
    missing = [row for row in statuses if row[0] == 'NO_PUBKEY']
    if (not errors or not missing or len(missing) > 4 or
            any(len(row) < 7 or row[6] != '9' for row in errors) or
            any(len(row) != 2 or not re.fullmatch(r'[A-Fa-f0-9]{16}', row[1]) for row in missing)):
        return []
    keys = sorted({row[1].upper() for row in missing})
    if any(row[1].upper() not in keys for row in errors):
        return []
    return keys


def _public_key_armor(data):
    """Reject secret/encrypted/compressed packets before invoking GPG import.

    The ASCII label alone is insufficient: a secret-key packet can be placed
    inside a block labelled PUBLIC KEY. Packet framing follows RFC 9580 4.2;
    cryptographic parsing and signature validation remain GPG's job.
    """
    try:
        lines = data.decode('ascii').replace('\r\n', '\n').split('\n')
    except UnicodeDecodeError:
        raise ValueError('Official keyserver returned non-ASCII public key data') from None
    while lines and not lines[-1]:
        lines.pop()
    begin, end = '-----BEGIN PGP PUBLIC KEY BLOCK-----', '-----END PGP PUBLIC KEY BLOCK-----'
    if (not lines or lines[0] != begin or lines[-1] != end or
            lines.count(begin) != 1 or lines.count(end) != 1):
        raise ValueError('Official keyserver did not return exactly one public key block')
    try:
        boundary = lines.index('', 1)
    except ValueError:
        raise ValueError('Public key armor has no payload boundary') from None
    if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*: [\x20-\x7e]*', line) for line in lines[1:boundary]):
        raise ValueError('Invalid public key armor headers')
    body = lines[boundary + 1:-1]
    if body and body[-1].startswith('='):
        if not re.fullmatch(r'=[A-Za-z0-9+/]{4}', body.pop()):
            raise ValueError('Invalid public key armor checksum')
    if not body or any(not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', line) for line in body):
        raise ValueError('Invalid public key armor payload')
    try:
        packets = base64.b64decode(''.join(body), validate=True)
    except binascii.Error:
        raise ValueError('Invalid public key armor base64') from None
    offset, primary = 0, 0
    while offset < len(packets):
        header = packets[offset]
        offset += 1
        if not header & 0x80 or offset >= len(packets):
            raise ValueError('Invalid public key packet header')
        if header & 0x40:
            tag, length = header & 0x3f, packets[offset]
            offset += 1
            if 192 <= length < 224:
                if offset >= len(packets):
                    raise ValueError('Truncated public key packet length')
                length = ((length - 192) << 8) + packets[offset] + 192
                offset += 1
            elif length == 255:
                if offset + 4 > len(packets):
                    raise ValueError('Truncated public key packet length')
                length = int.from_bytes(packets[offset:offset + 4], 'big')
                offset += 4
            elif length >= 224:
                raise ValueError('Partial-length public key packets are not accepted')
        else:
            tag, length_type = (header >> 2) & 0x0f, header & 3
            if length_type == 3:
                raise ValueError('Indeterminate-length public key packets are not accepted')
            width = 1 << length_type
            if offset + width > len(packets):
                raise ValueError('Truncated public key packet length')
            length = int.from_bytes(packets[offset:offset + width], 'big')
            offset += width
        if tag not in {2, 6, 13, 14, 17}:
            raise ValueError('Only public key, identity and signature packets may be imported')
        if not primary and tag != 6:
            raise ValueError('Public key block must start with its primary public key')
        primary += tag == 6
        if not length or offset + length > len(packets) or primary > 1:
            raise ValueError('Invalid or multiple primary public key packets')
        offset += length
    if primary != 1:
        raise ValueError('Public key block has no primary public key')


class _NoKeyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise ValueError('Official Ubuntu public key lookup cannot redirect')


def _fetch_public_key(key):
    if not re.fullmatch(r'[A-F0-9]{16}', key):
        raise ValueError('Invalid public signing key identifier')
    # HKPS is the HTTPS transport of this same official lookup API. Fetching
    # directly avoids Git/MSYS dirmngr's Windows agent/IPC compatibility issues.
    url = KEYSERVER_LOOKUP + '?op=get&search=0x' + key
    request = urllib.request.Request(url, headers={'User-Agent': 'Dafeiyu-Mobile-Bundle/1'})
    try:
        opener = urllib.request.build_opener(_NoKeyRedirects())
        with opener.open(request, timeout=30) as response:
            if response.geturl() != url:
                raise ValueError('Official Ubuntu public key lookup changed source')
            data = response.read(MAX_PUBLIC_KEY_BYTES + 1)
    except (OSError, urllib.error.URLError, ValueError):
        raise ValueError('Official Ubuntu public signing key retrieval failed') from None
    if not 0 < len(data) <= MAX_PUBLIC_KEY_BYTES:
        raise ValueError('Official Ubuntu public signing key exceeds the bounded size')
    _public_key_armor(data)
    return data


def verify_clearsigned(file: Path, work: Path, keyring: Path | None = None) -> str:
    """Return only GPG-verified plaintext, or fail without returning any text.

    An explicit archive keyring is exclusive and never triggers network key
    retrieval. Source descriptor keys use only Ubuntu's fixed official HTTPS
    (HKPS) lookup, with public key storage inside ``work``. Expired, revoked or bad
    signatures are rejected.  No user/global GPG configuration is loaded.
    """
    file = _project_path(file)
    if not file.is_file() or not 0 < file.stat().st_size <= MAX_MESSAGE_BYTES:
        raise ValueError('Missing or oversized clear-signed metadata')
    original = file.read_bytes()
    _envelope(original)
    work = _project_path(work, directory=True)
    work.mkdir(parents=True, exist_ok=True)
    home = _project_path(work / 'gpg', directory=True)
    # This contains only public verification keys. Inherit the project ACL:
    # Windows Python's mode=0700 can lock out the managed build process itself.
    # Keep the basename short: Git/MSYS agent sockets have a Unix path limit.
    home.mkdir(exist_ok=True)
    _check_home(home)
    explicit_keyring = keyring is not None
    ring = _project_path(keyring) if explicit_keyring else home / 'public-keys.gpg'
    if explicit_keyring and (not ring.is_file() or ring.stat().st_size > 20 * 1024 * 1024):
        raise ValueError('Missing or oversized public archive keyring')
    ring = _project_path(ring)
    environment = dict(os.environ, GNUPGHOME=str(home))
    for name in ('GPG_AGENT_INFO', 'GPG_TTY'):
        environment.pop(name, None)
    common = [str(_gpg()), '--no-options', '--batch', '--no-tty',
              '--homedir', _argument(home), '--no-default-keyring',
              '--keyring', _argument(ring), '--no-auto-key-retrieve',
              '--no-auto-key-import', '--auto-key-locate', 'clear',
              '--no-auto-check-trustdb', '--no-use-embedded-filename']
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    def run(arguments, timeout=30):
        try:
            result = subprocess.run(common + arguments, cwd=ROOT, env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=timeout, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Public metadata signature verification tool failed') from None
        _check_home(home)
        return result

    # Distinct status files avoid stale success and keep attacker-controlled
    # cleartext out of the machine-readable signature status channel.
    for attempt in range(2):
        status = _project_path(home / ('status-' + uuid.uuid4().hex + '.txt'))
        try:
            checked = run(['--status-file', _argument(status), '--max-output',
                           str(MAX_MESSAGE_BYTES), '--output', '-', '--decrypt',
                           _argument(file)])
            states = _statuses(status)
        finally:
            status.unlink(missing_ok=True)
        if _valid(states, checked.returncode):
            if not isinstance(checked.stdout, bytes) or len(checked.stdout) > MAX_MESSAGE_BYTES:
                raise ValueError('GPG returned invalid plaintext')
            try:
                plaintext = checked.stdout.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('Verified metadata is not UTF-8') from None
            if not plaintext.strip() or '\x00' in plaintext:
                raise ValueError('Verified metadata is empty or malformed')
            # A caller must not change the cache while it is being verified.
            if file.read_bytes() != original:
                raise ValueError('Clear-signed metadata changed during verification')
            return plaintext
        missing = _missing_keys(states)
        if explicit_keyring or attempt or not missing:
            raise ValueError('Public metadata signature verification failed')
        for key in missing:
            public = _fetch_public_key(key)
            temporary = _project_path(home / ('public-import-' + uuid.uuid4().hex + '.asc'))
            try:
                temporary.write_bytes(public)
                imported = run(['--import-options', 'import-minimal', '--import', _argument(temporary)])
                if imported.returncode:
                    raise ValueError('Official Ubuntu public signing key import failed')
            finally:
                temporary.unlink(missing_ok=True)
    raise ValueError('Public metadata signature verification failed')


def _descriptor_fields(plaintext):
    fields, previous, finished = {}, None, False
    for line in plaintext.splitlines():
        if not line:
            if fields:
                finished = True
            continue
        if finished:
            raise ValueError('Source descriptor must contain exactly one paragraph')
        if line[0] in ' \t':
            if previous is None:
                raise ValueError('Malformed source descriptor continuation')
            fields[previous] += '\n' + line[1:]
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)', line)
        if not match or match[1].lower() in {name.lower() for name in fields}:
            raise ValueError('Malformed or duplicate source descriptor field')
        previous = match[1]
        fields[previous] = match[2]
    return fields


def verify_source_descriptor(file: Path, name: str, version: str, work: Path) -> dict:
    """Verify one exact historical DSC before its hashes enter the source lock."""
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name) or
            not isinstance(version, str) or
            not re.fullmatch(r'(?:[0-9]+:)?[0-9][A-Za-z0-9.+~\-]*', version)):
        raise ValueError('Invalid expected source identity')
    if Path(file).suffix != '.dsc':
        raise ValueError('Expected a source descriptor file')
    record = _descriptor_fields(verify_clearsigned(file, work))
    if record.get('Source') != name or record.get('Version') != version:
        raise ValueError('Signed source descriptor identity does not match the requested source/version')
    checksums, seen = record.get('Checksums-Sha256', '').strip().splitlines(), set()
    if not checksums:
        raise ValueError('Signed source descriptor has no SHA256 payloads')
    for line in checksums:
        parts = line.split()
        if (len(parts) != 3 or not re.fullmatch(r'[a-f0-9]{64}', parts[0]) or
                not re.fullmatch(r'[0-9]+', parts[1]) or not 0 < int(parts[1]) <= 2 * 1024 * 1024 * 1024 or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+~_-]*', parts[2]) or
                parts[2] in seen):
            raise ValueError('Signed source descriptor contains an unsafe SHA256 payload')
        seen.add(parts[2])
    return record
