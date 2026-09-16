const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.isConnected=true;}
  appendChild(n){this.children.push(n);return n;} replaceChildren(){this.children=[];}
  setAttribute(k,v){this[k]=v;} all(){return [this,...this.children.flatMap(n=>n.all())];}
  set value(v){this._value=String(v);} get value(){return this._value||'';}
}
async function test(role){
  let render;const calls=[];
  const request=async(path,options)=>{calls.push({path,options});
    if(path==='/api/system/planes')return {ok:true,result:{revision:9,dp:{boot_id:'current',fabric_available:true},state:{enabled:false,pending:null},hardware:{enabled:0},qualified:false,
      qualification:{report:{discover_sent:3,counters:{wan_rx:148}}}}};
    return path==='/api/auth/me'?{role}:
    {config:{revision:7,vifs:{}},ports:[5,13],running:false,forwarding:false,
      copper_ports:{2:{phy:17,bcm_port:28,speed_mbps:1000,reason:'packet path not commissioned'}}};};
  const context={crypto:{randomUUID:()=> 'cc3107f8-eb91-4c58-ae04-b25f8fa4a988'},document:{createElement:t=>new Node(t)},window:{ffnExtensions:{request,registerPage:(id,page,fn)=>{assert.equal(page,'vifs');render=fn;}}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/vif-ui.js','utf8'),context);
  assert.equal(calls.length,0);const root=new Node('main');await render(root);
  const all=root.all(),field=name=>all.find(n=>n['aria-label']===name);
  assert(all.some(n=>n.textContent==='packet path not commissioned'));
  assert(all.some(n=>n.textContent==='17 / 28'));
  assert(all.some(n=>n.textContent==='1000 Mbps'));
  assert(!field('Front port').children.some(n=>n.value==='2'),'uncommissioned copper must not be selectable');
  const save=all.find(n=>n.textContent==='Save assignment');assert.equal(save.disabled,role!=='admin');
  assert.equal(field('VIF name').value,'');assert.equal(field('Front port').value,'');
  if(role==='admin'){
    field('VIF name').value='fv9';field('Front port').value='13';field('Wire VLAN (blank for untagged)').value='200';
    await save.onclick();const change=calls.find(c=>c.options);
    assert.equal(change.path,'/api/system/runtime/vifs/set');
    assert.deepEqual(JSON.parse(change.options.body),{revision:7,vifs:{fv9:{port:13,vlan:200,enabled:true,network:{mode:'l3',mtu:1500,addresses:[]}}}});
    assert.equal(calls.filter(c=>c.options).length,1,'save must not start forwarding');
    const nodes=root.all(),read=nodes.find(n=>n.textContent==='Read WAN status'),probe=nodes.find(n=>n.textContent==='Test DHCP packet path');
    assert.equal(probe.disabled,true);
    await read.onclick();assert.equal(probe.disabled,false);
    assert(nodes.some(n=>String(n.textContent).includes('WAN frames received: 148')));
    await probe.onclick();
    const changes=calls.filter(c=>c.path==='/api/system/planes').map(c=>JSON.parse(c.options.body));
    assert.deepEqual(changes.map(c=>c.action),['status','apply','status']);
    assert.deepEqual(changes[1].payload,{revision:9,operation:'probe',expected_boot_id:'current'});
    assert.equal(changes[1].resource,'wan-path');
  }else{
    assert(!all.some(n=>n.textContent==='Test DHCP packet path'));
  }
}
(async()=>{await test('admin');await test('viewer');console.log('VIF UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
