import fs from'node:fs/promises';import path from'node:path';import{execFileSync}from'node:child_process';import{createHash}from'node:crypto';import{root}from'./project-lib.mjs';
const output=path.join(root,'.tools/native-sources');const records=[];
for(const [name,file]of[['proot','proot'],['libtalloc','talloc'],['libandroid-shmem','shmem']]){
  const entries=JSON.parse(await fs.readFile(path.join(output,file+'-recipe.json'),'utf8'));
  for(const entry of entries){
    if(entry.type!=='file'||!(/^[A-Za-z0-9_.-]+$/.test(entry.name))||!entry.git_url.startsWith('https://api.github.com/repos/termux/termux-packages/git/blobs/'))throw Error('Invalid official recipe metadata');
    const directory=path.join(output,'recipes',name);await fs.mkdir(directory,{recursive:true});
    const response=path.join(directory,entry.name+'.json');
    execFileSync('curl.exe',['--fail','--silent','--show-error','--location','--max-time','60','--output',response,entry.git_url]);
    const blob=JSON.parse(await fs.readFile(response,'utf8'));if(blob.encoding!=='base64')throw Error('Unexpected blob encoding');
    const bytes=Buffer.from(blob.content,'base64');
    const gitHash=createHash('sha1').update(Buffer.concat([Buffer.from('blob '+bytes.length+'\0'),bytes])).digest('hex');
    if(gitHash!==entry.sha)throw Error('Official recipe hash mismatch');
    await fs.writeFile(path.join(directory,entry.name),bytes);await fs.unlink(response);
    records.push({package:name,file:entry.name,url:entry.git_url,gitBlob:gitHash,sha256:createHash('sha256').update(bytes).digest('hex')});
  }
}
await fs.writeFile(path.join(root,'runtime/native-build-recipes.lock.json'),JSON.stringify({schemaVersion:1,files:records},null,2)+'\n');
console.log('official source build recipes verified: '+records.length);
