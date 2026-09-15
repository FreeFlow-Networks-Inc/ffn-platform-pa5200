const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.isConnected=true;}
  appendChild(n){this.children.push(n);return n;} replaceChildren(){this.children=[];}
  setAttribute(k,v){this[k]=v;} all(){return [this,...this.children.flatMap(n=>n.all())];}
  set value(v){this._value=String(v);} get value(){return this._value||'';}
}
async function test(role){
  let render;const calls=[];
  const request=async(path,options)=>{calls.push({path,options});return path==='/api/auth/me'?{role}:
    {config:{revision:7,vifs:{}},ports:[5,13],running:false,forwarding:false};};
  const context={document:{createElement:t=>new Node(t)},window:{ffnExtensions:{request,registerPage:(id,page,fn)=>{assert.equal(page,'vifs');render=fn;}}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/vif-ui.js','utf8'),context);
  assert.equal(calls.length,0);const root=new Node('main');await render(root);
  const all=root.all(),field=name=>all.find(n=>n['aria-label']===name);
  const save=all.find(n=>n.textContent==='Save assignment');assert.equal(save.disabled,role!=='admin');
  assert.equal(field('VIF name').value,'');assert.equal(field('Front port').value,'');
  if(role==='admin'){
    field('VIF name').value='fv9';field('Front port').value='13';field('Wire VLAN (blank for untagged)').value='200';
    await save.onclick();const change=calls.find(c=>c.options);
    assert.equal(change.path,'/api/system/runtime/vifs/set');
    assert.deepEqual(JSON.parse(change.options.body),{revision:7,vifs:{fv9:{port:13,vlan:200,enabled:true,network:{mode:'l3',mtu:1500,addresses:[]}}}});
    assert.equal(calls.filter(c=>c.options).length,1,'save must not start forwarding');
  }
}
(async()=>{await test('admin');await test('viewer');console.log('VIF UI tests passed');})().catch(e=>{console.error(e);process.exit(1);});
