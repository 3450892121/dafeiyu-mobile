import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, writeFile, readFile, rm, symlink } from 'node:fs/promises';
import path from 'node:path';
import { root, inside } from '../tools/project-lib.mjs';
import { allowedOfflinePath, validateOfflineManifest, inspectOfflineBundle, prepareOfflineAssets, offlineLimitBytes } from '../tools/offline-assets-lib.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const seal = files => ({ schemaVersion: 1, files, sha256: hash(JSON.stringify(files)), totalBytes: files.reduce((sum, item) => sum + item.size, 0) });

async function fixture(t) {
  await mkdir(path.join(root, 'build'), { recursive: true });
  const directory = await mkdtemp(path.join(root, 'build/offline-assets-test-'));
  t.after(async () => { assert.ok(inside(root, directory)); await rm(directory, { recursive: true, force: true }); });
  const cache = path.join(directory, 'cache'), packaged = path.join(directory, 'generated/offline');
  await mkdir(cache);
  const files = [];
  for (const name of [
    'archives/napcat.zip', 'base/base-arm64.tar.gz', 'models/sensevoice.tar.bz2', 'apt/sources.list',
    'apt/dists/noble/InRelease', 'apt/dists/noble/main/binary-arm64/Packages.xz',
    'apt/pool/main/p/python3/python3_3.12_arm64.deb', 'wheels/example-1.0-py3-none-any.whl',
  ].sort()) {
    const data = Buffer.from('fictional-public-component:' + name);
    const target = path.join(cache, name);
    await mkdir(path.dirname(target), { recursive: true }); await writeFile(target, data);
    files.push({ path: name, size: data.length, sha256: hash(data), source: 'https://example.invalid/official/' + name, purpose: '隔离测试资源' });
  }
  const manifest = seal(files), fixed = path.join(directory, 'fixed-manifest.json');
  const bytes = JSON.stringify(manifest, null, 2) + '\n';
  await writeFile(path.join(cache, 'manifest.json'), bytes); await writeFile(fixed, bytes);
  return { directory, cache, packaged, fixed, manifest };
}

test('complete offline assets copy and check their actual bytes separately from business payload', async t => {
  const f = await fixture(t);
  const result = await prepareOfflineAssets(f.cache, f.packaged, { expectedManifestPath: f.fixed });
  assert.equal(result.files.length, 8);
  await prepareOfflineAssets(f.cache, f.packaged, { check: true, expectedManifestPath: f.fixed });
  assert.deepEqual(await readFile(path.join(f.packaged, 'manifest.json')), await readFile(f.fixed));
  assert.deepEqual(await readFile(path.join(f.packaged, 'models/sensevoice.tar.bz2')), await readFile(path.join(f.cache, 'models/sensevoice.tar.bz2')));
});

test('corrupt cached component fails before replacing an existing complete generated bundle', async t => {
  const f = await fixture(t);
  await prepareOfflineAssets(f.cache, f.packaged, { expectedManifestPath: f.fixed });
  const before = await readFile(path.join(f.packaged, 'base/base-arm64.tar.gz'));
  const item = f.manifest.files.find(file => file.path === 'base/base-arm64.tar.gz');
  await writeFile(path.join(f.cache, item.path), Buffer.alloc(item.size));
  await assert.rejects(prepareOfflineAssets(f.cache, f.packaged, { expectedManifestPath: f.fixed }), /大小或摘要/);
  assert.deepEqual(await readFile(path.join(f.packaged, item.path)), before);
});

test('check mode rejects a corrupt packaged resource and extra unmanifested APK assets', async t => {
  const f = await fixture(t);
  await prepareOfflineAssets(f.cache, f.packaged, { expectedManifestPath: f.fixed });
  const item = f.manifest.files.find(file => file.path === 'models/sensevoice.tar.bz2');
  await writeFile(path.join(f.packaged, item.path), Buffer.alloc(item.size));
  await assert.rejects(prepareOfflineAssets(f.cache, f.packaged, { check: true }), /大小或摘要/);
  await writeFile(path.join(f.packaged, item.path), await readFile(path.join(f.cache, item.path)));
  await writeFile(path.join(f.packaged, 'unlisted-component.zip'), 'fictional');
  await assert.rejects(prepareOfflineAssets(f.cache, f.packaged, { check: true }), /未列文件/);
});

