import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const page=readFileSync(new URL('../runtime/core/dashboard.html',import.meta.url),'utf8');
const normalize=page.match(/function normalizeModels\(list\)\{[\s\S]*?\n\}/)?.[0];
const save=page.match(/async function saveModels\(\)\{[\s\S]*?\n\}\nlet settingsBaseline/)?.[0].replace(/\nlet settingsBaseline$/,'');

test('model save refreshes credential indicators from the safe server response',async()=>{
  assert.ok(normalize);assert.ok(save);
  for(const configured of [false,true]){
    const elements={'#visionSelect':{value:'m1'},'#modelMsg':{textContent:''}};
    const context={
      models:[{id:'m1',base_url:'https://other.example.invalid/v1',model:'temporary',api_key:'',keySet:true}],
      modelsRendered:false, URLSearchParams,
      $:selector=>elements[selector],setTimeout:()=>0,renderModels:()=>{},
      fetch:async()=>({ok:true,json:async()=>({ok:true,models:[{id:'m1',base_url:'https://other.example.invalid/v1',model:'temporary',api_key_set:configured}]})}),
    };
    vm.runInNewContext(normalize+'\n'+save+'\nglobalThis.save=saveModels;',context);
    await context.save();
    assert.equal(context.models[0].keySet,configured);
    assert.equal(context.models[0].api_key,'');
    assert.equal(context.modelsRendered,true);
    assert.equal(elements['#visionSelect'].value,'m1');
  }
});
