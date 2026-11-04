import { createReadStream } from 'node:fs';
import { copyFile, lstat, mkdir, readFile, readdir, realpath, rename, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { root, inside } from './project-lib.mjs';

export const offlineLimitBytes = 1800 * 1024 * 1024;
const manifestLimitBytes = 8 * 1024 * 1024;
const digestPattern = /^[a-f0-9]{64}$/;

export function allowedOfflinePath(value) {
  if (typeof value !== 'string' || !value || value.includes('\\') || value.includes(':') ||
      value.startsWith('/') || value.split('/').some(part => !part || part === '.' || part === '..')) return false;
  if (/(?:^|\/)(?:linux)?qq[^/]*\.(?:deb|zip|tar(?:\.gz|\.xz|\.bz2)?)$/i.test(value)) return false;
  return /^(?:base\/base-arm64\.tar\.gz|archives\/napcat\.zip|models\/sensevoice\.tar\.bz2|apt\/sources\.list)$/.test(value) ||
    /^apt\/dists\/[A-Za-z0-9_.+~%/-]+\/(?:InRelease|Packages\.xz)$/.test(value) ||
    /^apt\/pool\/[A-Za-z0-9_.+~%/-]+\.deb$/.test(value) ||
    /^wheels\/[A-Za-z0-9_.+~%-]+\.whl$/.test(value);
}

export function validateOfflineManifest(manifest) {
  if (!manifest || manifest.schemaVersion !== 1 || !Array.isArray(manifest.files) ||
      !manifest.files.length || manifest.files.length > 10000 || !digestPattern.test(manifest.sha256 ?? ''))
    throw new Error('内置资源清单缺少版本、文件或有效摘要');
  const names = new Set();
  let previous = '', total = 0;
  for (const item of manifest.files) {
    if (!item || !allowedOfflinePath(item.path) || !Number.isSafeInteger(item.size) || item.size <= 0 ||
        !digestPattern.test(item.sha256 ?? '')) throw new Error('内置资源条目路径、大小或摘要无效');
    if (item.path <= previous || names.has(item.path.toLowerCase())) throw new Error('内置资源路径重复或未按ASCII排序');
    previous = item.path; names.add(item.path.toLowerCase());
    let source;
    try { source = new URL(item.source); } catch { throw new Error('内置资源缺少公开HTTPS来源：' + item.path); }
    if (source.protocol !== 'https:' || source.username || source.password || source.search || source.hash ||
        source.hostname === 'qq.com' || source.hostname.endsWith('.qq.com'))
      throw new Error('内置资源来源无效或属于未获再分发授权的QQ：' + item.path);
    total += item.size;
    if (!Number.isSafeInteger(total) || total > offlineLimitBytes) throw new Error('内置资源超过单APK体积预算');
  }
  for (const required of ['base/base-arm64.tar.gz', 'archives/napcat.zip', 'models/sensevoice.tar.bz2', 'apt/sources.list'])
    if (!names.has(required)) throw new Error('缺少必须内置的资源：' + required);
  for (const [label, pattern] of [
    ['APT签名索引', /^apt\/dists\/.+\/InRelease$/], ['APT包索引', /^apt\/dists\/.+\/Packages\.xz$/],
    ['系统依赖', /^apt\/pool\/.+\.deb$/], ['Python wheels', /^wheels\/.+\.whl$/],
  ]) if (!manifest.files.some(item => pattern.test(item.path))) throw new Error('内置资源缺少' + label);
  if (manifest.totalBytes !== total) throw new Error('内置资源总大小不匹配');
  const digest = createHash('sha256').update(JSON.stringify(manifest.files)).digest('hex');
  if (digest !== manifest.sha256) throw new Error('内置资源清单摘要不匹配');
  return manifest;
}

async function projectDirectory(directory) {
  const absolute = path.resolve(directory);
  if (!inside(root, absolute) || absolute === root) throw new Error('内置资源操作必须位于本项目子目录');
  let parent = absolute;
  while (inside(root, parent)) {
    try {
      if ((await lstat(parent)).isSymbolicLink() || !inside(root, await realpath(parent)))
        throw new Error('内置资源路径不能包含符号链接或越界');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (parent === root) break;
    parent = path.dirname(parent);
  }
  return absolute;
}

async function listedFiles(directory, relative = '') {
  const result = [];
  for (const item of await readdir(path.join(directory, relative), { withFileTypes: true })) {
    const name = path.posix.join(relative, item.name);
    if (item.isSymbolicLink()) throw new Error('内置资源不接受链接：' + name);
    if (item.isDirectory()) result.push(...await listedFiles(directory, name));
    else if (item.isFile()) result.push(name);
    else throw new Error('内置资源只接受普通文件：' + name);
  }
  return result;
}

export async function fileSha256(file) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest('hex');
}

export async function inspectOfflineBundle(directory, expectedManifestPath) {
  directory = await projectDirectory(directory);
  const manifestPath = path.join(directory, 'manifest.json');
  const manifestStat = await lstat(manifestPath);
  if (!manifestStat.isFile() || manifestStat.isSymbolicLink() || manifestStat.size > manifestLimitBytes)
    throw new Error('内置资源清单不是有效的普通小文件');
  const bytes = await readFile(manifestPath);
  const manifest = validateOfflineManifest(JSON.parse(bytes.toString('utf8')));
  if (expectedManifestPath) {
    const fixedPath = await projectDirectory(expectedManifestPath), fixedStat = await lstat(fixedPath);
    if (!fixedStat.isFile() || fixedStat.size > manifestLimitBytes) throw new Error('固定内置清单不是有效的普通小文件');
    const fixed = validateOfflineManifest(JSON.parse(await readFile(fixedPath, 'utf8')));
    if (JSON.stringify(fixed) !== JSON.stringify(manifest)) throw new Error('内置资源缓存与入库固定清单不一致');
  }
  const actual = new Set(await listedFiles(directory));
  const expected = new Set(['manifest.json', ...manifest.files.map(item => item.path)]);
  for (const name of actual) if (!expected.has(name)) throw new Error('内置资源目录含未列文件：' + name);
  for (const name of expected) if (!actual.has(name)) throw new Error('内置资源文件缺失：' + name);
  for (const item of manifest.files) {
    const file = path.join(directory, item.path);
    const info = await lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.size !== item.size || await fileSha256(file) !== item.sha256)
      throw new Error('内置资源大小或摘要错误：' + item.path);
  }
  return { manifest, bytes };
}