test('a cache with unlisted files or a missing listed asset cannot be packaged', async t => {
  const f = await fixture(t), extra = path.join(f.cache, 'credentials.json');
  await writeFile(extra, '{}');
  await assert.rejects(inspectOfflineBundle(f.cache), /未列文件/);
  await rm(extra);
  await rm(path.join(f.cache, 'archives/napcat.zip'));
  await assert.rejects(inspectOfflineBundle(f.cache), /文件缺失/);
});

test('cache must match the tracked fixed manifest, including provenance metadata', async t => {
  const f = await fixture(t), changed = structuredClone(f.manifest.files);
  changed[0].source = 'https://different.example.invalid/changed-source';
  await writeFile(f.fixed, JSON.stringify(seal(changed)));
  await assert.rejects(inspectOfflineBundle(f.cache, f.fixed), /固定清单不一致/);
});

test('manifest rejects path escape, QQ archives, credentials and duplicate case-sensitive paths', async t => {
  const f = await fixture(t);
  for (const name of ['../outside.deb', '/base/base-arm64.tar.gz', 'C:/secret', 'apt/pool/../private.deb', 'apt\\pool\\file.deb', 'archives/qq-arm64.deb', 'apt/pool/main/q/qq_1.0_arm64.deb', 'wheels/credential.key'])
    assert.equal(allowedOfflinePath(name), false, name);
  for (const source of ['http://example.invalid/file', 'https://user:fictional@example.invalid/file', 'https://example.invalid/file?token=fictional', 'https://dldir1.qq.com/qq.deb']) {
    const changed = structuredClone(f.manifest.files); changed[0].source = source;
    assert.throws(() => validateOfflineManifest(seal(changed)), /来源|QQ/);
  }
  const duplicate = [...f.manifest.files, { ...f.manifest.files.at(-1), path: 'wheels/EXAMPLE-1.0-py3-none-any.whl' }].sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  assert.throws(() => validateOfflineManifest(seal(duplicate)), /重复/);
});

test('manifest requires all component groups, its own digest and the exact declared total', async t => {
  const f = await fixture(t);
  for (const pattern of [/^base\//, /^archives\//, /^models\//, /^apt\/pool\//, /^wheels\//, /\/InRelease$/, /\/Packages\.xz$/, /^apt\/sources\.list$/])
    assert.throws(() => validateOfflineManifest(seal(f.manifest.files.filter(item => !pattern.test(item.path)))), /缺少/);
  assert.throws(() => validateOfflineManifest({ ...f.manifest, totalBytes: f.manifest.totalBytes + 1 }), /总大小/);
  assert.throws(() => validateOfflineManifest({ ...f.manifest, sha256: '0'.repeat(64) }), /清单摘要/);
  const huge = structuredClone(f.manifest.files); huge[0].size = offlineLimitBytes + 1;
  assert.throws(() => validateOfflineManifest(seal(huge)), /体积预算/);
});

test('source tree links are refused even when their target is another in-project fixture', async t => {
  const f = await fixture(t), target = path.join(f.directory, 'unrelated');
  await mkdir(target);
  try { await symlink(target, path.join(f.cache, 'linked-directory'), 'junction'); }
  catch (error) { if (error.code === 'EPERM' || error.code === 'EACCES') { t.skip('host does not permit test junction creation'); return; } throw error; }
  await assert.rejects(inspectOfflineBundle(f.cache), /链接/);
});

test('manifest reads and generated replacement cannot escape the current project', async t => {
  const f = await fixture(t), outside = path.resolve(root, '../not-this-project');
  await assert.rejects(inspectOfflineBundle(f.cache, path.join(outside, 'manifest.json')), /本项目/);
  await assert.rejects(prepareOfflineAssets(f.cache, outside), /本项目/);
});
