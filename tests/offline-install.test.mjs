import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {root, inside} from '../tools/project-lib.mjs';

test('actual shell APT phase pins every bundled version and encodes epoch cache names', () => {
  const build=path.join(root,'build');
  fs.mkdirSync(build,{recursive:true});
  const fixture=fs.mkdtempSync(path.join(build,'offline install fixture-'));
  assert.ok(inside(root,fs.realpathSync(fixture)));
  try {
    fs.mkdirSync(path.join(fixture,'apt/pool'),{recursive:true});
    fs.writeFileSync(path.join(fixture,'manifest.json'),'fixture');
    fs.writeFileSync(path.join(fixture,'apt/sources.list'),
      ['noble','noble-updates','noble-security'].map(suite=>
        'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] file:/opt/offline/apt '+suite+' main universe\n').join(''));
    const files=['gawk.deb','shared.deb','python.deb'];
    for(const name of files)fs.writeFileSync(path.join(fixture,'apt/pool',name),'fictional archive');
    fs.writeFileSync(path.join(fixture,'.deb-files.list'),files.map(name=>'apt/pool/'+name+'\n').join(''));
    const original=fs.readFileSync(path.join(root,'runtime/install-runtime.sh'),'utf8');
    const stop=original.indexOf('\nif [ ! -x /home/mobile/tools/bin/python3 ]; then');
    assert.ok(stop>0,'isolate only the APT phase before Python/QQ initialization');
    const phase=original.slice(0,stop);
    assert.equal(phase.split('offline=/opt/offline\n').length,2);
    const relative=path.relative(root,fixture).replaceAll('\\','/');
    const quote=value=>"'"+value.replaceAll("'","'\\''")+"'";
    // These shell functions intercept every mutation or external command in
    // this phase. The production loop runs unchanged, with fake package data;
    // only its offline fixture path is substituted. No APT/QQ process runs.
    const harness=`fixture=${quote(relative)}
mkdir() { :; }
chmod() { :; }
apt-get() { printf 'APT\\0' >> "$fixture/calls"; printf '%s\\0' "$@" >> "$fixture/calls"; printf '\\0' >> "$fixture/calls"; }
cp() { printf 'COPY\\0' >> "$fixture/calls"; printf '%s\\0' "$@" >> "$fixture/calls"; printf '\\0' >> "$fixture/calls"; }
dpkg-deb() {
  case "\${!#}" in
    *gawk.deb) printf 'gawk\\t1:5.2.1-2build3\\tarm64' ;;
    *shared.deb) printf 'x11-common\\t1:7.7+23ubuntu3\\tall' ;;
    *python.deb) printf 'python3\\t3.12.3-0ubuntu2.1\\tarm64' ;;
    *) return 99 ;;
  esac
}
${phase.replace('offline=/opt/offline\n','offline="$fixture"\n')}
`;
    const bash=process.platform==='win32'?'C:/Program Files/Git/bin/bash.exe':'bash';
    const result=spawnSync(bash,['--noprofile','--norc','-c',harness],
      {cwd:root,encoding:'utf8',timeout:10000,windowsHide:true,env:{...process.env,BASH_ENV:'',ENV:''}});
    assert.equal(result.error,undefined,result.error?.message);
    assert.equal(result.status,0,result.stderr);
    const calls=fs.readFileSync(path.join(fixture,'calls'),'utf8').split('\0\0').filter(Boolean).map(call=>call.split('\0'));
    const installs=calls.filter(call=>call[0]==='APT'&&call.includes('install'));
    assert.equal(installs.length,1);
    assert.ok(installs[0].includes('--no-download'));
    assert.ok(installs[0].includes('--no-install-recommends'));
    for(const spec of ['gawk=1:5.2.1-2build3','x11-common=1:7.7+23ubuntu3','python3=3.12.3-0ubuntu2.1'])
      assert.equal(installs[0].filter(argument=>argument===spec).length,1,spec);
    for(const name of ['python3','ffmpeg','tzdata'])assert.ok(installs[0].includes(name));
    assert.deepEqual(calls.filter(call=>call[0]==='COPY').map(call=>call[2]),[
      '/var/cache/apt/archives/gawk_1%3a5.2.1-2build3_arm64.deb',
      '/var/cache/apt/archives/x11-common_1%3a7.7+23ubuntu3_all.deb',
      '/var/cache/apt/archives/python3_3.12.3-0ubuntu2.1_arm64.deb',
    ]);
  } finally {
    assert.ok(inside(root,fs.realpathSync(fixture)));
    fs.rmSync(fixture,{recursive:true,force:true});
  }
});
