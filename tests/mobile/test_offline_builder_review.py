"""Offline build closure regressions; all package records and transports are fictional."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('offline_builder_review', ROOT / 'tools/fetch-offline-runtime.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
verify_spec = importlib.util.spec_from_file_location('offline_source_verify', ROOT / 'tools/offline_source_verify.py')
source_verify = importlib.util.module_from_spec(verify_spec)
sys.modules.setdefault('offline_source_verify', source_verify)
verify_spec.loader.exec_module(source_verify)


def package(name, version='1', *, installed=False, **fields):
    item = {'Package': name, 'Version': version, 'Architecture': 'arm64', **fields}
    if installed:
        item['Status'] = 'install ok installed'
    else:
        item['Filename'] = 'pool/main/fixture/' + name + '_' + version.replace(':', '_') + '_arm64.deb'
    return item


def wheel(name, version='1', requires=()):
    return {'name': name, 'version': version, 'requires': list(requires)}


def fake_host_environment():
    # Marker.evaluate obtains this base even when passed an explicit target.
    # Keep it different from the target without querying the real Windows host.
    return {'implementation_name': 'cpython', 'implementation_version': '3.13.16',
            'os_name': 'nt', 'platform_machine': 'AMD64', 'platform_release': '10',
            'platform_system': 'Windows', 'platform_version': '10.0.99999',
            'python_full_version': '3.13.16', 'platform_python_implementation': 'CPython',
            'python_version': '3.13', 'sys_platform': 'win32'}


class OfflineBuildClosureTests(unittest.TestCase):
    def setUp(self):
        # Even a regression that unexpectedly invokes a transport must stay local.
        network = patch.object(builder.urllib.request, 'urlopen', side_effect=AssertionError('No network in closure fixtures'))
        network.start()
        self.addCleanup(network.stop)
        markers = patch('packaging.markers.default_environment', side_effect=fake_host_environment)
        markers.start()
        self.addCleanup(markers.stop)

    def test_upgrading_a_base_dependency_preserves_reverse_dependencies(self):
        installed = [package('base-client', installed=True, Depends='libx (= 1)'),
                     package('libx', installed=True)]
        records = [package('root', Depends='libx (>= 2)'), package('libx', '2'),
                   package('base-client', '2', Depends='libx (= 2)')]
        selected = {item['Package']: item for item in builder.resolve_packages(records, installed, ['root'])}
        self.assertEqual(selected['libx']['Version'], '2')
        self.assertEqual(selected['base-client']['Version'], '2')

    def test_alternative_rolls_back_a_candidate_with_an_unavailable_dependency(self):
        records = [package('root', Depends='first | second'),
                   package('first', Depends='unavailable'), package('second')]
        selected = {item['Package'] for item in builder.resolve_packages(records, [], ['root'])}
        self.assertEqual(selected, {'root', 'second'})

    def test_failed_candidate_constraints_do_not_poison_the_next_candidate(self):
        records = [package('root', Depends='client'),
                   package('client', '2', Depends='shared (= 2), unavailable'),
                   package('client', '1', Depends='shared (= 1)'),
                   package('shared', '1'), package('shared', '2')]
        selected = {item['Package']: item for item in builder.resolve_packages(records, [], ['root'])}
        self.assertEqual(selected['client']['Version'], '1')
        self.assertEqual(selected['shared']['Version'], '1')

    def test_breaks_requires_upgrading_a_retained_base_package(self):
        installed = [package('base-client', installed=True)]
        records = [package('root', Breaks='base-client (<< 2)'),
                   package('base-client', '2')]
        selected = {item['Package']: item for item in builder.resolve_packages(records, installed, ['root'])}
        self.assertEqual(selected['base-client']['Version'], '2')

    def test_unsatisfiable_conflict_with_a_retained_base_package_fails_closed(self):
        installed = [package('base-client', installed=True)]
        records = [package('root', Conflicts='base-client')]
        with self.assertRaises(ValueError):
            builder.resolve_packages(records, installed, ['root'])

    def test_versioned_virtual_provides_cannot_satisfy_a_higher_requirement(self):
        records = [package('root', Depends='capability (>= 2)'),
                   package('provider', Provides='capability (= 1)')]
        with self.assertRaises(ValueError):
            builder.resolve_packages(records, [], ['root'])

    def test_native_implementation_version_uses_the_target_cpython(self):
        records = {'root': wheel('root', requires=['needed; implementation_version < "3.13"'])}
        # The build host may run Python 3.13, while the bundled Ubuntu uses 3.12.
        self.assertEqual(builder.linux_requirements(records, ['root']), ['needed'])

    def test_linux_arm64_dependencies_and_requested_extras_are_included(self):
        records = {'root': wheel('root', requires=[
            'linux-only; sys_platform == "linux" and platform_machine == "aarch64"',
            'windows-only; sys_platform == "win32"',
            'extra-only; extra == "media"',
        ])}
        self.assertEqual(builder.linux_requirements(records, ['root[media]']), ['extra-only', 'linux-only'])

    def test_an_extra_discovered_later_revisits_the_existing_wheel(self):
        records = {'root': wheel('root', requires=['helper[media]', 'helper']),
                   'helper': wheel('helper', requires=['extra-only; extra == "media"'])}
        self.assertEqual(builder.linux_requirements(records, ['root']), ['extra-only'])

    def test_debian_version_order_includes_epoch_tilde_revision_and_numeric_runs(self):
        ordered = ['1.0~~', '1.0~rc1', '1.0', '1.0-1', '1.0-1+b1',
                   '1.0-2', '1.0-10', '1.0a', '1.0+', '2.0', '1:0.1']
        for lower, higher in zip(ordered, ordered[1:]):
            with self.subTest(lower=lower, higher=higher):
                self.assertLess(builder.version_compare(lower, higher), 0)
                self.assertGreater(builder.version_compare(higher, lower), 0)
        for left, right in [('1.0', '1.0-0'), ('0:1.02', '1.2'), ('1.0-01', '1.0-1')]:
            with self.subTest(left=left, right=right):
                self.assertEqual(builder.version_compare(left, right), 0)

    def test_source_identity_preserves_the_explicit_source_epoch(self):
        self.assertEqual(builder.source_identity(package('binary', '9', Source='source (2:1.2-3)')),
                         ('source', '2:1.2-3'))
        self.assertEqual(builder.source_identity(package('binary', '1:2.3')),
                         ('binary', '1:2.3'))

    def test_descriptor_caches_for_different_epochs_cannot_alias(self):
        filename = 'source_1.2-3.dsc'  # Debian filenames omit the epoch.
        first = builder.source_descriptor_path('source', '1:1.2-3', filename)
        second = builder.source_descriptor_path('source', '2:1.2-3', filename)
        self.assertNotEqual(first, second)
        self.assertTrue(first.resolve().is_relative_to(builder.WORK.resolve()))
        self.assertTrue(second.resolve().is_relative_to(builder.WORK.resolve()))

    def test_public_archive_paths_cannot_escape_or_use_ambiguous_separators(self):
        base = ROOT / 'build/offline-review-fixture'
        self.assertEqual(builder.safe_child(base, 'pool/main/fixture.deb'),
                         (base / 'pool/main/fixture.deb').resolve())
        for relative in ('', '../outside', '/absolute', 'pool/../outside',
                         'pool/./fixture', 'pool//fixture', 'pool\\fixture', 'C:/outside'):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                builder.safe_child(base, relative)

    def test_unplanned_or_missing_bundle_files_are_rejected(self):
        expected = {'base/base-arm64.tar.gz', 'apt/pool/main/fixture/current_1_arm64.deb'}
        builder.validate_bundle_paths(expected, expected)
        with self.assertRaisesRegex(ValueError, 'extra='):
            builder.validate_bundle_paths(expected | {'apt/pool/main/fixture/stale_0_arm64.deb'}, expected)
        with self.assertRaisesRegex(ValueError, 'missing='):
            builder.validate_bundle_paths(expected - {'base/base-arm64.tar.gz'}, expected)


class OfflineWheelArchiveTests(unittest.TestCase):
    def setUp(self):
        markers = patch('packaging.markers.default_environment', side_effect=fake_host_environment)
        markers.start()
        self.addCleanup(markers.stop)
        base = ROOT / 'build/offline-review-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wheel_file(self, tag, *, metadata_name='fixture'):
        file = self.root / ('fixture-1-' + tag + '.whl')
        with zipfile.ZipFile(file, 'w') as archive:
            archive.writestr('fixture-1.dist-info/METADATA',
                             'Metadata-Version: 2.1\nName: ' + metadata_name + '\nVersion: 1\n\n')
        self.addCleanup(file.unlink, missing_ok=True)
        return file

    def test_incompatible_stale_wheels_cannot_make_linux_closure_look_complete(self):
        for tag in ('cp313-cp313-win_amd64', 'cp313-cp313-linux_aarch64',
                    'cp312-cp312-manylinux_2_40_aarch64', 'cp312-cp312-linux_x86_64'):
            with self.subTest(tag=tag):
                file = self.wheel_file(tag)
                try:
                    with self.assertRaisesRegex(ValueError, 'incompatible'):
                        builder.wheel_records(self.root)
                finally:
                    file.unlink(missing_ok=True)

    def test_target_native_abi3_and_universal_wheels_are_accepted(self):
        for tag in ('cp312-cp312-manylinux_2_39_aarch64',
                    'cp38-abi3-manylinux2014_aarch64', 'py3-none-any'):
            with self.subTest(tag=tag):
                file = self.wheel_file(tag)
                try:
                    records = builder.wheel_records(self.root)
                    self.assertEqual(builder.linux_requirements(records, ['fixture==1']), [])
                finally:
                    file.unlink(missing_ok=True)

    def test_wheel_filename_identity_must_match_distribution_metadata(self):
        self.wheel_file('py3-none-any', metadata_name='unrelated')
        with self.assertRaisesRegex(ValueError, 'metadata differ'):
            builder.wheel_records(self.root)


class OfflineSourceVerificationTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'build/offline-review-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.message = self.root / 'fixture.dsc'
        self.keyring = self.root / 'archive-keyring.gpg'
        self.keyring.write_bytes(b'fictional-public-keyring')
        self.work = self.root / 'work'
        self.plaintext = self.descriptor()
        self.message.write_text(self.envelope(self.plaintext), encoding='utf-8')
        network = patch.object(source_verify.subprocess, 'run', side_effect=self.signed_response)
        self.gpg = network.start()
        self.addCleanup(network.stop)
        for target in (patch.object(source_verify, '_gpg', return_value=Path('fictional-gpg')),
                       patch('urllib.request.urlopen', side_effect=AssertionError('No network in source fixtures'))):
            target.start()
            self.addCleanup(target.stop)

    @staticmethod
    def descriptor(name='fixture', version='1:1.2-3'):
        return ('Format: 3.0 (quilt)\nSource: ' + name + '\nVersion: ' + version +
                '\nChecksums-Sha256:\n ' + '0' * 64 + ' 12 fixture.orig.tar.gz\n')

    @staticmethod
    def envelope(plaintext):
        return ('-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n' + plaintext +
                '-----BEGIN PGP SIGNATURE-----\n\nfictional-signature\n-----END PGP SIGNATURE-----\n')

    def signed_response(self, command, **kwargs):
        status = Path(kwargs['cwd']) / command[command.index('--status-file') + 1]
        fingerprint = '0123456789ABCDEF0123456789ABCDEF01234567'
        status.write_text('[GNUPG:] GOODSIG 0123456789ABCDEF fixture\n[GNUPG:] VALIDSIG ' +
                          fingerprint + ' 2024-01-01 1704067200 0 4 0 1 10 01 ' + fingerprint + '\n',
                          encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, stdout=self.plaintext.encode(), stderr=b'')

    def test_unsigned_prefix_and_tail_are_rejected_before_running_gpg(self):
        original = self.envelope(self.plaintext)
        for text in (' ' + 'f' * 64 + ' 1 main/binary-arm64/Packages.xz\n' + original,
                     '\n' + original, original + 'Source: unrelated\n', original + ' \n'):
            with self.subTest(prefix=text.startswith('-----')):
                self.message.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    source_verify.verify_clearsigned(self.message, self.work, self.keyring)
        self.gpg.assert_not_called()

    def test_only_gpg_verified_plaintext_is_returned_and_the_archive_keyring_is_exclusive(self):
        self.message.write_text(self.envelope(self.descriptor('unverified-body')), encoding='utf-8')
        self.assertEqual(source_verify.verify_clearsigned(self.message, self.work, self.keyring), self.plaintext)
        command = self.gpg.call_args.args[0]
        self.assertIn('--decrypt', command)
        self.assertIn('--no-auto-key-retrieve', command)
        self.assertIn('--no-default-keyring', command)
        self.assertNotIn('--recv-keys', command)
        self.assertEqual(self.gpg.call_count, 1)

    def test_bad_signature_never_returns_plaintext_even_if_gpg_emits_it(self):
        def bad_signature(command, **kwargs):
            status = Path(kwargs['cwd']) / command[command.index('--status-file') + 1]
            status.write_text('[GNUPG:] BADSIG 0123456789ABCDEF fixture\n', encoding='utf-8')
            return subprocess.CompletedProcess(command, 1, stdout=self.plaintext.encode(), stderr=b'')
        self.gpg.side_effect = bad_signature
        with self.assertRaisesRegex(ValueError, 'signature verification failed'):
            source_verify.verify_clearsigned(self.message, self.work, self.keyring)

    def test_unknown_archive_key_does_not_fetch_an_external_public_key(self):
        def missing_signature(command, **kwargs):
            status = Path(kwargs['cwd']) / command[command.index('--status-file') + 1]
            status.write_text('[GNUPG:] ERRSIG 0123456789ABCDEF 1 10 01 1704067200 9\n'
                              '[GNUPG:] NO_PUBKEY 0123456789ABCDEF\n', encoding='utf-8')
            return subprocess.CompletedProcess(command, 2, stdout=b'', stderr=b'')
        self.gpg.side_effect = missing_signature
        with self.assertRaises(ValueError):
            source_verify.verify_clearsigned(self.message, self.work, self.keyring)
        self.assertEqual(self.gpg.call_count, 1)
        self.assertNotIn('--recv-keys', self.gpg.call_args.args[0])

    def test_source_name_and_full_epoch_version_must_match_the_requested_source(self):
        for name, version in (('unrelated', '1:1.2-3'), ('fixture', '2:1.2-3'), ('fixture', '1.2-3')):
            with self.subTest(name=name, version=version):
                self.plaintext = self.descriptor(name, version)
                with self.assertRaisesRegex(ValueError, 'identity does not match'):
                    source_verify.verify_source_descriptor(self.message, 'fixture', '1:1.2-3', self.work)

    def test_matching_source_descriptor_retains_its_verified_payload_hashes(self):
        record = source_verify.verify_source_descriptor(self.message, 'fixture', '1:1.2-3', self.work)
        self.assertEqual(record['Source'], 'fixture')
        self.assertEqual(record['Version'], '1:1.2-3')
        self.assertIn('0' * 64 + ' 12 fixture.orig.tar.gz', record['Checksums-Sha256'])


if __name__ == '__main__':
    unittest.main()
