"""Historical source fixtures never run the real source stage or transports."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('source_pool_reuse_builder', ROOT / 'tools/fetch-offline-runtime.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
import offline_source_verify


class SourcePoolReuseTests(unittest.TestCase):
    def fixture(self, *, indexed_sha='a' * 64, indexed_size=17,
                indexed_filename='fixture_1.0.orig.tar.xz', include_patch=True):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'build', prefix='source-pool-fixture-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        work = root / 'work'
        work.mkdir()
        (work / 'apt-plan.json').write_text(json.dumps({
            'basePackages': [{'Package': 'fixture', 'Version': '1:1.0-1'}], 'packages': []}), encoding='utf-8')
        current = {'Package': 'fixture', 'Version': '1:1.0-2', 'Directory': 'pool/main/f/fixture',
                   'Checksums-Sha256': '\n ' + indexed_sha + ' ' + str(indexed_size) + ' ' + indexed_filename}
        old = {'Source': 'fixture', 'Version': '1:1.0-1',
               'Checksums-Sha256': '\n ' + 'a' * 64 + ' 17 fixture_1.0.orig.tar.xz'}
        if include_patch:
            old['Checksums-Sha256'] += '\n ' + 'b' * 64 + ' 12 fixture_1.0-1.debian.tar.xz'
        descriptor_bytes = b'fictional signed old descriptor; verified consumer is mocked'

        def descriptor_download(url, target, *args, **kwargs):
            self.assertTrue(url.startswith('https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/'))
            self.assertTrue(url.endswith('/fixture_1.0-1.dsc'))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(descriptor_bytes)
            return target

        def verified_index(suite, component, kind, keyring):
            self.assertEqual(kind, 'source')
            return [current] if (suite, component) == ('noble', 'main') else []

        with patch.object(builder, 'ROOT', root), patch.object(builder, 'WORK', work), \
                patch.object(builder, 'SOURCES', root / 'sources'), \
                patch.object(builder, 'base_records', return_value=({}, [], root / 'archive-keyring.gpg')), \
                patch.object(builder, 'verified_index', side_effect=verified_index), \
                patch.object(builder, 'download', side_effect=descriptor_download) as descriptor_fetch, \
                patch.object(offline_source_verify, 'verify_source_descriptor', return_value=old) as verify_descriptor, \
                patch.object(builder, 'parallel_download') as payload_fetch, \
                patch.object(builder, 'write_json') as write_lock, \
                patch.object(builder.urllib.request, 'urlopen', side_effect=AssertionError('No source fixture network')), \
                patch.object(builder.subprocess, 'run', side_effect=AssertionError('No source fixture processes')):
            # All stage inputs/outputs above are isolated fixtures; no actual
            # project cache, lock, source payload or resolver is read or changed.
            builder.sources_stage()
        self.assertEqual(descriptor_fetch.call_count, 1)
        self.assertEqual(verify_descriptor.call_args.args[1:3], ('fixture', '1:1.0-1'))
        self.assertEqual(write_lock.call_args.args[0], root / 'runtime/linux-sources.lock.json')
        lock = write_lock.call_args.args[1]
        self.assertEqual(lock['packages'][0]['version'], '1:1.0-1')
        files = {Path(item['path']).name: item for item in lock['files']}
        descriptor = files['fixture_1.0-1.dsc']
        self.assertEqual(descriptor['source'], descriptor_fetch.call_args.args[0])
        self.assertEqual(descriptor['sha256'], hashlib.sha256(descriptor_bytes).hexdigest())
        self.assertEqual((root / 'sources' / descriptor['path']).read_bytes(), descriptor_bytes)
        self.assertEqual(files['fixture_1.0.orig.tar.xz']['sha256'], 'a' * 64)
        self.assertEqual(files['fixture_1.0.orig.tar.xz']['size'], 17)
        self.assertEqual({item[0] for item in payload_fetch.call_args.args[0]},
                         {item['source'] for item in lock['files']})
        if include_patch:
            patch_file = files['fixture_1.0-1.debian.tar.xz']
            self.assertTrue(patch_file['source'].startswith('https://launchpad.net/'))
            self.assertEqual(patch_file['sha256'], 'b' * 64)
        return files

    def test_matching_shared_orig_uses_official_pool_without_changing_old_version_or_patches(self):
        files = self.fixture()
        self.assertEqual(files['fixture_1.0.orig.tar.xz']['source'],
                         builder.UBUNTU + 'pool/main/f/fixture/fixture_1.0.orig.tar.xz')

    def test_hash_size_and_filename_must_all_match_before_reusing_an_indexed_file(self):
        for change in ({'indexed_sha': 'c' * 64}, {'indexed_size': 18},
                       {'indexed_filename': 'different_1.0.orig.tar.xz'}):
            with self.subTest(change=change):
                files = self.fixture(**change)
                self.assertTrue(files['fixture_1.0.orig.tar.xz']['source'].startswith('https://launchpad.net/'))

    def test_last_payload_reuse_cannot_redirect_the_historical_signed_descriptor(self):
        files = self.fixture(include_patch=False)
        self.assertTrue(files['fixture_1.0.orig.tar.xz']['source'].startswith(builder.UBUNTU))
        self.assertTrue(files['fixture_1.0-1.dsc']['source'].startswith('https://launchpad.net/'))

    def test_shared_url_map_cannot_add_nonpool_urls_or_traversal_paths(self):
        for directory, filename in (('https://unrelated.invalid/pool', 'fixture.tar.xz'),
                                    ('pool/main/../fixture', 'fixture.tar.xz'),
                                    ('pool/main/f/fixture', '../fixture.tar.xz')):
            with self.subTest(directory=directory, filename=filename), self.assertRaises(ValueError):
                builder.source_pool_file_urls({'Directory': directory,
                    'Checksums-Sha256': 'a' * 64 + ' 17 ' + filename})


if __name__ == '__main__':
    unittest.main()
