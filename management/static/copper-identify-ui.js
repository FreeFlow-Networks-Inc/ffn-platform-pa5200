/* FFN copper identification UI v1: MP-owned commissioning, no direct PHY writes. */
(() => {
  'use strict';
  const api=window.ffnExtensions.request;
  const el=(tag,text,parent)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.appendChild(n);return n;};
  async function call(action,payload){
    const base='/api/system/runtime/copper-identify';
    return action==='status'?api(base):api(base+'/set',{method:'POST',body:JSON.stringify(payload)});
  }
  window.ffnCopperIdentify={async render(parent,onMapped){
    const root=el('section',undefined,parent);root.className='card';
    el('h3','Identify copper ports',root);
    const message=el('p','Reading copper port mappings…',root);
    let state;
    try{state=await call('status',{});}catch(e){message.textContent=e.message;return;}
    if(!root.isConnected)return;
    const controls=[];
    const button=(text,fn,disabled=false)=>{const b=el('button',text,root);b.disabled=disabled;b.onclick=fn;controls.push(b);return b;};
    async function change(operation,fields){
      controls.forEach(n=>n.disabled=true);
      try{
        await call('apply',{revision:state.config.revision,operation,...fields});
        if(root.isConnected)await onMapped();
      }catch(e){message.textContent=e.message+' Refresh before retrying.';refresh.disabled=false;}
    }
    const refresh=button('Refresh identification',onMapped);
    if(state.complete){message.textContent='All four copper port mappings are recorded. Use the port controls below.';return;}
    const known=Object.entries(state.config.mapping).map(([p,m])=>'Port '+p+' → PHY '+m.phy+' / BCM '+m.bcm_port);
    if(known.length)el('p',known.join('; '),root);
    if(state.pending){
      message.textContent='Connect one spare active Ethernet link to copper port '+state.pending.port+'. Leave existing links in place. Then refresh identification.';
      if(state.candidate)el('p','Detected PHY '+state.candidate.phy+' / BCM '+state.candidate.bcm_port+'. Confirm only if the test cable is in port '+state.pending.port+'.',root);
      else el('p',state.waiting_reason||'Waiting for one new link.',root);
      el('p','Identification expires in '+state.pending.seconds_remaining+' seconds. This records wiring; packet forwarding requires separate verification.',root);
      button('Confirm port '+state.pending.port,()=>change('confirm',{token:state.pending.token}),!state.candidate);
      button('Cancel identification',()=>change('cancel',{token:state.pending.token}));
    }else{
      message.textContent='Select an unmapped copper port with no cable attached and start identification. Then connect a spare active Ethernet link to that port. Keep the live WAN cable in place.';
      const label=el('label','Copper port ',root),select=el('select',undefined,label);
      select.setAttribute('aria-label','Copper port to identify');controls.push(select);
      for(let p=1;p<=4;p++)if(!state.config.mapping[String(p)]){el('option','Port '+p,select).value=String(p);}
      button('Start identification',()=>change('begin',{port:Number(select.value)}));
    }
  }};
})();
