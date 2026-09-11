/* SPDX-License-Identifier: GPL-2.0-or-later */
(() => {
  'use strict';
  const api = window.ffnExtensions.request;
  const prefix = '/api/system/runtime';
  function element(tag, text, parent) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }
  function button(parent, label, action, disabled = false) {
    const b = element('button', label, parent);
    b.className = 'btn btn-sm'; b.disabled = disabled;
    b.onclick = action;
    return b;
  }
  function card(parent, title) {
    const box = element('section', undefined, parent); box.className = 'card';
    element('h3', title, box); return box;
  }
  function json(parent, value) {
    const pre = element('pre', JSON.stringify(value, null, 2), parent);
    pre.style.whiteSpace = 'pre-wrap'; pre.style.maxHeight = '320px'; pre.style.overflow = 'auto';
  }
  function field(parent, label, value) {
    const wrap = element('label', label + ' ', parent);
    wrap.className = 'form-group';
    const input = element('input', undefined, wrap);
    input.value = value; input.setAttribute('aria-label', label);
    return input;
  }
  function select(parent, label, choices, value) {
    const wrap = element('label', label + ' ', parent);
    wrap.className = 'form-group';
    const input = element('select', undefined, wrap);
    for (const choice of choices) { const option = element('option', choice, input); option.value = choice; }
    input.value = value; input.setAttribute('aria-label', label);
    return input;
  }
  async function render(parent, section = 'dataplane') {
    const titles = {dataplane:'Dataplane Status', chassis:'Chassis & Cooling',
      interfaces:'Dataplane Interfaces', routing:'Dataplane Routing',
      overlay:'Dataplane Tunnels', inspection:'Dataplane Inspection'};
    parent.replaceChildren();
    const root = element('div', undefined, parent);
    const header = element('div', undefined, root); header.className = 'page-header';
    element('h2', titles[section], header);
    const message = element('p', 'Loading appliance state…', root);
    const refresh = button(header, 'Refresh', () => render(parent, section));
    let snapshot;
    try { snapshot = await api(prefix + '/status'); }
    catch (e) { message.textContent = e.message; return; }
    if (!root.isConnected) return;
    const writable = snapshot.can_write;
    if (section === 'dataplane') {
    const readiness = card(root, 'OCTEON dataplane handshake');
    function showHandshake(agent) {
    readiness.replaceChildren();
    element('h3', 'OCTEON dataplane handshake', readiness);
    if (!agent || !agent.available) {
      element('p', 'Agent unavailable: ' + (agent?.error || 'not installed or unreachable'), readiness);
    } else {
      const dp = agent.data;
      element('p', 'State: ' + dp.state + ' · Architecture: ' + dp.arch, readiness);
      element('p', 'Boot ID: ' + dp.boot_id, readiness);
      element('p', 'Available packet detectors: ' + (dp.engines?.available || []).join(', '), readiness);
      element('p', 'Execution: OCTEON CPU. Readiness does not imply hardware acceleration or active inspection.', readiness);
    }
    }
    showHandshake(snapshot.resources.dataplane);
    async function pollHandshake() {
      if (!root.isConnected) return;
      let agent;
      try { agent = {available:true, data:await api(prefix + '/dataplane')}; }
      catch (e) { agent = {available:false, error:e.message}; }
      if (!root.isConnected) return;
      showHandshake(agent);
      setTimeout(pollHandshake, 5000);
    }
    setTimeout(pollHandshake, 5000);
    }
    message.textContent = 'Observed ' + new Date(snapshot.collected_at * 1000).toLocaleString() +
      (writable ? '. Changes apply immediately and persist separately from candidate/commit.' : '. Read-only access.');
    if (section === 'dataplane') element('p', 'Forwarding uses the commissioning relay on ports 1, 3, 5 and 13; MTU 1500. Hardware flow offload is not active.', root);
    const notice = element('p', '', root);
    let busy = false;
    async function apply(path, payload, target) {
      if (busy) return;
      busy = true; target.disabled = true; refresh.disabled = true;
      notice.textContent = 'Applying…';
      try {
        const result = await api(prefix + path, {method: 'POST', body: JSON.stringify(payload)});
        notice.textContent = result.activation === 'active' ? 'Inspection revision verified active on the OCTEON dataplane.' :
          result.activation ? 'Inspection activation: ' + result.activation + '. Refresh state before another edit.' :
          'Controller accepted the change. Refresh state to verify activation before another edit.';
        json(root, result);
        // Existing editors retain their captured revisions and stay disabled.
        root.querySelectorAll('[data-apply]').forEach(b => { b.disabled = true; });
      } catch (e) {
        notice.textContent = e.message + ' Refresh state before retrying.';
        root.querySelectorAll('[data-apply]').forEach(b => { b.disabled = true; });
      } finally { busy = false; refresh.disabled = false; }
    }
    function applyButton(box, label, path, payload) {
      const b = button(box, label, () => {
        try { apply(path, payload(), b); }
        catch (e) { notice.textContent = e.message; }
      }, !writable);
      b.dataset.apply = 'true'; return b;
    }
    const resources = snapshot.resources;
    if (section === 'chassis') {
    const health = card(root, 'Power supplies, cooling and fabric');
    for (const name of ['chassis', 'thermal', 'fabric']) {
      const r = resources[name];
      const detail = element('details', undefined, health);
      element('summary', name + (r.available ? '' : ' — unavailable'), detail);
      json(detail, r.available ? r.data : r.error);
    }
    if (resources.thermal.available) {
      applyButton(health, 'Automatic fan control', '/thermal/auto', () => ({}));
      applyButton(health, 'Full-speed fans', '/thermal/full', () => ({}));
    }
    const chassis = resources.chassis;
    if (chassis.available) {
      for (const ps of chassis.data.power_supplies || [])
        element('p', ps.name + ' supply: ' + ps.state, health);
      element('p', 'LEDs: ' + Object.entries(chassis.data.leds || {}).map(([n,v]) => n + ' ' + v).join(', '), health);
    }
    }
    const selected = {interfaces:['network'], routing:['network'], overlay:['overlay'], inspection:['inspection']}[section] || [];
    for (const resource of selected) {
      const r = resources[resource];
      const box = card(root, titles[section]);
      if (!r.available) { element('p', r.error, box); continue; }
      if (section === 'interfaces') {
        element('p', 'Use L2 VLAN membership or L3 addresses and a virtual router per port. Routes, VRFs and policy rules are edited below.', box);
        for (const [name, config] of Object.entries(r.data.config.ports)) {
          const row = element('div', undefined, box);
          element('h4', name, row);
          const mode = select(row, name + ' mode', ['disabled', 'l2', 'l3'], config.mode);
          const vlans = field(row, name + ' VLANs (comma separated)', (config.vlans || []).join(','));
          const pvid = field(row, name + ' Native VLAN', config.pvid || '');
          const addresses = field(row, name + ' Addresses (comma separated)', (config.addresses || []).join(','));
          const vrf = select(row, name + ' Virtual router', ['', ...Object.keys(r.data.config.vrfs || {})], config.vrf || '');
          function updateFields() {
            mode.disabled = !writable;
            vlans.disabled = pvid.disabled = !writable || mode.value !== 'l2';
            addresses.disabled = vrf.disabled = !writable || mode.value !== 'l3';
          }
          mode.onchange = updateFields; updateFields();
          applyButton(row, 'Apply ' + name, '/network/patch', () => ({revision: r.data.config.revision,
            ports: {[name]: {mode:mode.value, mtu:config.mtu || 1500,
              ...(mode.value === 'l2' ? {vlans:vlans.value.split(',').map(x => Number(x.trim())),
                ...(pvid.value.trim() ? {pvid:Number(pvid.value)} : {})} : {}),
              ...(mode.value === 'l3' ? {addresses:addresses.value.split(',').map(x => x.trim()).filter(Boolean),
                ...(vrf.value ? {vrf:vrf.value} : {})} : {})}}}));
        }
      }
      if (resource === 'overlay') element('p', 'VXLAN, Geneve, GRE and MACsec link settings. MACsec key provisioning is not managed here.', box);
      if (resource === 'inspection') {
        element('p', 'Literal UDP/TCP payload matching: off, alert or block. Fragments and unsupported traffic pass; no stream reassembly or TLS decryption.', box);
        const mode = select(box, 'Inspection mode', ['off', 'alert', 'block'], r.data.config.mode);
        const ports = field(box, 'Inspection ports', r.data.config.ports.join(','));
        const literal = field(box, 'Payload text to match', r.data.config.literal);
        const detectors = field(box, 'Additional detectors (credit_card, ssn, api_key)', (r.data.config.detectors || []).join(','));
        for (const input of [mode, ports, literal, detectors]) input.disabled = !writable;
        applyButton(box, 'Apply inspection policy', '/inspection/set', () => ({revision:r.data.config.revision,
          mode:mode.value, ports:ports.value.split(',').map(x=>x.trim()).filter(Boolean).map(Number), literal:literal.value,
          detectors:detectors.value.split(',').map(x=>x.trim()).filter(Boolean)}));
      }
      const details = element('details', undefined, box);
      element('summary', 'Edit ' + resource + ' configuration (JSON)', details);
      const editor = element('textarea', undefined, details);
      const keys = section === 'interfaces' ? ['revision','ports'] : section === 'routing' ? ['revision','routes','vrfs','rules'] : Object.keys(r.data.config);
      editor.value = JSON.stringify(Object.fromEntries(keys.filter(k => k in r.data.config).map(k => [k,r.data.config[k]])), null, 2); editor.rows = 16; editor.style.width = '100%';
      editor.disabled = !writable; editor.setAttribute('aria-label', resource + ' configuration');
      applyButton(details, 'Apply ' + resource, '/' + resource + (resource === 'network' ? '/patch' : '/set'), () => JSON.parse(editor.value));
      const live = element('details', undefined, box);
      element('summary', 'Runtime state and diagnostics', live); json(live, r.data);
    }
    if (section === 'routing') {
    const lookup = card(root, 'Route lookup');
    const dst = element('input', undefined, lookup); dst.placeholder = 'Destination IPv4 or IPv6'; dst.setAttribute('aria-label', 'Route destination');
    const vrf = element('input', undefined, lookup); vrf.placeholder = 'Optional VRF name'; vrf.setAttribute('aria-label', 'Route VRF');
    const output = element('div', undefined, lookup);
    button(lookup, 'Look up route', async () => {
      output.replaceChildren();
      try { json(output, await api(prefix + '/network/lookup', {method:'POST', body:JSON.stringify({dst:dst.value, ...(vrf.value ? {vrf:vrf.value} : {})})})); }
      catch(e) { output.textContent = e.message; }
    }, !resources.network.available);
    }
  }
  for (const section of ['dataplane', 'chassis', 'interfaces', 'routing', 'overlay', 'inspection'])
    window.ffnExtensions.registerPage('pa5200', section, parent => render(parent, section));
  async function faceplate(parent) {
    parent.replaceChildren();
    const root=element('div',undefined,parent);
    const header=element('div',undefined,root);header.className='page-header';
    element('h2','Faceplate Ports',header);
    const refresh=button(header,'Refresh',()=>faceplate(parent));
    const message=element('p','Reading faceplate hardware…',root);
    let data, user;
    try { [data,user]=await Promise.all([api(prefix+'/faceplate'),api('/api/auth/me')]); }
    catch(e) { message.textContent=e.message;return; }
    if (!root.isConnected) return;
    const writable=['admin','superuser'].includes(user.role) && !data.saved?.pending;
    message.textContent=data.saved?.pending ? 'Previous change has an uncertain outcome. Review hardware state before resolving it.' :
      'Administrative state controls the physical port. Link and speed are observations; link up does not confirm forwarding.';
    const table=element('table',undefined,root);table.className='data-table';
    const headings=element('tr',undefined,element('thead',undefined,table));
    for(const h of ['Port','Admin','Link','Speed','Action']) element('th',h,headings);
    const body=element('tbody',undefined,table);
    for(const port of data.ports) {
      const row=element('tr',undefined,body);
      for(const value of [port.name,port.available?(port.enabled?'Enabled':'Disabled'):'Unavailable',
                         port.link===null?'Unknown':port.link?'Up':'Down',port.speed_mbps?port.speed_mbps+' Mbps':'Unknown'])
        element('td',value,row);
      const action=element('td',undefined,row);
      const b=button(action,port.enabled?'Disable':'Enable',async()=>{
        root.querySelectorAll('button').forEach(node=>{node.disabled=true;});
        refresh.disabled=true;message.textContent='Applying and verifying '+port.name+'…';
        try {
          const result=await api(prefix+'/faceplate/set',{method:'POST',body:JSON.stringify({revision:data.revision,port:port.port,enabled:!port.enabled})});
          if(result.activation!=='verified') throw new Error('Hardware change was not verified');
          if(root.isConnected) await faceplate(parent);
        } catch(e) { if(root.isConnected){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;} }
      },!writable||!port.available);
    }
  }
  window.ffnExtensions.registerPage('pa5200', 'nif', faceplate);
})();
