// Corresponding, unmodified source for the native executable aggregate in the APK.
import fs from'node:fs/promises';import path from'node:path';import{createHash}from'node:crypto';
import{root}from'./project-lib.mjs';
import{execFileSync}from'node:child_process';
const lock=JSON.parse(await fs.readFile(path.join(root,'runtime/native-sources.lock.json'),'utf8'));
const output=path.join(root,'.tools/native-sources');await fs.mkdir(output,{recursive:true});
const check=process.argv.includes('--check');
async function fetchFile(url,target){
  if(check)throw Error('Missing verified source: '+path.basename(target));
  execFileSync(process.platform==='win32'?'curl.exe':'curl',['--fail','--silent','--show-error','--location','--max-time','180','--proto','=https','--proto-redir','=https','--output',target,url]);
  return fs.readFile(target);
}
for(const entry of lock.sources){
  const target=path.join(output,entry.file);let bytes;
  try{bytes=await fs.readFile(target);}catch{}
  const hash=b=>createHash('sha256').update(b).digest('hex');
  if(!bytes||hash(bytes)!==entry.sha256){bytes=await fetchFile(entry.url,target);if(hash(bytes)!==entry.sha256)throw Error(entry.name+' source hash mismatch');}
  console.log('corresponding source verified: '+entry.name);
}
const recipes=JSON.parse(await fs.readFile(path.join(root,'runtime/native-build-recipes.lock.json'),'utf8'));
for(const recipe of recipes.files){
  if(!/^[A-Za-z0-9_.-]+$/.test(recipe.package)||!/^[A-Za-z0-9_.-]+$/.test(recipe.file))throw Error('Unsafe source recipe path');
  const target=path.join(output,'recipes',recipe.package,recipe.file);await fs.mkdir(path.dirname(target),{recursive:true});
  let bytes;try{bytes=await fs.readFile(target);}catch{}
  if(!bytes||createHash('sha256').update(bytes).digest('hex')!==recipe.sha256){
    const temporary=target+'.json';const response=JSON.parse((await fetchFile(recipe.url,temporary)).toString('utf8'));
    bytes=Buffer.from(response.content,'base64');if(createHash('sha256').update(bytes).digest('hex')!==recipe.sha256)throw Error('Source recipe hash mismatch');
    await fs.writeFile(target,bytes);await fs.unlink(temporary);
  }
}
await fs.copyFile(path.join(root,'runtime/native-sources.lock.json'),path.join(output,'native-sources.lock.json'));
await fs.copyFile(path.join(root,'THIRD-PARTY-NOTICES.md'),path.join(output,'THIRD-PARTY-NOTICES.md'));
