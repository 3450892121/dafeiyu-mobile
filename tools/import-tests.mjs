// One-time read-only import of MIT behavioral regressions; never used by APK builds.
import {readFile,writeFile,readdir,mkdir}from'node:fs/promises';
import path from'node:path';import{createHash}from'node:crypto';
import{root,containsSecret}from'./project-lib.mjs';
const reference=path.resolve(root,'..','大肥鱼服务器版','02-服务器版','tests');
const out=path.join(root,'tests/ported');await mkdir(out,{recursive:true});
const exclude=/backup|snapshot|bot_registry|ops_unified|storage_logrotate|storage_maintenance|model_call_budget|console|voice|cover/;
const manifest=[];
for(const name of await readdir(reference)){
  if(!/^test_.*\.py$/.test(name)||exclude.test(name))continue;
  const original=await readFile(path.join(reference,name),'utf8');
  if(containsSecret(original))throw Error('测试有敏感内容：'+name);
  let text=original.replace(/parents\[1\]\s*\/\s*(['"])bot\1/g,"parents[2] / 'runtime' / 'core'")
    .replace(/(['"])\.\.\1,\s*(['"])bot\2/g,"'..', '..', 'runtime', 'core'")
    .replace(/from console import/g,'from console_base import').replace(/^import console$/gm,'import console_base as console');
  await writeFile(path.join(out,name),text,{flag:'wx'});
  manifest.push({file:name,source:'02-服务器版/tests/'+name,sha256:createHash('sha256').update(original).digest('hex')});
}
await writeFile(path.join(root,'tests/ported-sources.json'),JSON.stringify({license:'MIT',files:manifest},null,2)+'\n');
console.log('read-only source test import: '+manifest.length);
