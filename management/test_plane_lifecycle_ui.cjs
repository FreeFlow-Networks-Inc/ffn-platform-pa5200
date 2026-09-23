const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
class Node {
  constructor(tag){this.tag=tag;this.children=[];this.isConnected=true;}
  appendChild(n){this.children.push(n);return n;}
  setAttribute(k,v){this[k]=v;}
  all(){return [this,...this.children.flatMap(n=>n.all())];}
}
(async()=>{
  let confirmed=false, calls=[],tick;
  const state={config:{revision:4},roles:{cp:{restart_available:false,reason:'Not commissioned'},dp:{restart_available:true,fresh:true,ready:true,boot_id:'dp-boot'}}};
  const ext={request:async(path,options)=>{
    const req=JSON.parse(options.body);calls.push(req);assert.equal(req.resource,'plane-lifecycle');
    if(req.action==='apply')return {ok:true,result:{message:'Queued'}};
    return {ok:true,result:state};
  }};
  const root=new Node('main');
  vm.runInNewContext(fs.readFileSync(__dirname+'/static/plane-lifecycle.js','utf8'),{
    window:{ffnExtensions:ext,confirm:()=>confirmed},document:{createElement:t=>new Node(t)},
    crypto:{randomUUID:()=> 'id'},setInterval:f=>{tick=f;},clearInterval:()=>{}
  });
  await ext.renderPlaneLifecycle(root);
  const buttons=root.all().filter(x=>x.tag==='button');
  assert.equal(buttons[0].disabled,true);assert.equal(buttons[1].disabled,false);
  await buttons[1].onclick();assert.equal(calls.filter(x=>x.action==='apply').length,0);
  confirmed=true;await buttons[1].onclick();
  assert.deepEqual(calls.find(x=>x.action==='apply').payload,{role:'dp',operation:'restart',revision:4,expected_boot_id:'dp-boot',acknowledge_outage:true});
  state.roles.dp.restart_available=false;await tick();assert.equal(buttons[1].disabled,true);
  console.log('Restart UI confirmation, role selection and availability passed');
})().catch(e=>{console.error(e);process.exit(1);});
