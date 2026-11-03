import fs from 'node:fs/promises';
import path from 'node:path';
import zlib from 'node:zlib';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {root,inside} from './project-lib.mjs';

const cache=path.join(root,'.tools/runtime-downloads');
const nativeOut=path.join(root,'app/build/generated/jni/arm64-v8a');
const assets=path.join(root,'app/build/generated/assets/runtime');
await Promise.all([cache,nativeOut,assets].map(p=>fs.mkdir(p,{recursive:true})));
const sha=b=>createHash('sha256').update(b).digest('hex');
async function request(url,headers={}){
  const response=await fetch(url,{headers,signal:AbortSignal.timeout(60000)});
  if(!response.ok)throw new Error('官方组件请求失败: '+new URL(url).hostname+' '+response.status);
  return Buffer.from(await response.arrayBuffer());
}
async function download(url,file,hash,headers={}){
  const target=path.join(cache,file);
  if(!inside(cache,target))throw new Error('下载路径越界');
  let bytes;
  try{bytes=await fs.readFile(target);if(hash&&sha(bytes)!==hash)bytes=null;}catch{}
  if(!bytes){bytes=await request(url,headers);if(hash&&sha(bytes)!==hash)throw new Error('组件校验失败: '+file);await fs.writeFile(target,bytes);}
  return {file:target,sha256:sha(bytes),size:bytes.length};
}

const selected=JSON.parse(await fs.readFile(path.join(root,'runtime/native.lock.json'),'utf8'));
const components=[];
for(const name of ['libandroid-shmem','libtalloc','proot']){
  const info=selected[name];
  const url='https://packages.termux.dev/apt/termux-main/'+info.Filename;
  const item=await download(url,name+'.deb',info.SHA256);
  const extract=path.join(cache,name);await fs.mkdir(extract,{recursive:true});
  execFileSync('tar.exe',['-xf',item.file,'-C',extract]);
  const dataTar=(await fs.readdir(extract)).find(n=>n.startsWith('data.tar'));
  const dataArchive=path.join(extract,dataTar);
  const listing=execFileSync('tar.exe',['-tvf',dataArchive],{encoding:'utf8',maxBuffer:4*1024*1024});
  const regular=listing.split('\n').filter(line=>line.startsWith('-')).map(line=>line.slice(line.indexOf('./data/')).trim()).filter(line=>line.startsWith('./data/'));
  let files;
  if(name==='proot') files=[['bin/proot','libproot.so'],['libexec/proot/loader','libproot-loader.so']];
  else if(name==='libtalloc') files=[['lib/libtalloc.so','libtalloc.so']];
  else files=[['lib/libandroid-shmem.so','libandroid-shmem.so']];
  for(const [from,to]of files){
    // Read regular ELF members without creating Linux symlinks on Windows.
    const prefix='./data/data/com.termux/files/usr/'+from;
    const member=regular.find(n=>n===prefix)||regular.find(n=>n.startsWith(prefix+'.'));
    if(!member)throw new Error('原生产物缺失: '+name+'/'+from);
    const data=execFileSync('tar.exe',['-xOf',dataArchive,member],{maxBuffer:16*1024*1024});
    if(data.readUInt32LE(0)!==0x464c457f||data.readUInt16LE(18)!==183)throw new Error('原生组件不是 ARM64 ELF: '+to);
    await fs.writeFile(path.join(nativeOut,to),data);
  }
  components.push({name,version:info.Version,url,sha256:item.sha256,license:name==='proot'?'GPL-2.0-or-later':name==='libtalloc'?'LGPL-3.0-or-later (library); GPL-3.0 (package/build files)':'BSD-3-Clause'});
  console.log('native component verified: '+name+' '+info.Version);
}

// Official Ubuntu Base archive avoids a dependency on Docker registry availability.
const baseUrl='https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/';
const baseName='ubuntu-base-24.04.5-base-arm64.tar.gz';
const sums=(await request(baseUrl+'SHA256SUMS')).toString();
const baseHash=sums.split('\n').find(line=>line.endsWith(baseName))?.slice(0,64);
if(!/^[a-f0-9]{64}$/.test(baseHash||''))throw new Error('Ubuntu 官方清单缺少摘要');
const base=await download(baseUrl+baseName,'base-arm64.tar.gz',baseHash);
components.push({name:'ubuntu-base-arm64',version:'24.04.5',sha256:base.sha256,size:base.size,source:baseUrl+baseName});
const nativeFiles=[];
for(const file of await fs.readdir(nativeOut)){const b=await fs.readFile(path.join(nativeOut,file));nativeFiles.push({file,sha256:sha(b),size:b.length});}
await fs.writeFile(path.join(root,'runtime/components.lock.json'),JSON.stringify({schemaVersion:1,components,nativeFiles},null,2)+'\n');
await fs.copyFile(path.join(root,'runtime/components.lock.json'),path.join(assets,'components.lock.json'));
console.log('official Ubuntu root filesystem verified: '+base.size+' bytes');
