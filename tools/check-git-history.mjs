import { spawn } from 'node:child_process';
import { readFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { StringDecoder } from 'node:string_decoder';
import { root, inside, containsSecret } from './project-lib.mjs';

// Inspect only the current repository. Never print object contents or Git stderr.
const environment = { ...process.env, GIT_CONFIG_NOSYSTEM: '1', GIT_NO_REPLACE_OBJECTS: '1' };
environment.GIT_CONFIG_GLOBAL = path.join(root, '.git', 'config');
for (const key of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
  'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_INDEX_FILE', 'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS']) delete environment[key];

function git(arguments_, input = '') {
  const child = spawn('git', ['--no-replace-objects', '-C', root, '-c', `safe.directory=${root}`,
    '-c', 'core.quotePath=false', ...arguments_], {
    cwd: root, env: environment, stdio: ['pipe', 'pipe', 'pipe'],
  });
  // Attach completion handlers immediately, including for an unavailable Git executable.
  const completed = new Promise((resolve, reject) => {
    child.once('error', () => reject(new Error('无法启动 Git，历史扫描未完成')));
    child.once('close', code => code === 0 ? resolve() : reject(new Error(`Git ${arguments_[0]} 执行失败，历史扫描未完成`)));
  });
  completed.catch(() => {});
  child.stderr.resume();
  child.stdin.on('error', () => {});
  child.stdin.end(input);
  return { child, completed };
}

async function metadata(arguments_, input = '') {
  const { child, completed } = git(arguments_, input);
  const chunks = [];
  for await (const chunk of child.stdout) chunks.push(chunk);
  await completed;
  return Buffer.concat(chunks).toString('utf8');
}

const sensitiveName = /(?:^|\/)(?:\.env(?:\..*)?|id_rsa.*|credentials.*|\.secret|\.password|\.passwd)$|\.(?:pem|key|p12|pfx|jks|keystore)$/i;
const oidPattern = /^[a-f0-9]{40}(?:[a-f0-9]{24})?$/;

