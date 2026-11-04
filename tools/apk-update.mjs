import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { root } from './project-lib.mjs';

// Public certificate and package metadata verified from the distributed 0.3.1 APK.
const official = {
  signer: '712e3330c2458f2de628782ab5e2bdee722e5ab1442005146d0f4c9c86f0a712',
  version: '0.3.1', code: 16,
};
const versionPattern = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

function compareVersions(left, right) {
  if (!versionPattern.test(left) || !versionPattern.test(right)) throw Error('Expected a numeric release version.');
  const a = left.split('.').map(Number), b = right.split('.').map(Number);
  return a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
}

export function signingAction(mode, { key, pin, previous }) {
  if (mode === 'official' && !key) throw Error('Official signing key is missing. Restore the original key before building an update.');
  if (mode === 'new-independent') {
    if (pin || previous) throw Error('Independent signer already initialized. Use -IndependentSigning with its original key.');
    return key ? 'use' : 'create';
  }
  if (mode === 'independent' && (!key || (!pin && !previous))) {
    throw Error('Restore the independent key and previous APK or signer pin. For a first build use -NewIndependentSigner.');
  }
  if (!['independent', 'official'].includes(mode)) throw Error('Unknown APK signing mode.');
  return 'use';
}

export function readIdentity(badging, certificates) {
  const pkg = /^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'/m.exec(badging);
  const signers = [...certificates.matchAll(/^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]{64})\s*$/gmi)];
  if (!pkg || pkg[1] !== 'app.dafeiyu.mobile' || !versionPattern.test(pkg[3]) || !Number.isSafeInteger(Number(pkg[2])) || Number(pkg[2]) < 1) {
    throw Error('Unexpected package ID, versionCode or versionName in the APK.');
  }
  if (signers.length !== 1) throw Error('The APK must have exactly one verified signer.');
  return { signer: signers[0][1].toLowerCase(), version: pkg[3], code: Number(pkg[2]) };
}

export function checkUpdate(candidate, { version, code, independent, pin, previous }) {
  if (candidate.version !== version || candidate.code !== Number(code)) throw Error('APK version differs from version.properties.');
  if (!independent && candidate.signer !== official.signer) throw Error('Wrong official APK signing certificate.');
  if (independent && candidate.signer === official.signer) throw Error('Use the official build mode for this certificate.');
  if (pin !== null && (!/^[a-f0-9]{64}$/.test(pin) || candidate.signer !== pin)) throw Error('Independent signer pin mismatch.');
  for (const baseline of [...(independent ? [] : [official]), ...(previous ? [previous] : [])]) {
    if (candidate.signer !== baseline.signer) throw Error('Previous APK uses a different signer.');
    const direction = compareVersions(candidate.version, baseline.version);
    if (direction < 0 || (direction === 0 ? candidate.code !== baseline.code : candidate.code <= baseline.code)) {
      throw Error('APK version rollback or reused versionCode for a different release.');
    }
  }
}

function previousApk(dir, independent) {
  const pattern = independent ? /^dafeiyu-mobile-(\d+\.\d+\.\d+)-independent-arm64\.apk$/ : /^dafeiyu-mobile-(\d+\.\d+\.\d+)-arm64\.apk$/;
  const versions = fs.readdirSync(dir).map(name => [name, pattern.exec(name)?.[1]]).filter(([, version]) => version);
  versions.sort((a, b) => compareVersions(b[1], a[1]));
  return versions[0];
}

function main([command, mode, java, apksigner, aapt, candidatePath, version, code]) {
  const independent = mode !== 'official';
  const dir = path.join(root, 'dist', independent ? 'independent' : '');
  const signingDir = path.join(root, '.local', 'signing');
  const pinFile = path.join(signingDir, 'independent-certificate.sha256');
  const pin = independent && fs.existsSync(pinFile) ? fs.readFileSync(pinFile, 'utf8').trim() : null;
  const previous = previousApk(dir, independent);
  const key = fs.existsSync(path.join(signingDir, independent ? 'independent.jks' : 'development.jks'));
  const action = signingAction(mode, { key, pin: pin !== null, previous: Boolean(previous) });
  if (command === 'plan') { console.log(action); return; }
  if (command !== 'verify') throw Error('Expected plan or verify.');
  function identity(apk) {
    // AAPT on Windows needs relative arguments in workspaces with non-ASCII paths.
    const arg = path.relative(root, apk).replaceAll('\\', '/');
    const options = { cwd: root, encoding: 'utf8', windowsHide: true };
    const badging = execFileSync(aapt, ['dump', 'badging', arg], options);
    const certs = execFileSync(java, ['-jar', apksigner, 'verify', '--print-certs', arg], options);
    return readIdentity(badging, certs);
  }
  const candidate = identity(candidatePath), baseline = previous ? identity(path.join(dir, previous[0])) : null;
  if (baseline && baseline.version !== previous[1]) throw Error('Previous APK filename does not match its signed version.');
  checkUpdate(candidate, { version, code, independent, pin, previous: baseline });
  if (independent && pin === null) fs.writeFileSync(pinFile, candidate.signer + '\n', { flag: 'wx' });
  console.log('APK update identity verified:', candidate.version, 'code', candidate.code, 'certificate', candidate.signer);
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try { main(process.argv.slice(2)); } catch (error) { console.error(error.message); process.exitCode = 1; }
}