export async function prepareOfflineAssets(sourceDirectory, destinationDirectory, { check = false, expectedManifestPath } = {}) {
  const source = await inspectOfflineBundle(sourceDirectory, expectedManifestPath);
  const destination = await projectDirectory(destinationDirectory);
  if (check) {
    const packaged = await inspectOfflineBundle(destination, expectedManifestPath);
    if (!source.bytes.equals(packaged.bytes)) throw new Error('APK内置资源清单不同步');
    return source.manifest;
  }
  // Validate the complete cache before replacing only this project's generated directory.
  await mkdir(path.dirname(destination), { recursive: true });
  const stage = await projectDirectory(destination + '.stage-' + process.pid + '-' + Date.now());
  await mkdir(stage, { recursive: false });
  try {
    for (const item of source.manifest.files) {
      const file = path.join(stage, item.path);
      await mkdir(path.dirname(file), { recursive: true });
      await copyFile(path.join(sourceDirectory, item.path), file);
    }
    await copyFile(path.join(sourceDirectory, 'manifest.json'), path.join(stage, 'manifest.json'));
    await inspectOfflineBundle(stage, expectedManifestPath);
    await rm(destination, { recursive: true, force: true });
    await rename(stage, destination);
  } finally {
    await rm(stage, { recursive: true, force: true });
  }
  return source.manifest;
}
