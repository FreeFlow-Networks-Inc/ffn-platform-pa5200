const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node{
  constructor(tag){this.tag=tag;this.children=[];this.isConnected=true;}
  appendChild(n){this.children.push(n);return n;}
  setAttribute(k,v){this[k]=v;}
  set value(v){this._value=String(v);}
  get value(){return this._value??this.children[0]?.value??'';}
  all(){return [this,...this.children.flatMap(n=>n.all())];}
}
async function test(pending,candidate){
  const calls=[];let refreshed=0;
  const state={config:{revision:7,mapping:{2:{phy:17,bcm_port:28}}},pending,candidate,complete:false};
  const context={crypto:{randomUUID:()=> '5e7df964-8cc0-46a0-928e-5b4572da54f1'},document:{createElement:t=>new Node(t)},
    window:{ffnExtensions:{request:async(path,options)=>{calls.push({path,data:options?JSON.parse(options.body):null});return state;}}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/copper-identify-ui.js','utf8'),context);
  assert.equal(calls.length,0);
  const root=new Node('main');await context.window.ffnCopperIdentify.render(root,async()=>{refreshed++;});
  if(!pending){
    const select=root.all().find(n=>n.tag==='select');
    assert.deepEqual(select.children.map(n=>n.value),['1','3','4']);
    await root.all().find(n=>n.textContent==='Start identification').onclick();
    assert.deepEqual(calls[1].data,{revision:7,operation:'begin',port:1});
  }else{
    const confirm=root.all().find(n=>n.textContent==='Confirm port 1');assert.equal(confirm.disabled,!candidate);
    if(candidate){await confirm.onclick();assert.deepEqual(calls[1].data,{revision:7,operation:'confirm',token:'probe-token'});}
    else{await root.all().find(n=>n.textContent==='Cancel identification').onclick();assert.equal(calls[1].data.operation,'cancel');}
  }
  assert.equal(refreshed,1);assert.equal(calls[0].path,'/api/system/runtime/copper-identify');
  assert.equal(calls[1].path,'/api/system/runtime/copper-identify/set');
}
(async()=>{await test(null,null);await test({port:1,token:'probe-token',seconds_remaining:890},null);
await test({port:1,token:'probe-token',seconds_remaining:890},{phy:16,bcm_port:13});
console.log('Copper identification UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
