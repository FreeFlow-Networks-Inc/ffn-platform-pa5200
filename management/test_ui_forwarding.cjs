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
async function check(writable, operation='port') {
  let render, calls=[];
  const good=data=>({available:true,data});
  const snapshot={can_write:writable,collected_at:0, resources:{
    dataplane:good({state:'ready',arch:'mips64',boot_id:'fixture',packet_io:{available:true,internal_link_ready:true,physical_packet_transport_verified:false}}),
    'port-events':good({stale:!writable,error:null,ports:[{port:5,admin_enabled:true,link:true,carrier:true,speed_mbps:10000}]}),
    chassis:good({power_supplies:[{name:'<script>alert(1)</script>',state:'good'}],leds:{ps0:'green'}}),
    thermal:good({errors:[]}), fabric:good({running:true}),
    network:good({config:{revision:9,ports:{p1:{mode:'l2',vlans:[100],pvid:100}},vrfs:{}}}),
    overlay:{available:false,error:'Offline'},
    lacp:good({config:{revision:0,groups:{}},capabilities:{activation_supported:false,reason:'Unqualified relay'},runtime:{}}),
    inspection:good({config:{revision:3,mode:'off',ports:[],literal:''}})}};
  const request=async(path,options)=> { calls.push({path,options});
    if(path.endsWith('/vifs'))return {forwarding:true,config:{vifs:{fv1:{enabled:true,network:{mode:'l3',vrf:'vrf-test'}},fv2:{enabled:false,network:{mode:'l3'}}}}};
    return options ? {revision:10} : snapshot; };
  const context={window:{ffnExtensions:{request,registerPage:(id,label,fn)=>{ assert.equal(id,'pa5200');if(label===(['dataplane','routing'].includes(operation)?operation:'interfaces')) render=fn; }}},
    document:{createElement:tag=>new Node(tag)},Date,JSON,Object,setTimeout:()=>{}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/ui.js','utf8'),context);
  assert.equal(calls.length,0,'loading the asset must not probe');
  const parent=new Node('main'); await render(parent);
  assert.equal(calls.length,operation==='routing'?2:1);
  if(operation==='routing') {
    assert.ok(parent.all().some(n=>n.textContent==='Configured L3 VIFs: fv1 (vrf-test). Transport: active'));
    const editor=parent.all().find(n=>n['aria-label']==='network configuration');
    editor.value=JSON.stringify({revision:9,routes:[{dst:'0.0.0.0/0',dev:'fv1',via:'198.18.0.2',table:1001}]});
    parent.all().find(n=>n.textContent==='Apply network').onclick();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(JSON.parse(calls.find(c=>c.options).options.body).routes[0].dev,'fv1');
    return;
  }
  if(operation==='dataplane') {
    assert.ok(parent.all().some(n=>n.textContent==='Internal DP link: ready'));
    assert.ok(parent.all().some(n=>n.textContent==='Physical packet forwarding: unverified'));
    return;
  }
  assert.ok(parent.all().some(n=>n.textContent==='Physical port observations'));
  assert.equal(parent.all().some(n=>n.textContent==='10000 Mbps'), writable,
    'expired observations must not show a live speed');
  const buttons=parent.all().filter(n=>n.dataset.apply);
  assert.ok(buttons.length>=3);
  assert.ok(buttons.every(b=>b.disabled===!writable));
  assert.ok(!parent.all().some(n=>n.tag==='script'));
  assert.ok(parent.all().some(n=>n.textContent==='Save LACP group'));
  assert.ok(!parent.all().some(n=>n.textContent==='Activate LACP group'));
  assert.equal(parent.all().find(n=>n['aria-label']==='Member ports (comma separated)').value, '');
  if (writable) {
    if (operation==='lacp') {
      parent.all().find(n=>n['aria-label']==='Member ports (comma separated)').value='p5,p13';
      await parent.all().find(n=>n.textContent==='Save LACP group').onclick();
      const change=calls.find(c=>c.options);
      assert.equal(change.path,'/api/system/runtime/lacp/set');
      const payload=JSON.parse(change.options.body);
      assert.equal(payload.revision,0);
      assert.deepEqual(payload.groups.lag1.members,['p5','p13']);
      assert.equal(payload.groups.lag1.activity,'active');
      assert.ok(!calls.some(c=>c.path.endsWith('/activate')));
      return;
    }
    parent.all().find(n=>n.textContent==='Apply p1').onclick();
    await new Promise(resolve=>setImmediate(resolve));
    const change=calls.find(c=>c.options);
    assert.equal(change.path,'/api/system/runtime/network/patch');
    assert.deepEqual(JSON.parse(change.options.body),{revision:9,ports:{p1:{mode:'l2',mtu:1500,vlans:[100],pvid:100}}});
    assert.ok(buttons.every(b=>b.disabled),'stale editors disabled after apply');
  }
}
(async()=>{await check(true);await check(false);await check(true,'lacp');await check(false,'dataplane');await check(true,'routing');console.log('PA-5220 UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
