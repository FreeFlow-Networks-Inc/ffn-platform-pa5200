const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Node {
  constructor(tag) { this.tag=tag; this.children=[]; this.dataset={}; this.style={}; this.isConnected=true; this.textContent=''; }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren() { this.children=[]; }
  setAttribute(k,v) { this[k]=v; }
  set value(v) { this._value=String(v); }
  get options() {return this.children.filter(n=>n.tag==='option');}
  get value() { return this._value || ''; }
  all() { return [this,...this.children.flatMap(n=>n.all())]; }
  querySelectorAll() { return this.all().filter(n=>n.dataset.apply); }
}
async function check(writable) {
  let pages={}, calls=[];
  const good=data=>({available:true,data});
  const snapshot={can_write:writable,collected_at:0, resources:{
    dataplane:good({state:'ready',arch:'mips64',boot_id:'test-boot',engines:{available:['literal','credit_card']}}),
    chassis:good({power_supplies:[{name:'<script>alert(1)</script>',state:'good'}],leds:{ps0:'green'}}),
    thermal:good({errors:[]}), fabric:good({running:true}),
    network:good({config:{revision:9,ports:{p1:{mode:'l2',vlans:[100],pvid:100}},vrfs:{}}}),
    overlay:{available:false,error:'Offline'},
    inspection:good({config:{revision:3,mode:'off',ports:[],literal:''}})}};
  const request=async(path,options)=> {
    calls.push({path,options});
    if(path==='/api/auth/me') return {role:writable?'admin':'viewer'};
    if(path.endsWith('/bcm'))return {revision:1,operation_complete:true,service:{ActiveState:'active',SubState:'running',MainPID:'12'},chip:{state:'ready'},warning:'Interrupts links'};
    if(path.endsWith('/phy'))return {revision:1,saved:{},phys:[{phy:17,interface:'ethernet1/2',identified:true,ready:true,firmware:0x1089,link:true,speed_mbps:1000,supported_speeds:[100,1000,10000],configured_speed:'auto'}],warning:'PHY settings only'};
    if(path.endsWith('/faceplate')) return {revision:10,saved:{ports:{}},ports:[{port:1,name:'ethernet1/1',available:true,enabled:true,link:false,speed_mbps:10000}]};
    return options ? {revision:10} : snapshot;
  };
  const context={window:{ffnExtensions:{request,registerPage:(id,label,fn)=>{ assert.equal(id,'pa5200');pages[label]=fn; }}},
    document:{createElement:tag=>new Node(tag),createTextNode:text=>{const n=new Node('text');n.textContent=text;return n;}},Date,JSON,Object,setTimeout(){}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/ui.js','utf8'),context);
  assert.equal(calls.length,0,'loading the asset must not probe');
  const parent=new Node('main'); await pages.interfaces(parent);
  assert.equal(calls.length,1);
  const buttons=parent.all().filter(n=>n.dataset.apply);
  assert.equal(buttons.length,2);
  assert.ok(buttons.every(b=>b.disabled===!writable));
  assert.ok(!parent.all().some(n=>n.textContent==='OCTEON dataplane handshake'));
  assert.ok(!parent.all().some(n=>n.tag==='script'));
  if (writable) {
    parent.all().find(n=>n.textContent==='Apply p1').onclick();
    await new Promise(resolve=>setImmediate(resolve));
    const change=calls.find(c=>c.options);
    assert.equal(change.path,'/api/system/runtime/network/patch');
    assert.deepEqual(JSON.parse(change.options.body),{revision:9,ports:{p1:{mode:'l2',mtu:1500,vlans:[100],pvid:100}}});
    assert.ok(buttons.every(b=>b.disabled),'stale editors disabled after apply');
  }
  await pages.dataplane(parent);
  assert.ok(parent.all().some(n=>n.textContent.includes('State: ready')));
  assert.ok(!parent.all().some(n=>n.dataset.apply));
  await pages.chassis(parent);
  assert.ok(parent.all().some(n=>n.textContent.includes('<script>')),'hardware strings remain text');
  await pages.routing(parent);
  assert.ok(parent.all().some(n=>n.textContent==='Route lookup'));
  assert.ok(!parent.all().some(n=>n.textContent==='Apply p1'));
  await pages.inspection(parent);
  assert.ok(parent.all().some(n=>n.textContent==='Apply inspection policy'));
  assert.ok(!parent.all().some(n=>n.textContent==='Route lookup'));
  await pages.nif(parent);
  assert.ok(parent.all().some(n=>n.textContent==='Faceplate Ports'));
  const disable=parent.all().find(n=>n.textContent==='Disable');
  assert.equal(disable.disabled,!writable);
  assert.ok(parent.all().some(n=>n.textContent==='ethernet1/1'));
  assert.equal(parent.all().find(n=>n.textContent==='Apply speed').disabled,true,'Unsupported speed is disabled');
  await pages.bcm(parent);
  const restart=parent.all().find(n=>n.textContent==='restart');
  assert.equal(restart.disabled,true);
  const ack=parent.all().find(n=>n.type==='checkbox');ack.checked=true;ack.onchange();
  assert.equal(restart.disabled,!writable);
  await pages.phy(parent);
  assert.equal(parent.all().find(n=>n.textContent==='Apply PHY setting').disabled,!writable);
  assert.ok(parent.all().some(n=>n.textContent==='17 / ethernet1/2'));
}
(async()=>{await check(true);await check(false);console.log('PA-5220 UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
