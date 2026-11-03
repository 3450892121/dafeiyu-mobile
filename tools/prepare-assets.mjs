import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { root, loadCatalogue, publicCatalogue, validateCatalogue } from './project-lib.mjs';

const catalogue = await loadCatalogue();
const errors = validateCatalogue(catalogue);
if (errors.length) throw new Error(errors.join('\n'));
const destination = path.join(root, 'app/build/generated/assets');
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
