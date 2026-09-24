const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag=tag; this.children=[]; this.style={}; this.isConnected=true; }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute(k,v) { this[k]=v; }
  all() { return [this,...this.children.flatMap(n=>n.all())]; }
}
(async()=>{
  let calls=[], tick, cleared=false, fail=false;
  const state={server:'https://patch.example',public_key_present:true,roles:{
    cp:{revision:3,available:{version:'cp-1',sha256:'a'.repeat(64),hardware_boot_verified:false},job:{status:'running'}},
    dp:{revision:8,available:{version:'dp-2',sha256:'b'.repeat(64),hardware_boot_verified:false}}
  }};
  const extension={request:async(path,options)=>{
    assert.equal(path,'/api/system/planes');
    const body=JSON.parse(options.body); calls.push(body);
    assert.equal(body.resource,'plane-images');
    if(body.action==='status') return {ok:true,result:state};
    if(fail) throw Error('Revision changed; retry');
    state.roles[body.payload.role].job={status:'queued'};
    return {ok:true,result:{status:'queued',id:'job'}};
  }};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/plane-images.js','utf8'),{
    window:{ffnExtensions:extension},document:{createElement:tag=>new Node(tag)},
    crypto:{randomUUID:()=> 'request'},setInterval:fn=>{tick=fn;return 1;},clearInterval:()=>{cleared=true;}
  });
  const parent=new Node('main'); await extension.renderPlaneImageCards(parent);
  const button=name=>parent.all().find(n=>n.tag==='button'&&n.textContent===name);
  assert.equal(button('Check CP').disabled,true);
  assert.equal(button('Download DP').disabled,false,'CP job must not block DP');
  await button('Download DP').onclick();
  assert.deepEqual(calls.find(c=>c.action==='apply').payload,{role:'dp',operation:'download',revision:8,sha256:'b'.repeat(64)});
  assert.equal(button('Download DP').disabled,true);
  state.roles.dp.job={status:'failed'}; fail=true; await tick();
  await button('Check DP').onclick();
  assert.ok(parent.all().some(n=>n.textContent==='Revision changed; retry'));
  state.public_key_present=false; await tick();
  assert.ok(parent.all().filter(n=>n.tag==='button').every(n=>n.disabled));
  parent.children[0].isConnected=false; await tick(); assert.equal(cleared,true);
  console.log('Independent plane image UI tests passed');
})().catch(error=>{console.error(error);process.exit(1);});
