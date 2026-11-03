import { readdir, readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const ignored = new Set(['.git', '.tools', '.local', '.gradle', 'node_modules', 'build', 'dist', 'out', 'notes', 'data']);
export const modules = ['application', 'qq-access', 'brain', 'media-tools', 'local-data'];
export const optionalIds = new Set(['voice-reply', 'audio-understanding', 'cover']);

export function inside(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative));
}

export async function loadCatalogue() {
  return JSON.parse(await readFile(path.join(root, 'spec/features.json'), 'utf8'));
}

export function validateCatalogue(catalogue) {
  const errors = [];
  if (catalogue.schemaVersion !== 1 || !Array.isArray(catalogue.features) || !catalogue.features.length)
    return ['功能清单缺少版本或内容'];
  const ids = new Set();
  for (const feature of catalogue.features) {
    if (!/^[a-z][a-z0-9-]+$/.test(feature.id) || ids.has(feature.id)) errors.push('编号不合法或重复：' + feature.id);
    ids.add(feature.id);
    if (!feature.name || !modules.includes(feature.module)) errors.push('名称或模块缺失：' + feature.id);
    if (!Object.hasOwn(catalogue.statusLabels ?? {}, feature.status)) errors.push('状态无对应标签：' + feature.id);
    const expected = optionalIds.has(feature.id) ? 'optional_if_infeasible' : 'required';
    if (feature.requirement !== expected) errors.push('未经授权改变功能范围：' + feature.id);
    if (feature.status === 'omitted' && (!optionalIds.has(feature.id) || !feature.omissionReason)) errors.push('省略缺少授权或理由：' + feature.id);
    if (feature.status === 'verified' && !feature.verificationEvidence) errors.push('验收缺少证据：' + feature.id);
    if (!Array.isArray(feature.sources) || !feature.sources.length || !feature.evidence) errors.push('缺少来源或验收方法：' + feature.id);
    for (const source of feature.sources ?? []) {
      if (typeof source !== 'string' || source.includes('..') || source.includes('\\') || !/^(02-服务器版\/|07-翻唱\/)/.test(source))
        errors.push('来源越界：' + feature.id);
    }
  }
  for (const id of optionalIds) if (!ids.has(id)) errors.push('遗漏可选功能：' + id);
  return errors;
}

export function publicCatalogue(catalogue) {
  return {
    schemaVersion: catalogue.schemaVersion,
    statusLabels: catalogue.statusLabels,
    features: catalogue.features.map(({ id, name, status, requirement, module }) => ({ id, name, status, requirement, module })),
  };
}

export async function sourceFiles(directory = root) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) throw new Error('不沿符号链接扫描：' + path.relative(root, path.join(directory, entry.name)));
    if (entry.isDirectory() && !ignored.has(entry.name)) result.push(...await sourceFiles(path.join(directory, entry.name)));
    else if (entry.isFile()) result.push(path.join(directory, entry.name));
  }
  return result;
}

export function containsSecret(text) {
  return [
    /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
    /\bAKIA[0-9A-Z]{16}\b/,
    /\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}/,
    /\bsk-[A-Za-z0-9_-]{24,}/,
  ].some(pattern => pattern.test(text));
}

export async function validateReference(catalogue) {
  const referenceRoot = path.resolve(root, '..', '大肥鱼服务器版');
  if (path.resolve(root, catalogue.referenceProject) !== referenceRoot) throw new Error('参考范围不是用户指定目录');
  const actualRoot = await realpath(referenceRoot);
  const sources = [...new Set(catalogue.features.flatMap(feature => feature.sources))];
  for (const source of sources) {
    const target = path.resolve(referenceRoot, source);
    if (!inside(referenceRoot, target) || !inside(actualRoot, await realpath(target)) || !(await stat(target)).isFile())
      throw new Error('参考源码缺失或越界：' + source);
  }
  return sources.length;
}
