"""Mock all GPG/keyserver work while exercising the public metadata consumer."""
import base64
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('offline_source_verify_tests',
                                             PROJECT / 'tools/offline_source_verify.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
FINGERPRINT = '0123456789ABCDEF0123456789ABCDEF01234567'
KEY = '89ABCDEF01234567'
PLAIN = ('Format: 3.0 (quilt)\nSource: fixture\nVersion: 2:1.0-1ubuntu1\n'
         'Checksums-Sha256:\n ' + 'a' * 64 + ' 17 fixture_1.0.orig.tar.xz\n')
GOOD = ('[GNUPG:] NEWSIG\n[GNUPG:] GOODSIG ' + KEY + ' Fixture Public Signer\n'
        '[GNUPG:] VALIDSIG ' + FINGERPRINT +
        ' 2024-01-01 1704067200 0 4 0 1 10 01 ' + FINGERPRINT + '\n')
MISSING = ('[GNUPG:] NEWSIG\n[GNUPG:] ERRSIG ' + KEY +
           ' 1 10 01 1704067200 9 ' + FINGERPRINT + '\n'
           '[GNUPG:] NO_PUBKEY ' + KEY + '\n')
PUBLIC = (b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' +
          base64.b64encode(b'\xc6\x06PUBLIC\xcd\x07fixture') +
          b'\n-----END PGP PUBLIC KEY BLOCK-----\n')


def envelope(plaintext=PLAIN):
    return ('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n' +
            plaintext + '-----BEGIN PGP SIGNATURE-----\n\n'
            'RmljdGlvbmFsIHNpZ25hdHVyZQ==\n-----END PGP SIGNATURE-----\n')


class SourceVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=PROJECT / 'build', prefix='source-verification-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'fixture_1.0-1ubuntu1.dsc'
        self.file.write_text(envelope(), encoding='utf-8')
        self.work = self.root / 'work'
        self.calls = []
        self.imported = []
        public_fetch = patch.object(verify, '_fetch_public_key', return_value=PUBLIC)
        self.fetch_public = public_fetch.start()
        self.addCleanup(public_fetch.stop)
        for item in (patch.object(verify, 'ROOT', self.root),
                     patch.object(verify, '_gpg', return_value=Path('fixture-gpg')),
                     patch.object(verify.subprocess, 'run', side_effect=self.gpg)):
            item.start()
            self.addCleanup(item.stop)
        self.responses = [(0, GOOD, PLAIN.encode())]

    def gpg(self, command, **kwargs):
        self.calls.append((command, kwargs))
        result, status, plain = self.responses.pop(0)
        if '--status-file' in command:
            (kwargs['cwd'] / command[command.index('--status-file') + 1]).write_text(status, encoding='utf-8')
        if '--import' in command:
            self.imported.append((kwargs['cwd'] / command[command.index('--import') + 1]).read_bytes())
        return subprocess.CompletedProcess(command, result, plain, b'intercepted diagnostic')

    def descriptor(self):
        return verify.verify_source_descriptor(self.file, 'fixture', '2:1.0-1ubuntu1', self.work)

    def test_verified_descriptor_preserves_epoch_and_only_uses_isolated_public_state(self):
        result = self.descriptor()
        self.assertEqual(result['Source'], 'fixture')
        self.assertEqual(result['Version'], '2:1.0-1ubuntu1')
        command, options = self.calls[0]
        for flag in ('--batch', '--no-options', '--no-tty', '--no-default-keyring',
                     '--no-auto-key-retrieve', '--no-auto-key-import', '--decrypt'):
            self.assertIn(flag, command)
        self.assertNotIn('--verify', command)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['cwd'], self.root)
        self.assertEqual(Path(options['env']['GNUPGHOME']), self.work / 'gpg')
        self.assertEqual(command[command.index('--output') + 1], '-')
        self.assertFalse(list((self.work / 'gpg').glob('status-*.txt')))

    def test_unknown_key_has_one_fixed_official_public_key_fetch_then_reverification(self):
        self.responses = [(2, MISSING, b'unverified text'), (0, '', b''), (0, GOOD, PLAIN.encode())]
        self.assertEqual(self.descriptor()['Source'], 'fixture')
        self.assertEqual(len(self.calls), 3)
        self.fetch_public.assert_called_once_with(KEY)
        imported, import_options = self.calls[1]
        self.assertIn('--import', imported)
        self.assertNotIn('--recv-keys', imported)
        self.assertEqual(imported[imported.index('--import-options') + 1], 'import-minimal')
        self.assertEqual(import_options['timeout'], 30)
        self.assertEqual(self.calls[0][1]['env']['GNUPGHOME'], import_options['env']['GNUPGHOME'])
        self.assertEqual(self.imported, [PUBLIC])
        self.assertFalse(list((self.work / 'gpg').glob('public-import-*.asc')))

    def test_failed_public_key_fetch_never_returns_unverified_plaintext(self):
        self.responses = [(2, MISSING, PLAIN.encode())]
        self.fetch_public.side_effect = ValueError('Official Ubuntu public signing key retrieval failed')
        with self.assertRaisesRegex(ValueError, 'retrieval failed'):
            self.descriptor()
        self.assertEqual(len(self.calls), 1)

    def test_failed_public_key_import_never_returns_unverified_plaintext(self):
        self.responses = [(2, MISSING, PLAIN.encode()), (2, '', b'')]
        with self.assertRaisesRegex(ValueError, 'import failed'):
            self.descriptor()
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(list((self.work / 'gpg').glob('public-import-*.asc')))

    def test_unknown_key_still_missing_after_fetch_does_not_loop_or_accept_plaintext(self):
        self.responses = [(2, MISSING, PLAIN.encode()), (0, '', b''), (2, MISSING, PLAIN.encode())]
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            self.descriptor()
        self.assertEqual(len(self.calls), 3)

    def test_keyserver_input_is_a_bounded_machine_status_hex_key_not_a_command(self):
        self.responses = [(2, MISSING.replace(KEY, '../../untrusted'), PLAIN.encode())]
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            self.descriptor()
        self.assertEqual(len(self.calls), 1)
        self.fetch_public.assert_not_called()

    def test_bad_revoked_expired_or_missing_validsig_fails_even_with_exit_zero(self):
        for status in ('[GNUPG:] GOODSIG ' + KEY + ' Fixture\n',
                       GOOD + '[GNUPG:] BADSIG ' + KEY + ' Fixture\n',
                       GOOD + '[GNUPG:] REVKEYSIG ' + KEY + ' Fixture\n',
                       GOOD + '[GNUPG:] EXPKEYSIG ' + KEY + ' Fixture\n',
                       GOOD + '[GNUPG:] EXPSIG ' + KEY + ' Fixture\n'):
            with self.subTest(status=status.splitlines()[-1]):
                self.responses = [(0, status, PLAIN.encode())]
                with self.assertRaisesRegex(ValueError, 'verification failed'):
                    self.descriptor()
        self.assertTrue(all('--import' not in call[0] for call in self.calls))
        self.fetch_public.assert_not_called()

    def test_global_home_and_agent_environment_cannot_change_the_verification_home(self):
        with patch.dict(os.environ, {'GNUPGHOME': str(self.root / 'unused'),
                                     'GPG_AGENT_INFO': 'fixture-agent', 'GPG_TTY': 'fixture-tty'}):
            self.descriptor()
        environment = self.calls[0][1]['env']
        self.assertEqual(Path(environment['GNUPGHOME']), self.work / 'gpg')
        self.assertNotIn('GPG_AGENT_INFO', environment)
        self.assertNotIn('GPG_TTY', environment)

    def test_existing_gpg_config_or_private_key_state_is_rejected_without_reading_it(self):
        for name in ('gpg.conf', 'common.conf', 'dirmngr.conf', 'secring.gpg', 'private-keys-v1.d/fixture.key'):
            with self.subTest(path=name):
                path = self.work / 'gpg' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fictional forbidden state; never load this')
                with self.assertRaises(ValueError):
                    self.descriptor()
                path.unlink()
        self.assertEqual(self.calls, [])

    def test_file_or_work_outside_project_fails_before_process_or_network(self):
        for file, work in ((self.root.parent / 'outside.dsc', self.work),
                           (self.file, self.root.parent / 'outside-work'),
                           (self.file, self.root / '.git' / 'work')):
            with self.subTest(file=file.name, work=work.name), self.assertRaises(ValueError):
                verify.verify_source_descriptor(file, 'fixture', '2:1.0-1ubuntu1', work)
        self.assertEqual(self.calls, [])

    def test_timeout_does_not_return_partial_gpg_plaintext(self):
        with patch.object(verify.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 30, output=PLAIN.encode())):
            with self.assertRaisesRegex(ValueError, 'tool failed'):
                self.descriptor()

    def test_unsafe_or_duplicate_payload_paths_never_enter_the_lock(self):
        for checksums in ('a' * 64 + ' 17 ../outside.tar.xz',
                          'a' * 64 + ' 0 empty.tar.xz',
                          'a' * 64 + ' 17 fixture.tar.xz\n ' + 'b' * 64 + ' 12 fixture.tar.xz'):
            with self.subTest(checksums=checksums[-40:]):
                text = PLAIN.split('Checksums-Sha256:')[0] + 'Checksums-Sha256:\n ' + checksums + '\n'
                self.responses = [(0, GOOD, text.encode())]
                with self.assertRaisesRegex(ValueError, 'unsafe SHA256 payload'):
                    self.descriptor()

    def test_changed_source_cache_fails_even_after_signature_success(self):
        original = self.gpg

        def mutate(command, **kwargs):
            result = original(command, **kwargs)
            self.file.write_text(envelope('Source: changed\n'), encoding='utf-8')
            return result

        with patch.object(verify.subprocess, 'run', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during verification'):
                self.descriptor()


class OfficialKeyLookupTests(unittest.TestCase):
    def response(self, data=PUBLIC, url=None):
        response = io.BytesIO(data)
        response.geturl = lambda: url or verify.KEYSERVER_LOOKUP + '?op=get&search=0x' + KEY
        return response

    def lookup(self, response):
        with patch.object(verify.urllib.request, 'build_opener') as builder:
            builder.return_value.open.return_value = response
            result = verify._fetch_public_key(KEY)
            self.assertIsInstance(builder.call_args.args[0], verify._NoKeyRedirects)
            opened = builder.return_value.open.call_args
            self.assertEqual(opened.args[0].full_url,
                             'https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x' + KEY)
            self.assertEqual(opened.kwargs['timeout'], 30)
            return result

    def test_only_fixed_official_https_lookup_is_used_and_returns_public_packets(self):
        self.assertEqual(self.lookup(self.response()), PUBLIC)

    def test_any_redirect_is_rejected_before_following_it(self):
        handler = verify._NoKeyRedirects()
        for url in ('https://unrelated.invalid/public.asc', 'http://keyserver.ubuntu.com/pks/lookup'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'cannot redirect'):
                handler.redirect_request(None, None, 302, '', {}, url)
        with self.assertRaisesRegex(ValueError, 'retrieval failed'):
            self.lookup(self.response(url='https://unrelated.invalid/public.asc'))

    def test_private_or_compressed_packets_cannot_hide_in_public_ascii_armor(self):
        for tag in (5, 7, 8, 9, 18):
            packets = b'\xc6\x06PUBLIC' + bytes([0xc0 | tag, 3]) + b'bad'
            data = (b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' + base64.b64encode(packets) +
                    b'\n-----END PGP PUBLIC KEY BLOCK-----\n')
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, 'Only public key'):
                self.lookup(self.response(data))

    def test_non_public_armor_invalid_packet_lengths_or_multiple_primary_keys_fail(self):
        invalid = [PUBLIC.replace(b'PUBLIC KEY', b'PRIVATE KEY'),
                   b'unsigned prefix\n' + PUBLIC,
                   PUBLIC + b'unsigned suffix',
                   b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' + base64.b64encode(b'\xc6\x7fshort') +
                   b'\n-----END PGP PUBLIC KEY BLOCK-----\n',
                   b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n' + base64.b64encode(b'\xc6\x03one\xc6\x03two') +
                   b'\n-----END PGP PUBLIC KEY BLOCK-----\n']
        for data in invalid:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                self.lookup(self.response(data))

    def test_keyserver_body_size_and_key_identifier_are_bounded_before_import(self):
        with patch.object(verify, 'MAX_PUBLIC_KEY_BYTES', 64):
            with self.assertRaisesRegex(ValueError, 'bounded size'):
                self.lookup(self.response(PUBLIC))
        with patch.object(verify.urllib.request, 'build_opener') as builder:
            with self.assertRaisesRegex(ValueError, 'identifier'):
                verify._fetch_public_key('../untrusted')
            builder.assert_not_called()


if __name__ == '__main__':
    unittest.main()
