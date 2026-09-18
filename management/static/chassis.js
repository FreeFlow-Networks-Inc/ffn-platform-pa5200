/* Rear-facing PA-5220 layout; animations describe telemetry, not decoration. */
export function render(parent, api, initial) {
  if(!document.querySelector('link[data-rear-style]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/static/extensions/pa5200/chassis.css';css.dataset.rearStyle='true';document.head.append(css);}
  const el=(tag,text,p)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(p)p.append(n);return n;};
  const root=el('section',undefined,parent);root.className='card rear-view';
  el('h3','PA-5220 · Rear chassis',root);
  const notice=el('p','Reading chassis telemetry…',root);notice.setAttribute('role','status');
  const scroll=el('div',undefined,root);scroll.className='rear-scroll';
  const panel=el('div',undefined,scroll);panel.className='rear-panel';panel.setAttribute('aria-label','Rear of PA-5220');
  const left=el('div',undefined,panel);left.className='rear-left';
  const drives=el('div',undefined,left);drives.className='rear-drives';
  const bayNodes=new Map(), fans=new Map(), psus=new Map();
  for(const name of ['SYS 1','SYS 2','LOG 1','LOG 2']){
    const box=el('div',undefined,drives);box.className='rear-drive';box.dataset.bay=name;
    el('span','▰ ▰ ▰',box);el('i',undefined,box).className='rear-led';
    el('span',name,box).className='rear-drive-label'+(name.startsWith('LOG')?' log':'');
    const text=el('small','Bay mapping unknown',box);bayNodes.set(name,{box,text});
  }
  function power(p,name,label){const box=el('div',undefined,p);box.className='rear-psu';box.dataset.psu=name;
    el('div',label,box).className='rear-psu-title';const row=el('div',undefined,box);row.className='rear-power';
    const socket=el('div',undefined,row);socket.className='rear-socket';for(let i=0;i<3;i++)el('i',undefined,socket);
    el('i',undefined,row).className='rear-led';const text=el('div','Unknown',box);text.className='rear-status';psus.set(name,{box,text});}
  power(left,'left','PWR 1');
  for(let bank=1;bank<=2;bank++){
    const tray=el('div',undefined,panel);tray.className='rear-tray';el('div','FAN TRAY '+bank,tray).className='rear-tray-title';
    const grid=el('div',undefined,tray);grid.className='rear-fans';
    for(let fan=1;fan<=4;fan++){
      const box=el('div',undefined,grid);box.className='rear-fan';box.dataset.fan=bank+'-'+fan;
      box.innerHTML='<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="48" fill="#17232d" stroke="#b6c3ce" stroke-width="2"/><circle cx="50" cy="50" r="43" fill="#293947"/><g class="blades" fill="#a4b8c8">'+[0,90,180,270].map(a=>`<path transform="rotate(${a} 50 50)" d="M48 48C19 43 18 16 40 11C56 10 38 32 51 44Z"/>`).join('')+'</g><circle cx="50" cy="50" r="10" fill="#182633" stroke="#7e95a8"/><circle cx="50" cy="50" r="36" fill="none" stroke="#506475" opacity=".5"/></svg>';
      const text=el('small','Fan '+fan+' · Unknown',box);fans.set(bank+'-'+fan,{box,text});
    }
    el('div',undefined,tray).className='rear-handle';
  }
  const right=el('div',undefined,panel);right.className='rear-right';const ground=el('div',undefined,right);ground.className='rear-ground';el('span','⊕',ground);el('span','⊕',ground);ground.title='Ground studs';power(right,'right','PWR 2');
  const legend=el('div',undefined,root);legend.className='rear-legend';el('span','Fan rotation = reported RPM',legend);el('span','PSU glow = power good',legend);el('span','Drive blink = observed I/O',legend);
  const storage=el('div',undefined,root);storage.className='rear-detail';
  const mapping=el('p','Drive bay positions require a verified hardware mapping. Detected disks are listed below until that mapping is available.',root);mapping.className='text-dim';
  let lastDisks=new Map(), lastGood=0;
  function draw(resources){
    const thermal=resources.thermal?.available?resources.thermal.data:null;
    const chassis=resources.chassis?.available?resources.chassis.data:null;
    for(const [key,node] of fans){const sample=thermal?.fans?.find(f=>f.bank+'-'+f.fan===key),rpm=sample?.rpm;
      const known=Number.isFinite(rpm)&&rpm>=0;node.box.classList.toggle('running',known&&rpm>0);
      node.box.style.setProperty('--fan-period',(known?Math.max(.35,12000/Math.max(rpm,1)) : 1)+'s');
      node.text.textContent='Fan '+key.split('-')[1]+' · '+(known?rpm.toLocaleString()+' RPM':'Unknown');
      node.box.title='Sensor bank '+key.split('-')[0]+' / channel '+key.split('-')[1]+(known?' · PWM '+sample.pwm+'/255':'');
    }
    for(const [key,node] of psus){const ps=chassis?.power_supplies?.find(p=>p.name===key);
      const state=ps?.present===false?'absent':ps?.power_good===true?'good':ps?.present===true&&ps?.power_good===false?'fault':'unknown';
      node.box.className='rear-psu '+state;node.text.textContent={good:'Power good',fault:'Power fault',absent:'Not present',unknown:'Unknown'}[state];
    }
    const diskState=resources['chassis-storage']?.available?resources['chassis-storage'].data:null;
    const current=new Map((diskState?.disks||[]).map(d=>[d.name,d]));
    function active(d){const prev=lastDisks.get(d?.name);return !!d&&(d.io_in_progress>0||!!prev&&(d.read_bytes>prev.read_bytes||d.write_bytes>prev.write_bytes));}
    for(const [name,node] of bayNodes){const bay=diskState?.bays?.find(b=>b.name===name),d=bay?.disk;
      const state=d?(d.state==='running'?'good':'fault'):bay?.state==='absent'?'absent':'unknown';
      node.box.className='rear-drive '+state+(active(d)?' active':'');node.text.textContent=d?d.name+' · '+(active(d)?'I/O active':d.state):!diskState||bay?.state==='unavailable'?'Telemetry unavailable':bay?.state==='absent'?'No drive detected':'Bay mapping unknown';
    }
    storage.replaceChildren();
    el('strong','Detected storage',storage);
    if(!diskState)el('p','Storage telemetry unavailable',storage);
    for(const d of current.values()){const line=el('p',d.name+' · '+d.model+' · '+(active(d)?'I/O active':d.state),storage);line.title=(d.paths||[]).join(', ');}
    mapping.hidden=!!diskState&&diskState.bays.every(b=>b.mapped);lastDisks=current;
    notice.textContent=thermal&&chassis?'Live chassis observation · '+new Date().toLocaleTimeString():'Some chassis observations are unavailable; unknown components are not animated.';
    if(thermal?.errors?.length)notice.textContent+=' · '+thermal.errors.join('; ');
  }
  draw(initial||{});lastGood=Date.now();
  // Stop motion if a request hangs rather than leaving stale fans spinning.
  const stale=setInterval(()=>{if(!root.isConnected){clearInterval(stale);return;}if(Date.now()-lastGood>15000)draw({});},1000);
  async function poll(){
    if(!root.isConnected)return;
    const names=['thermal','chassis','chassis-storage'];
    const values=await Promise.allSettled(names.map(async name=>({data:await api('/api/system/runtime/'+name),received:Date.now()})));
    if(!root.isConnected)return;
    draw(Object.fromEntries(names.map((name,i)=>[name,values[i].status==='fulfilled'&&Date.now()-values[i].value.received<15000?{available:true,data:values[i].value.data}:{available:false}])));
    lastGood=Date.now();setTimeout(poll,5000);
  }
  poll();
  return root;
}
