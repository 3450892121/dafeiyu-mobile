import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { root, loadCatalogue, validateCatalogue, publicCatalogue, containsSecret, inside } from '../tools/project-lib.mjs';

test('功能清单完整且每项具有来源、边界和验收方法', async () => {
  assert.deepEqual(validateCatalogue(await loadCatalogue()), []);
});

test('不能把视频理解或其他必做项静默改为可省略', async () => {
  const data = await loadCatalogue();
  data.features.find(feature => feature.id === 'video-understanding').requirement = 'optional_if_infeasible';
  assert.ok(validateCatalogue(data).some(error => error.includes('未经授权改变功能范围')));
});

test('不能把缺少验收证据的功能标为完成', async () => {
  const data = await loadCatalogue();
  data.features[0].status = 'verified';
  assert.ok(validateCatalogue(data).some(error => error.includes('验收缺少证据')));
});

test('有条件省略也需要记录原因', async () => {
  const data = await loadCatalogue();
  data.features.find(feature => feature.id === 'cover').status = 'omitted';
  assert.ok(validateCatalogue(data).some(error => error.includes('省略缺少授权或理由')));
});

test('清单来源路径不能越出指定的参考代码范围', async () => {
  const data = await loadCatalogue();
  data.features[0].sources = ['../../其他项目/config.json'];
  assert.ok(validateCatalogue(data).some(error => error.includes('来源越界')));
});

test('应用资源只带展示字段，不携带参考项目路径', async () => {
  const data = publicCatalogue(await loadCatalogue());
  assert.equal(Object.hasOwn(data, 'referenceProject'), false);
  assert.ok(data.features.every(feature => !Object.hasOwn(feature, 'sources')));
  assert.equal(data.features.filter(feature => feature.status === 'verified').length, 0);
});

test('敏感扫描仅报告是否命中，不返回密钥内容', () => {
  assert.equal(containsSecret('sk-' + 'x'.repeat(30)), true);
  assert.equal(containsSecret('github_pat_' + 'x'.repeat(30)), true);
  assert.equal(containsSecret('AKIA' + 'X'.repeat(16)), true);
  assert.equal(containsSecret('sk- 只是格式说明'), false);
});

test('构建输出范围不能指向参考项目或相邻目录', () => {
  assert.equal(inside(root, path.join(root, 'build')), true);
  assert.equal(inside(root, path.resolve(root, '../大肥鱼服务器版')), false);
  assert.equal(inside(root, root + '-其他'), false);
});

test('基础应用没有联网、QQ 注入或读取其他应用数据的权限', async () => {
  const manifest = await readFile(path.join(root, 'app/src/main/AndroidManifest.xml'), 'utf8');
  assert.doesNotMatch(manifest, /<uses-permission\b/);
  assert.match(manifest, /android:allowBackup="false"/);
});
