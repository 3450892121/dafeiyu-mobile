// Build-computer helper for a slow official GitHub connection. The APK never
// runs this tool or needs a proxy. Authentication stays inside the GitHub CLI.
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {root, inside} from './project-lib.mjs';

const lock=JSON.parse(fs.readFileSync(path.join(root,'runtime/asr-model.lock.json'),'utf8'));
const api=new URL(lock.provenance);
if(api.origin!=='https://api.github.com'||!/^\/repos\/k2-fsa\/sherpa-onnx\/releases\/assets\/\d+$/.test(api.pathname))throw Error('Unexpected official model asset');
const work=path.join(root,'.tools/offline-work/model-parts');
const output=path.join(root,'.tools/offline-bundle/models/sensevoice.tar.bz2');
for(const file of [work,output])if(!inside(root,file))throw Error('Model output outside project');
fs.mkdirSync(work,{recursive:true});fs.mkdirSync(path.dirname(output),{recursive:true});
const hash=file=>{const h=createHash('sha256'),fd=fs.openSync(file,'r'),b=Buffer.alloc(1024*1024);try{let n;while((n=fs.readSync(fd,b,0,b.length,null)))h.update(b.subarray(0,n));}finally{fs.closeSync(fd);}return h.digest('hex');};
if(fs.existsSync(output)&&fs.statSync(output).size===lock.size&&hash(output)===lock.sha256){console.log('Fixed official model already verified');process.exit(0);}
const chunk=2*1024*1024,count=Math.ceil(lock.size/chunk);
let next=0,completed=0;
async function part(index){
  const first=index*chunk,last=Math.min(lock.size-1,first+chunk-1),size=last-first+1;
  const file=path.join(work,String(index).padStart(4,'0'));
  if(fs.existsSync(file)&&fs.statSync(file).size===size){completed++;return;}
  for(let attempt=0;attempt<3;attempt++){
    const partial=file+'.part',fd=fs.openSync(partial,'w');
    const ok=await new Promise(resolve=>{
      const child=spawn('gh',['api','--hostname','github.com',api.pathname.slice(1)+'?part='+index,'--method','GET','--header','Accept: application/octet-stream','--header',`Range: bytes=${first}-${last}`],{cwd:root,stdio:['ignore',fd,'pipe']});
      const timer=setTimeout(()=>child.kill(),180000);
      child.stderr.resume();child.once('error',()=>{clearTimeout(timer);resolve(false);});
      child.once('close',code=>{clearTimeout(timer);resolve(code===0);});
    });
    fs.closeSync(fd);
    if(ok&&fs.statSync(partial).size===size){fs.renameSync(partial,file);completed++;console.log('official model parts:',completed+'/'+count);return;}
    fs.unlinkSync(partial);
  }
  throw Error('Official model range failed: '+index);
}
await Promise.all(Array.from({length:16},async()=>{while(next<count){const index=next++;await part(index);}}));
const temporary=output+'.assembling',fd=fs.openSync(temporary,'w');
try{for(let i=0;i<count;i++){const source=fs.openSync(path.join(work,String(i).padStart(4,'0')),'r'),buffer=Buffer.alloc(1024*1024);try{let n;while((n=fs.readSync(source,buffer,0,buffer.length,null)))fs.writeSync(fd,buffer,0,n);}finally{fs.closeSync(source);}}}finally{fs.closeSync(fd);}
if(fs.statSync(temporary).size!==lock.size||hash(temporary)!==lock.sha256){fs.unlinkSync(temporary);throw Error('Assembled model does not match official fixed SHA256');}
fs.renameSync(temporary,output);
fs.rmSync(output+'.part',{force:true});
console.log('Fixed official model verified:',lock.size,lock.sha256);
