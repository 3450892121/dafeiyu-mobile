import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { root, loadCatalogue, validateCatalogue, validateReference, sourceFiles, containsSecret, modules } from './project-lib.mjs';

const errors = [];
const catalogue = await loadCatalogue();
errors.push(...validateCatalogue(catalogue));
for(const feature of catalogue.features)for(const file of [...(feature.implementation??[]),...(feature.offlineTests??[])]){
  if(file.includes('..')||path.isAbsolute(file)){errors.push('实现证据路径越界：'+feature.id);continue;}
  try{if(!(await stat(path.join(root,file))).isFile())errors.push('实现证据文件缺失：'+file);}catch{errors.push('实现证据文件缺失：'+file);}
}
const requiredFiles = [
  'AGENTS.md', '.gitignore', 'README.md', 'LICENSE', 'THIRD-PARTY-NOTICES.md', 'version.properties',
  'spec/00-overview.md', 'spec/acceptance.md', 'spec/qq-access.md',
  ...modules.map(module => 'spec/modules/' + module + '.md'),
  'app/src/main/AndroidManifest.xml', 'tools/build-apk.ps1',
];
for (const file of requiredFiles) {
  try { if (!(await stat(path.join(root, file))).isFile()) errors.push('缺少文件：' + file); }
  catch { errors.push('缺少文件：' + file); }
}
const agents = await readFile(path.join(root, 'AGENTS.md'), 'utf8');
for (const heading of ['本文件的边界', '项目概览', '记忆与交接', '仓库结构', '执行纪律', '目标规格', '事后记忆', '构建与测试命令', '改动与提交', '测试与发布', '版本迭代', '安全红线', '收尾清单'])
  if (!agents.includes('## ' + heading)) errors.push('规则缺少章节：' + heading);
const manifest = await readFile(path.join(root, 'app/src/main/AndroidManifest.xml'), 'utf8');
const allowedPermissions=new Set(['INTERNET','ACCESS_NETWORK_STATE','FOREGROUND_SERVICE','WAKE_LOCK','RECEIVE_BOOT_COMPLETED','POST_NOTIFICATIONS']);
for(const match of manifest.matchAll(/<uses-permission\s+android:name="android.permission.([A-Z_]+)"/g))
  if(!allowedPermissions.has(match[1]))errors.push('申请了范围外权限：'+match[1]);
if (!manifest.includes('android:allowBackup="false"')) errors.push('初始化应用应禁止自动备份');
for (const file of await sourceFiles()) {
  const relative = path.relative(root, file).replaceAll('\\', '/');
  if (/(?:^|\/)(?:\.env(?:\..*)?|id_rsa.*|credentials.*|\.secret|\.password|\.passwd)$|\.(?:pem|key|p12|pfx|jks|keystore)$/i.test(relative))
    errors.push('发现敏感文件名：' + relative);
  const text = await readFile(file, 'utf8');
  if (containsSecret(text)) errors.push('发现敏感内容特征：' + relative);
  if (text.includes(['<待', '补：'].join(''))) errors.push('残留占位：' + relative);
}
if (process.argv.includes('--reference')) {
  try { console.log('reference sources checked read-only: ' + await validateReference(catalogue)); }
  catch (error) { errors.push(error.message); }
}
if (errors.length) {
  errors.forEach(error => console.error(error));
  process.exitCode = 1;
} else {
  console.log('project checks passed');
  console.log('secret scan: no matching files or content');
}
