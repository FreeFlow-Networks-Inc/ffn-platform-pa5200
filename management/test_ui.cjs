const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.style={}; this.isConnected=true; this.textContent=''; }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren() { this.children=[]; }
  setAttribute(k,v) { this[k]=v; }
  set value(v) { this._value=String(v); }
  get value() { return this._value || ''; }
  all() { return [this,...this.children.flatMap(n=>n.all())]; }
  querySelectorAll() { return this.all().filter(n=>n.dataset.apply); }
}
async function check(writable) {
  let render, calls=[];
  const good=data=>({available:true,data});
  const snapshot={can_write:writable,collected_at:0, resources:{
    chassis:good({power_supplies:[{name:'<script>alert(1)</script>',state:'good'}],leds:{ps0:'green'}}),
    thermal:good({errors:[]}), fabric:good({running:true}),
    network:good({config:{revision:9,ports:{p1:{mode:'l2',vlans:[100],pvid:100}},vrfs:{}}}),
    overlay:{available:false,error:'Offline'},
    inspection:good({config:{revision:3,mode:'off',ports:[],literal:''}})}};
  const request=async(path,options)=> { calls.push({path,options}); return options ? {revision:10} : snapshot; };
  const context={window:{ffnExtensions:{request,register:(id,label,fn)=>{ assert.equal(id,'pa5200');render=fn; }}},
    document:{createElement:tag=>new Node(tag)},Date,JSON,Object};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/ui.js','utf8'),context);
  assert.equal(calls.length,0,'loading the asset must not probe');
  const parent=new Node('main'); await render(parent);
  assert.equal(calls.length,1);
  const buttons=parent.all().filter(n=>n.dataset.apply);
  assert.ok(buttons.length>=4);
  assert.ok(buttons.every(b=>b.disabled===!writable));
  assert.ok(parent.all().some(n=>n.textContent.includes('<script>')),'hardware strings remain text');
  assert.ok(!parent.all().some(n=>n.tag==='script'));
  if (writable) {
    parent.all().find(n=>n.textContent==='Apply p1').onclick();
    await new Promise(resolve=>setImmediate(resolve));
    const change=calls.find(c=>c.options);
    assert.equal(change.path,'/api/pa5200/network/patch');
    assert.deepEqual(JSON.parse(change.options.body),{revision:9,ports:{p1:{mode:'l2',mtu:1500,vlans:[100],pvid:100}}});
    assert.ok(buttons.every(b=>b.disabled),'stale editors disabled after apply');
  }
}
(async()=>{await check(true);await check(false);console.log('PA-5220 UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
