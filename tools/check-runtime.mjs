import fs from'node:fs/promises';import path from'node:path';import{createHash}from'node:crypto';import{execFileSync}from'node:child_process';import{root,inside}from'./project-lib.mjs';
const hash=b=>createHash('sha256').update(b).digest('hex');
const lock=JSON.parse(await fs.readFile(path.join(root,'runtime/components.lock.json'),'utf8'));
const nativeDirectory=path.join(root,'app/build/generated/jni/arm64-v8a');
const report=[];
for(const entry of lock.nativeFiles){
  const file=path.join(nativeDirectory,entry.file);if(!inside(nativeDirectory,file))throw Error('Native path escapes');
  const bytes=await fs.readFile(file);if(hash(bytes)!==entry.sha256||bytes.length!==entry.size)throw Error('Native hash mismatch: '+entry.file);
  if(bytes.readUInt32LE(0)!==0x464c457f||bytes[4]!==2||bytes.readUInt16LE(18)!==183)throw Error('Expected ARM64 ELF: '+entry.file);
  const offset=Number(bytes.readBigUInt64LE(32)),size=bytes.readUInt16LE(54),count=bytes.readUInt16LE(56);const align=[];
  for(let i=0;i<count;i++){const p=offset+i*size;if(bytes.readUInt32LE(p)===1)align.push(Number(bytes.readBigUInt64LE(p+48)));}
  report.push({file:entry.file,arm64:true,loadAlignments:align});
}
const base=lock.components.find(c=>c.name==='ubuntu-base-arm64');
const archive=path.join(root,'.tools/runtime-downloads/base-arm64.tar.gz');if(hash(await fs.readFile(archive))!==base.sha256)throw Error('Ubuntu base archive mismatch');
const qq=JSON.parse(await fs.readFile(path.join(root,'runtime/qq-components.lock.json'),'utf8'));
for(const [name,cached]of[['qq','qq-arm64.deb'],['napcat','napcat-complete.zip']]){const bytes=await fs.readFile(path.join(root,'.tools/downloads',cached));if(hash(bytes)!==qq[name].sha256||bytes.length!==qq[name].size)throw Error('Official '+name+' package mismatch');}
const source=await fs.readFile(path.join(root,'runtime/mobile_console.py'),'utf8');
for(const marker of['127.0.0.1','httpServers','httpClients','messagePostFormat','mobile_autostart','get_status'])if(!source.includes(marker))throw Error('Missing QQ adapter setting');
await fs.mkdir(path.join(root,'build'),{recursive:true});await fs.writeFile(path.join(root,'build/runtime-component-checks.json'),JSON.stringify({native:report,ubuntu:true,qq:true,napcat:true},null,2)+'\n');
console.log('runtime components checked: ARM64 ELF, all hashes, QQ and NapCat archive structure');
console.log('ELF load alignment: '+report.map(x=>x.file+'='+x.loadAlignments.join('/')).join(', '));
