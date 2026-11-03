import { mkdir, readFile, writeFile, readdir, rm } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { root, loadCatalogue, publicCatalogue, validateCatalogue } from './project-lib.mjs';

const catalogue = await loadCatalogue();
const errors = validateCatalogue(catalogue);
if (errors.length) throw new Error(errors.join('\n'));
const destination = path.join(root, 'app/build/generated/assets');
// Ubuntu is fetched directly from its publisher on the phone; don't repackage its binaries.
if(!process.argv.includes('--check'))await rm(path.join(destination,'runtime/base-arm64.tar.gz'),{force:true});
const file = path.join(destination, 'features.json');
const content = JSON.stringify(publicCatalogue(catalogue), null, 2) + '\n';
if (process.argv.includes('--check')) {
  if (await readFile(file, 'utf8') !== content) throw new Error('应用清单和规格不一致，请先生成资源');
  console.log('catalogue assets match');
} else {
  await mkdir(destination, { recursive: true });
  await writeFile(file, content, 'utf8');
  console.log('catalogue assets prepared: ' + catalogue.features.length);
}

const version=Object.fromEntries((await readFile(path.join(root,'version.properties'),'utf8')).trim().split(/\r?\n/).map(line=>line.split('=')));
const payload=path.join(destination,'runtime/payload');
const entries=[];
async function copyRuntime(relative=''){
  for(const item of await readdir(path.join(root,'runtime',relative),{withFileTypes:true})){
    if(item.name==='__pycache__'||item.name.startsWith('.'))continue;
    const name=path.posix.join(relative,item.name);
    if(item.isSymbolicLink())throw Error('运行源码不能含链接：'+name);
    if(item.isDirectory()){await copyRuntime(name);continue;}
    if(!/\.(?:py|html|json|txt|sh)$/.test(name)&&item.name!=='UPSTREAM-LICENSE')continue;
    const data=await readFile(path.join(root,'runtime',name));
    if(name.endsWith('.sh')&&data.includes(Buffer.from('\r\n')))throw Error('Linux 安装脚本必须使用 LF 换行：'+name);
    entries.push({path:name,sha256:createHash('sha256').update(data).digest('hex')});
    if(!process.argv.includes('--check')){await mkdir(path.dirname(path.join(payload,name)),{recursive:true});await writeFile(path.join(payload,name),data);}
    else if(!data.equals(await readFile(path.join(payload,name))))throw Error('运行源码资源不同步：'+name);
  }
}
await copyRuntime();
const release=Buffer.from(JSON.stringify({versionName:version.versionName,versionCode:Number(version.versionCode)})+'\n');
entries.push({path:'release.json',sha256:createHash('sha256').update(release).digest('hex')});
entries.sort((a,b)=>a.path.localeCompare(b.path));
const manifest={files:entries,sha256:createHash('sha256').update(JSON.stringify(entries)).digest('hex')};
if(!process.argv.includes('--check')){
  await writeFile(path.join(payload,'release.json'),release);
  await writeFile(path.join(destination,'runtime/payload-manifest.json'),JSON.stringify(manifest,null,2)+'\n');
  for(const name of ['layout.json','components.lock.json'])await writeFile(path.join(destination,'runtime',name),await readFile(path.join(root,'runtime',name)));
}else{
  if(!release.equals(await readFile(path.join(payload,'release.json'))))throw Error('版本资源不同步');
  if(JSON.stringify(JSON.parse(await readFile(path.join(destination,'runtime/payload-manifest.json'),'utf8')))!==JSON.stringify(manifest))throw Error('运行资源目录不同步');
}
console.log('runtime payload verified: '+entries.length);
if(!process.argv.includes('--check')){
  const notices=[await readFile(path.join(root,'THIRD-PARTY-NOTICES.md'),'utf8')];
  for(const name of (await readdir(path.join(root,'runtime/licenses'))).sort())notices.push('\n\n'+name+'\n'+await readFile(path.join(root,'runtime/licenses',name),'utf8'));
  notices.push('\n\nUpstream business source MIT\n'+await readFile(path.join(root,'runtime/core/UPSTREAM-LICENSE'),'utf8'));
  await writeFile(path.join(destination,'licenses.txt'),notices.join('\n'),'utf8');
}
