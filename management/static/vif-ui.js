/* FFN PA5200 VIF UI v1: bundled into ui.js by install-vif.py. */
(() => {
  'use strict';
  const api=window.ffnExtensions.request, base='/api/system/runtime/vifs';
  const el=(tag,text,parent)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.appendChild(n);return n;};
  async function render(parent) {
    parent.replaceChildren();const root=el('section',undefined,parent);root.className='card';
    el('h2','Virtual Interfaces',root);
    const notice=el('p','Loading assignments…',root);
    const controls=[];
    function button(label,fn,disabled=false) {const b=el('button',label,root);b.className='btn btn-sm';b.disabled=disabled;b.onclick=fn;controls.push(b);return b;}
    const refresh=button('Refresh',()=>render(parent));
    let state,user;
    try {[state,user]=await Promise.all([api(base),api('/api/auth/me')]);}
    catch(e){notice.textContent=e.message;return;}
    if(!root.isConnected)return;
    const writable=['admin','superuser'].includes(user.role);
    notice.textContent='Revision '+state.config.revision+' · '+(state.running?'Running':'Stopped')+
      ' · Packet transport '+(state.forwarding?'active':'inactive')+
      (state.recovery_required?' · Recovery required':'')+(state.runtime_error?' · '+state.runtime_error:'');
    el('p','Assign each VIF to a commissioned front port and an optional wire VLAN. L2 bridge VLANs join logical interfaces; L3 addresses use the selected virtual router. Saving applies immediately. Start enables the packet transport.',root);
    el('p','Commissioned ports: '+state.ports.join(', ')+'. FE100 VIF session offload is not active.',root);
    if(state.link_observation_error)el('p','Physical observation: '+state.link_observation_error,root);
    if(state.copper_ports){
      const copper=el('table',undefined,root);copper.className='data-table';
      const ch=el('tr',undefined,copper);['Copper port','PHY / BCM','Speed','VIF readiness'].forEach(x=>el('th',x,ch));
      for(const [p,s] of Object.entries(state.copper_ports)){
        const row=el('tr',undefined,copper);
        [p,s.phy===null?'Unverified':s.phy+' / '+s.bcm_port,s.speed_mbps===null?'Unknown':s.speed_mbps+' Mbps',s.reason].forEach(x=>el('td',String(x),row));
      }
    }
    let busy=false;
    async function change(op,fields={}) {
      if(busy)return;busy=true;controls.forEach(b=>b.disabled=true);notice.textContent='Applying…';
      try {await api(base+'/'+op,{method:'POST',body:JSON.stringify({revision:state.config.revision,...fields})});if(root.isConnected)await render(parent);}
      catch(e){notice.textContent=e.message+' Refresh before another change.';refresh.disabled=false;}
    }
    button('Start transport',()=>change('start'),!writable||state.running||state.recovery_required);
    button('Stop transport',()=>change('stop'),!writable||!state.running);
    button('Recover interrupted change',()=>change('recover'),!writable||state.running||!state.recovery_required);
    const table=el('table',undefined,root);table.className='data-table';
    const head=el('tr',undefined,table);['VIF','Front port','Wire VLAN','Enabled','Mode','Carrier','Actions'].forEach(x=>el('th',x,head));
    function input(label,value,choices) {
      const wrap=el('label',label+' ',root);wrap.className='form-group';
      const n=el(choices?'select':'input',undefined,wrap);n.setAttribute('aria-label',label);
      if(choices)choices.forEach(x=>{el('option',String(x),n).value=String(x);});
      n.value=value;n.disabled=!writable;return n;
    }
    el('h3','Create or edit an assignment',root);
    const name=input('VIF name',''),port=input('Front port','',state.ports),vlan=input('Wire VLAN (blank for untagged)','');
    const enabled=input('Enabled','true',['true','false']),mode=input('Mode','l3',['l2','l3','disabled']);
    const bridge=input('L2 bridge VLAN',''),addresses=input('L3 addresses (comma separated)',''),vrf=input('Virtual router (blank for default)','');
    const mtu=input('MTU','1500');
    function load(n,b) {name.value=n;port.value=b.port;vlan.value=b.vlan===null?'':b.vlan;enabled.value=String(b.enabled);
      mode.value=b.network.mode;bridge.value=b.network.pvid||'';addresses.value=(b.network.addresses||[]).join(',');vrf.value=b.network.vrf||'';mtu.value=b.network.mtu||1500;}
    for(const [n,b] of Object.entries(state.config.vifs)) {
      const link=(state.vif_links||{})[n];
      const carrier=!state.running?'Stopped':!b.enabled||b.network.mode==='disabled'?'Disabled':link?(link.carrier===true?'Up':link.reason):'Unknown';
      const row=el('tr',undefined,table);[n,b.port,b.vlan===null?'Untagged':b.vlan,String(b.enabled),b.network.mode,carrier].forEach(x=>el('td',String(x),row));
      const cell=el('td',undefined,row);
      for(const [label,fn] of [['Edit',()=>load(n,b)],['Remove',()=>change('set',{vifs:Object.fromEntries(Object.entries(state.config.vifs).filter(([key])=>key!==n))})]]) {
        const btn=el('button',label,cell);btn.disabled=!writable||state.recovery_required;btn.onclick=fn;controls.push(btn);
      }
    }
    button('Save assignment',()=>{
      try {
        if(!/^fv[1-9][0-9]{0,3}$/.test(name.value)||!port.value)throw new Error('Enter a VIF name (fv1–fv4094) and front port');
        const network={mode:mode.value,mtu:Number(mtu.value)};
        if(mode.value==='l2'){network.vlans=[Number(bridge.value)];network.pvid=Number(bridge.value);}
        if(mode.value==='l3'){network.addresses=addresses.value.split(',').map(x=>x.trim()).filter(Boolean);if(vrf.value.trim())network.vrf=vrf.value.trim();}
        return change('set',{vifs:{...state.config.vifs,[name.value]:{port:Number(port.value),vlan:vlan.value.trim()?Number(vlan.value):null,enabled:enabled.value==='true',network}}});
      }catch(e){notice.textContent=e.message;}
    },!writable||state.recovery_required);
    const details=el('details',undefined,root);el('summary','Runtime counters and configuration',details);
    const pre=el('pre',JSON.stringify(state,null,2),details);pre.style.whiteSpace='pre-wrap';
  }
  window.ffnExtensions.registerPage('pa5200','vifs',render);
})();