async function main() {
  // A local include or alternate object store could otherwise extend the scan
  // outside this project even though global/system configuration is disabled.
  for (const name of ['config', 'config.worktree', 'objects/info/alternates']) {
    const file = path.join(root, '.git', name);
    let content;
    try {
      if (!inside(root, await realpath(file))) throw new Error('Git 路径越出本项目，历史扫描未完成');
      content = await readFile(file, 'utf8');
    } catch (error) { if (error.code === 'ENOENT' && name !== 'config') continue; throw error; }
    if (name.endsWith('alternates') ? content.trim() : /^\s*\[include(?:If)?(?:\s|\])/im.test(content))
      throw new Error('Git 包含外部配置或对象来源，未读取其他位置');
  }
  const top = (await metadata(['rev-parse', '--show-toplevel'])).trim();
  if (path.resolve(top) !== path.resolve(root)) throw new Error('Git 根目录不匹配，未扫描其他仓库');
  if ((await metadata(['rev-parse', '--is-shallow-repository'])).trim() !== 'false')
    throw new Error('浅克隆缺少历史，不能报告历史扫描通过');

  const paths = new Map();
  const addPath = (oid, name) => {
    if (!oidPattern.test(oid) || /^0+$/.test(oid) || !name) return;
    if (!paths.has(oid)) paths.set(oid, new Set());
    paths.get(oid).add(name);
  };
  const objects = [];
  for (const line of (await metadata(['rev-list', '--all', '--objects'])).split('\n')) {
    if (!line) continue;
    const separator = line.indexOf(' ');
    const oid = separator === -1 ? line : line.slice(0, separator);
    if (!oidPattern.test(oid)) throw new Error('Git 对象列表无效，历史扫描未完成');
    objects.push(oid);
    if (separator !== -1) addPath(oid, line.slice(separator + 1));
  }
  // rev-list reports one name per object. Raw NUL-delimited changes also retain
  // deleted paths and every historical name when identical blobs were renamed.
  const changes = (await metadata(['log', '--all', '--format=', '--raw', '-z', '--no-abbrev', '--no-renames', '--root', '-m'])).split('\0');
  for (let i = 0; i < changes.length; i++) {
    const match = changes[i].trimStart().match(/^:\d{6} \d{6} ([a-f0-9]+) ([a-f0-9]+) [A-Z]\d*$/);
    if (match) {
      const name = changes[++i];
      addPath(match[1], name);
      addPath(match[2], name);
    }
  }

  const types = await metadata(['cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'], objects.join('\n') + '\n');
  const scanned = [];
  for (const line of types.trim().split('\n')) {
    if (!line) continue;
    const match = line.match(/^([a-f0-9]+) (blob|tree|commit|tag) (\d+)$/);
    if (!match) throw new Error('Git 对象不可读，历史扫描未完成');
    const size = Number(match[3]);
    if (!Number.isSafeInteger(size)) throw new Error('Git 对象大小无效，历史扫描未完成');
    if (match[2] !== 'tree') scanned.push({ oid: match[1], type: match[2], size });
  }
  const blobs = scanned.filter(object => object.type === 'blob');
  const objectTypes = new Map(scanned.map(object => [object.oid, object.type]));

  const findings = new Map();
  const record = (oid, reason) => {
    if (!findings.has(oid)) findings.set(oid, new Set());
    findings.get(oid).add(reason);
  };
  for (const { oid } of blobs) if ([...(paths.get(oid) ?? [])].some(name => sensitiveName.test(name))) record(oid, '敏感文件名');

  if (scanned.length) {
    const { child, completed } = git(['cat-file', '--batch'], scanned.map(object => object.oid).join('\n') + '\n');
    let index = 0, header = Buffer.alloc(0), remaining = 0, trailer = false, decoder, tail = '', hit = false, truncated = false;
    const inspect = text => {
      const combined = tail + text;
      // A discarded preceding word character must not create a false boundary.
      if (containsSecret((truncated ? 'a' : '') + combined)) hit = true;
      // Existing patterns require at most 44 characters to establish a match.
      // Retain the preceding boundary as well; never skip binary or large blobs.
      truncated ||= combined.length > 128;
      tail = combined.slice(-128);
    };
    for await (const chunk of child.stdout) {
      let offset = 0;
      while (offset < chunk.length) {
        if (trailer) {
          if (chunk[offset++] !== 10) throw new Error('Git 对象分隔无效，历史扫描未完成');
          index++; trailer = false; decoder = undefined;
        } else if (!decoder) {
          const end = chunk.indexOf(10, offset);
          if (end === -1) { header = Buffer.concat([header, chunk.subarray(offset)]); break; }
          const line = Buffer.concat([header, chunk.subarray(offset, end)]).toString('ascii');
          const expected = scanned[index];
          if (!expected || line !== `${expected.oid} ${expected.type} ${expected.size}`) throw new Error('Git 对象批次不匹配，历史扫描未完成');
          remaining = expected.size; header = Buffer.alloc(0); offset = end + 1;
          decoder = new StringDecoder('utf8'); tail = ''; hit = false; truncated = false;
          if (!remaining) { inspect(decoder.end()); trailer = true; }
        } else {
          const end = Math.min(chunk.length, offset + remaining);
          inspect(decoder.write(chunk.subarray(offset, end)));
          remaining -= end - offset; offset = end;
          if (!remaining) {
            inspect(decoder.end());
            if (hit) record(scanned[index].oid, '敏感内容特征');
            trailer = true;
          }
        }
      }
    }
    await completed;
    if (index !== scanned.length || header.length || decoder) throw new Error('Git 对象流不完整，历史扫描未完成');
  }
  console.log(`git history scan: ${objects.length} reachable objects, ${blobs.length} blobs, ${scanned.length - blobs.length} commit/tag records, ${findings.size} findings`);
  for (const [oid, reasons] of findings) {
    const names = objectTypes.get(oid) === 'blob' ? [...(paths.get(oid) ?? ['<无可用路径>'])].sort() : [`metadata:${objectTypes.get(oid)}`];
    for (const name of names) console.log(`${[...reasons].join('、')}: ${JSON.stringify(name)} (${oid})`);
  }
  if (findings.size) process.exitCode = 1;
  else console.log('git history checks passed: no matching filenames or content');
}

try { await main(); }
catch (error) { console.error(error.message); process.exitCode = 1; }
