import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { root, containsSecret } from './project-lib.mjs';
const reference = path.resolve(root,'..','大肥鱼服务器版');
const manifestPath = path.join(root,'runtime/reference-manifest.json');
const manifest = JSON.parse(await readFile(manifestPath,'utf8'));
for (const [source,target] of [
  ['02-服务器版/console.py','runtime/console_base.py'],
  ['02-服务器版/bot/ops_api.py','runtime/core/ops_base.py']
]) {
  const data = await readFile(path.join(reference,source));
  if(containsSecret(data.toString('utf8'))) throw new Error('敏感内容，未导入 '+source);
  await writeFile(path.join(root,target),data,{flag:'wx'});
  manifest.files.push({path:target,source,sha256:createHash('sha256').update(data).digest('hex')});
}
await writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
async function patch(file,replacements){
  let value=await readFile(path.join(root,file),'utf8');
  for(const [before,after] of replacements){
    if(!value.includes(before))throw new Error('适配位置不存在 '+file);
    value=value.replace(before,after);
  }
  await writeFile(path.join(root,file),value,'utf8');
}
await patch('runtime/core/config.py',[
  ['DEFAULT_MODELS = [\n    {', 'DEFAULT_MODELS = [\n    {'],
  ['return "/home/dafeiyu/DafeiyuBot/data/shared_models.json"','return os.path.join(DATA_DIR, "models.json")']
]);
await patch('runtime/core/bili_bridge.py',[
  ['or "/opt/dafeiyu/bili-venv/bin/bili"','or os.path.join(BOT_HOME, "tools", "bin", "bili")'],
  ['from util import log','from util import log, BOT_HOME'],
  ['or "/home/dafeiyu/DafeiyuBot/bili"','or os.path.join(BOT_HOME, "bili")'],
  ['env["HOME"] = "/home/dafeiyu"','env["HOME"] = BOT_HOME']
]);
await patch('runtime/core/video_reader.py',[
  ['or "/opt/dafeiyu/venv/bin/yt-dlp"','or "yt-dlp"']
]);
await patch('runtime/console_base.py',[
  ['os.path.join(BOT_DIR, "bot")','os.path.join(BOT_DIR, "core")'],
  ['BRAIN = os.path.join(BOT_DIR, "bot", "main.py")','BRAIN = os.path.join(BOT_DIR, "mobile_brain.py")'],
  ['os.path.join(BOT_DIR, "bot", "dashboard.html")','os.path.join(BOT_DIR, "core", "dashboard.html")'],
  ['os.path.join(BOT_DIR, "bot", "ops.html")','os.path.join(BOT_DIR, "core", "ops.html")'],
  ['os.path.join(BOT_DIR, "bot", "video.html")','os.path.join(BOT_DIR, "core", "video.html")'],
  ['os.path.join(BOT_DIR, "bot", "kb.html")','os.path.join(BOT_DIR, "core", "kb.html")'],
  ['or "/home/dafeiyu/DafeiyuBot/data"','or DATA_DIR'],
  ['cwd=os.path.join(BOT_DIR, "bot")','cwd=BOT_DIR']
]);
let ops=await readFile(path.join(root,'runtime/core/ops_base.py'),'utf8');
ops=ops.replace(/DEFAULT_BOTS = \[[\s\S]*?\n\]/,'DEFAULT_BOTS = []');
await writeFile(path.join(root,'runtime/core/ops_base.py'),ops,'utf8');
console.log('independent mobile paths prepared; reference files unchanged');
