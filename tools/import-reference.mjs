import { readdir, readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { root, inside, containsSecret } from './project-lib.mjs';

const reference = path.resolve(root, '..', '大肥鱼服务器版');
const source = path.join(reference, '02-服务器版/bot');
const destination = path.join(root, 'runtime/core');
const excluded = new Set(['ops_api.py', 'bot_registry.py', 'version.py']);
const entries = (await readdir(source, {withFileTypes:true})).filter(entry => entry.isFile() && /\.(?:py|html)$/.test(entry.name) && !excluded.has(entry.name));
const files = [];
const snapshots = [];
for (const entry of entries) {
  const from = path.join(source, entry.name), to = path.join(destination, entry.name);
  if (!inside(source, from) || !inside(destination, to)) throw new Error('源码路径越界');
  const data = await readFile(from);
  if (containsSecret(data.toString('utf8'))) throw new Error('参考源码有敏感特征，停止导入：' + entry.name);
  snapshots.push({from,to,data});
  files.push({path:entry.name,source:'02-服务器版/bot/'+entry.name,sha256:createHash('sha256').update(data).digest('hex')});
}
// Read the complete allowed files before writing only to this new project.
for (const item of snapshots) {
  const current = await readFile(item.from);
  if (!current.equals(item.data)) throw new Error('参考源码在读取期间变化，未执行导入');
}
await mkdir(destination,{recursive:true});
for (const item of snapshots) await writeFile(item.to,item.data,{flag:'wx'});
await writeFile(path.join(destination,'UPSTREAM-LICENSE'),await readFile(path.join(reference,'LICENSE')),{flag:'wx'});
await writeFile(path.join(root,'runtime/reference-manifest.json'),JSON.stringify({source:'大肥鱼服务器版',license:'MIT',files},null,2)+'\n');
console.log('read-only reference imported into the new project: '+files.length+' source files; no runtime data');
