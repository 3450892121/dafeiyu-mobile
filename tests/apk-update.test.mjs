import test from 'node:test';
import assert from 'node:assert/strict';
import { checkUpdate, readIdentity, signingAction } from '../tools/apk-update.mjs';

const signer = '712e3330c2458f2de628782ab5e2bdee722e5ab1442005146d0f4c9c86f0a712';
const independentSigner = '1'.repeat(64);
const prior = { signer, version: '0.3.1', code: 16 };
const current = { signer, version: '0.3.2', code: 17 };
const options = { independent: false, pin: null, version: '0.3.2', code: '17', previous: prior };
const certs = 'Signer #1 certificate SHA-256 digest: ' + signer;

test('only an explicit first independent build can generate a signing key', () => {
  const none = { key: false, previous: false, pin: false };
  for (const mode of ['official', 'independent']) assert.throws(() => signingAction(mode, none));
  assert.equal(signingAction('new-independent', none), 'create');
  for (const evidence of [{ pin: true }, { previous: true }]) {
    assert.throws(() => signingAction('new-independent', { ...none, ...evidence }));
    assert.throws(() => signingAction('independent', { ...none, ...evidence }));
    assert.equal(signingAction('independent', { ...none, ...evidence, key: true }), 'use');
  }
  assert.equal(signingAction('official', { ...none, key: true }), 'use');
  assert.equal(signingAction('new-independent', { ...none, key: true }), 'use');
  assert.throws(() => signingAction('independent', { ...none, key: true }));
});

test('read SDK tool output and reject unexpected packages, malformed metadata or extra signers', () => {
  const manifest = "package: name='app.dafeiyu.mobile' versionCode='16' versionName='0.3.1' platformBuildVersionName='15'\r\n";
  assert.deepEqual(readIdentity(manifest, certs), prior);
  for (const wrong of [manifest.replace('app.dafeiyu.mobile', 'app.dafeiyu.else'), manifest.replace('16', '0'), '']) {
    assert.throws(() => readIdentity(wrong, certs));
  }
  assert.throws(() => readIdentity(manifest, ''));
  assert.throws(() => readIdentity(manifest, certs + '\r\n' + certs.replace('#1', '#2')));
});

test('official builds match the released certificate and the signed previous APK', () => {
  assert.doesNotThrow(() => checkUpdate(current, options));
  assert.doesNotThrow(() => checkUpdate(prior, { ...options, previous: null, version: '0.3.1', code: 16 }));
  assert.throws(() => checkUpdate({ ...current, signer: independentSigner }, options), /official/);
  assert.throws(() => checkUpdate(current, { ...options, previous: { ...prior, signer: independentSigner } }), /different signer/);
  assert.throws(() => checkUpdate(current, { ...options, version: '0.3.3' }), /version.properties/);
});

test('changed version requires a higher code, rebuild requires the original code, and older releases are refused', () => {
  for (const previous of [{ ...prior, code: 17 }, { ...current, code: 16 }, { ...prior, version: '0.3.12' }]) {
    assert.throws(() => checkUpdate(current, { ...options, previous }), /rollback|reused/);
  }
  assert.doesNotThrow(() => checkUpdate(current, { ...options, previous: current }));
});

test('independent builds bind to their own public signer and previous version', () => {
  const candidate = { ...current, signer: independentSigner };
  const independent = { ...options, independent: true, pin: independentSigner, previous: { ...prior, signer: independentSigner } };
  assert.doesNotThrow(() => checkUpdate(candidate, independent));
  assert.throws(() => checkUpdate(current, independent), /official build mode/);
  assert.throws(() => checkUpdate(candidate, { ...independent, pin: signer }), /pin mismatch/);
  assert.throws(() => checkUpdate(candidate, { ...independent, pin: '' }), /pin mismatch/);
  assert.throws(() => checkUpdate(candidate, { ...independent, previous: prior }), /different signer/);
});
