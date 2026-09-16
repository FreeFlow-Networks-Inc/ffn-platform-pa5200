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
    const message = element('p', 'Loading appliance stateâ€¦', root);
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
      if (dp.packet_io) {
        const io = dp.packet_io;
        element('p', 'Internal DP link: ' + (!io.available ? 'unknown' : io.internal_link_ready ? 'ready' : 'not ready'), readiness);
        element('p', 'Physical packet forwarding: ' + (io.physical_packet_transport_verified === true ? 'verified' : 'unverified'), readiness);
      }
      element('p', 'State: ' + dp.state + ' Â· Architecture: ' + dp.arch, readiness);
      element('p', 'Boot ID: ' + dp.boot_id, readiness);
      if (dp.boot) {
        element('p', 'Debian boot: ' + (dp.boot.ready ? 'complete' : 'incomplete'), readiness);
        for (const reason of dp.boot.reasons || []) element('p', reason, readiness);
      }
      element('p', 'Available packet detectors: ' + (dp.engines?.available || []).join(', '), readiness);
      element('p', 'Execution: OCTEON CPU. Readiness does not imply hardware acceleration or active inspection.', readiness);
    }
    }
    showHandshake(snapshot.resources.dataplane);
    const control = card(root, 'Control plane and forwarding telemetry');
    async function showControl() {
      if (!writable) {
        element('p', 'Administrator access is required for control telemetry.', control);
        return;
      }
      let status;
      try { status = await api('/api/system/control'); }
      catch (e) {
        if (!root.isConnected) return;
        control.replaceChildren();
        element('h3', 'Control plane and forwarding telemetry', control);
        element('p', 'Control telemetry unavailable: ' + e.message, control);
        return;
      }
      if (!root.isConnected) return;
      control.replaceChildren();
      element('h3', 'Control plane and forwarding telemetry', control);
      element('p', 'Configuration owner: ' + status.owner, control);
      const agents = Object.entries(status.agents || {});
      if (!agents.length) element('p', 'No platform agent channels are configured.', control);
      for (const [name, state] of agents) {
        const report = state.last_observation?.report || {};
        const box = card(control, name + ' (' + state.role.toUpperCase() + ')');
        element('p', state.fresh ? (state.ready ? 'Agent ready' : 'Agent connected; subsystem not ready') :
          'Agent unavailable or stale; last observation is historical', box);
        if (state.age_seconds !== null) element('p', 'Last observation: ' + Math.floor(state.age_seconds) + ' seconds ago', box);
        if (!state.fresh) continue;
        if (report.bcm) element('p', 'BCM owner: ' + (report.bcm.available ? 'responding' : 'unavailable'), box);
        if (report.policy?.available) element('p', 'FE100 policy revision: ' + report.policy.configured_revision +
          '. Configured phase: ' + report.policy.configured_phase + '. Journaled sessions: ' + report.policy.journaled_sessions, box);
        if (report.fe100) {
          element('p', 'FE100 statistics: ' + (report.fe100.available ? 'available' : 'unavailable'), box);
          if (report.fe100.available) {
            const summary = report.fe100.summary || {};
            element('p', 'Nonempty lookup queues: ' + (summary.nonempty_queues || []).length +
              '. Fault or pause flags: ' + (summary.mode_faults_or_pauses || []).length, box);
            element('p', 'Hardware flow offload: ' + (report.fe100.offload_verified === true ? 'verified' : 'unverified'), box);
            const detail = element('details', undefined, box);
            element('summary', 'FE100 counters and queue details', detail);
            json(detail, {summary, counters: report.fe100.counters});
          }
        }
        if (report.vifs) {
          element('p', 'VIF forwarding: ' + (!report.vifs.available ? 'unavailable' : report.vifs.forwarding ? 'active' : 'inactive'), box);
          element('p', 'Applied VIF revision: ' + (report.vifs.revision ?? 'unavailable'), box);
          if (report.vifs.available) json(box, report.vifs.counters || {});
        }
      }
    }
    await showControl();
    async function pollHandshake() {
      if (!root.isConnected) return;
      let agent;
      try { agent = {available:true, data:await api(prefix + '/dataplane')}; }
      catch (e) { agent = {available:false, error:e.message}; }
      if (!root.isConnected) return;
      showHandshake(agent);
      if (writable) await showControl();
      setTimeout(pollHandshake, 5000);
    }
    setTimeout(pollHandshake, 5000);
    }
    message.textContent = 'Observed ' + new Date(snapshot.collected_at * 1000).toLocaleString() +
      (writable ? '. Changes apply immediately and persist separately from candidate/commit.' : '. Read-only access.');
    if (section === 'dataplane') element('p', 'The VIF packet transport is commissioned on ports 5 and 13 with MTU 1500. Check Virtual Interfaces for active assignments and Faceplate Ports for link state. Hardware flow offload is not active.', root);
    const notice = element('p', '', root);
    let busy = false;
    async function apply(path, payload, target) {
      if (busy) return;
      busy = true; target.disabled = true; refresh.disabled = true;
      notice.textContent = 'Applyingâ€¦';
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
    if (section === 'interfaces') {
      const box = card(root, 'Physical port observations');
      const observed = resources['port-events'];
      if (!observed?.available) {
        element('p', 'Physical port observations unavailable. Refresh to retry.', box);
      } else {
        const data = observed.data;
        const invalid = data.stale !== false || !!data.error;
        element('p', invalid ? 'Observations expired or unavailable; physical state is unknown.' :
          'Snapshot at refresh. Link up does not confirm packet forwarding.', box);
        const table = element('table', undefined, box); table.className = 'data-table';
        const head = element('tr', undefined, element('thead', undefined, table));
        for (const name of ['Port', 'Admin', 'Link', 'Effective carrier', 'Speed']) element('th', name, head);
        const body = element('tbody', undefined, table);
        const state = (value, yes, no) => invalid || typeof value !== 'boolean' ? 'Unknown' : value ? yes : no;
        for (const port of data.ports || []) {
          const row = element('tr', undefined, body);
          for (const value of ['p' + port.port, state(port.admin_enabled, 'Enabled', 'Disabled'),
              state(port.link, 'Up', 'Down'), state(port.carrier, 'Up', 'Down'),
              !invalid && port.carrier === true && port.speed_mbps > 0 ? port.speed_mbps + ' Mbps' : 'Unknown'])
            element('td', value, row);
        }
      }
    }
    if (section === 'chassis') {
    const health = card(root, 'Power supplies, cooling and fabric');
    for (const name of ['chassis', 'thermal', 'fabric']) {
      const r = resources[name];
      const detail = element('details', undefined, health);
      element('summary', name + (r.available ? '' : ' â€” unavailable'), detail);
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
      if (section === 'routing') {
        element('p', 'Routes and policy rules can use enabled L3 VIF names as dev, nexthop dev or iif. The route table must match the egress virtual router. Remove dependent routes and rules before changing an assignment.', box);
        try {
          const vifs = await api(prefix + '/vifs');
          if (!root.isConnected) return;
          const names = Object.entries(vifs.config.vifs).filter(([,b]) => b.enabled && b.network.mode === 'l3').map(([n,b]) => n + ' (' + (b.network.vrf || 'main') + ')');
          element('p', 'Configured L3 VIFs: ' + (names.join(', ') || 'none') + '. Transport: ' + (vifs.forwarding ? 'active' : 'inactive'), box);
        } catch (e) { element('p', 'VIF routing state unavailable: ' + e.message, box); }
      }
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
    if (section === 'interfaces') {
    const lacp = resources.lacp;
    const lagBox = card(root, 'LACP link aggregation');
    if (!lacp || !lacp.available) {
      element('p', lacp ? lacp.error : 'LACP controller is not installed', lagBox);
    } else {
      const data = lacp.data;
      element('p', 'Save groups without assigning live ports. Activation is a separate action. Members must connect to the same LACP peer and be disabled in port controls first.', lagBox);
      if (!data.capabilities.activation_supported) element('p', data.capabilities.reason, lagBox);
      const name = select(lagBox, 'LACP group', Array.from({length:12}, (_,i) => 'lag' + (i+1)), 'lag1');
      const members = field(lagBox, 'Member ports (comma separated)', '');
      const activity = select(lagBox, 'LACP activity', ['active','passive'], 'active');
      const rate = select(lagBox, 'LACP rate', ['fast','slow'], 'fast');
      const hash = select(lagBox, 'Transmit hash', ['layer2','layer2+3'], 'layer2+3');
      const minimum = field(lagBox, 'Minimum active links', '1');
      const priority = field(lagBox, 'System priority', '32768');
      const network = element('textarea', undefined, lagBox);
      network.setAttribute('aria-label', 'LACP group L2 or L3 settings');
      network.value = JSON.stringify({mode:'l2', vlans:[100], pvid:100}, null, 2);
      function loadGroup() {
        const g = data.config.groups[name.value];
        members.value = g ? g.members.join(',') : '';
        activity.value = g ? g.activity : 'active'; rate.value = g ? g.rate : 'fast';
        hash.value = g ? g.hash : 'layer2+3'; minimum.value = g ? g.min_links : '1';
        priority.value = g ? g.system_priority : '32768';
        network.value = JSON.stringify(g ? g.network : {mode:'l2', vlans:[100], pvid:100}, null, 2);
      }
      name.onchange = loadGroup; loadGroup();
      for (const input of [name,members,activity,rate,hash,minimum,priority,network]) input.disabled = !writable;
      applyButton(lagBox, 'Save LACP group', '/lacp/set', () => ({revision:data.config.revision,
        groups:{...data.config.groups, [name.value]:{members:members.value.split(',').map(x=>x.trim()).filter(Boolean),
          activity:activity.value, rate:rate.value, hash:hash.value, min_links:Number(minimum.value),
          system_priority:Number(priority.value), network:JSON.parse(network.value)}}}));
      applyButton(lagBox, 'Delete LACP group', '/lacp/set', () => ({revision:data.config.revision,
        groups:Object.fromEntries(Object.entries(data.config.groups).filter(([n])=>n!==name.value))}));
      if (data.capabilities.activation_supported)
        applyButton(lagBox, 'Activate LACP group', '/lacp/activate', () => ({revision:data.config.revision, group:name.value}));
      applyButton(lagBox, 'Deactivate LACP group', '/lacp/deactivate', () => ({revision:data.config.revision, group:name.value}));
      const live = element('details', undefined, lagBox);
      element('summary', 'LACP partner and member diagnostics', live); json(live, data.runtime);
    }
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
  window.ffnExtensions.interfaceLinkCapabilities=async name=>{
    const data=await api(prefix+'/faceplate');
    const port=data.ports.find(p=>p.name===name);
    if(!port)return null;
    return {source:'MP faceplate controller',configurable:!!port.speed_configuration,current_speed_mbps:port.speed_mbps,
      configured_speed:port.configured_speed,supported_speeds:(port.supported_speeds||[]).map(speed=>({speed_mbps:speed,duplex:'full'}))};
  };
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
    if(writable && window.ffnCopperIdentify)await window.ffnCopperIdentify.render(root,()=>faceplate(parent));
    message.textContent=data.saved?.pending ? 'Previous change has an uncertain outcome. Review hardware state before resolving it.' :
      'Changes apply immediately through the MP daemon and persist across boot. Speed changes can interrupt the link. Copper Auto advertises all supported speeds; optical Auto retains its advertisement. Copper link reflects the external PHY; switch link and forwarding are reported separately.';
    const table=element('table',undefined,root);table.className='data-table';
    const headings=element('tr',undefined,element('thead',undefined,table));
    for(const h of ['Port','Admin','Link','Negotiated speed','Configured speed','Action']) element('th',h,headings);
    const body=element('tbody',undefined,table);
    for(const port of data.ports) {
      const row=element('tr',undefined,body);
      for(const value of [port.name,port.available?(port.enabled===null?'Unknown':port.enabled?'Enabled':'Disabled'):'Unavailable',
                         port.link===null?'Unknown':port.media==='copper'?(port.link?'Up':'Down')+' (switch '+(port.mac_link?'up':'down')+')':port.link?'Up':'Down',port.speed_mbps?port.speed_mbps+' Mbps':'Unknown'])
        element('td',value,row);
      const speedCell=element('td',undefined,row);
      const speedSelect=element('select',undefined,speedCell);
      for(const value of ['auto',...(port.supported_speeds||[]).map(String)]){
        const option=element('option',value==='auto'?'Auto':value+' Mbps',speedSelect);option.value=value;
      }
      if(port.configured_speed && !Array.from(speedSelect.options).some(o=>o.value===port.configured_speed)){
        const option=element('option',port.configured_speed+' Mbps (observed)',speedSelect);option.value=port.configured_speed;
      }
      speedSelect.value=port.configured_speed||'auto';speedSelect.disabled=!writable||!port.speed_configuration||port.phy_pending;
      speedSelect.setAttribute('aria-label',port.name+' link speed');
      button(speedCell,'Apply speed',async()=>{
        root.querySelectorAll('button,select').forEach(node=>{node.disabled=true;});
        message.textContent='Applying and verifying '+port.name+' link speed...';
        try{
          const result=await api(prefix+'/faceplate/set',{method:'POST',body:JSON.stringify({revision:data.revision,port:port.port,speed:speedSelect.value})});
          if(result.activation!=='verified')throw new Error('Link setting was not verified');
          if(root.isConnected)await faceplate(parent);
        }catch(e){if(root.isConnected){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;}}
      },!writable||!port.speed_configuration||port.phy_pending);
      if(!port.speed_configuration)element('small',port.speed_error||'Speed control unavailable for this port',speedCell);
      if(port.media==='copper' && data.copper_sync){
        const sync=data.copper_sync.ports?.[port.name];
        const state=sync?.state||data.copper_sync.state;
        element('small','Switch rate: '+(state==='synchronized'?'matches copper PHY':state||'not observed'),speedCell);
      }
      const action=element('td',undefined,row);
      if(port.media==='copper') {
        const n=port.negotiation;
        if(n)element('small',!n.valid?'Negotiation status unavailable':
          (n.complete_1000||n.complete_10000?'Negotiation complete':'Negotiating / no completed partner exchange'),action);
        button(action,'Renegotiate',async()=>{
          if(!confirm('Restart copper negotiation on '+port.name+'? This interrupts this port.'))return;
          root.querySelectorAll('button,select').forEach(node=>{node.disabled=true;});
          message.textContent='Restarting '+port.name+' negotiation...';
          try {
            const result=await api(prefix+'/faceplate/set',{method:'POST',body:JSON.stringify({revision:data.revision,port:port.port,restart_autoneg:true})});
            if(result.activation!=='verified')throw new Error('Negotiation control was not verified');
            if(root.isConnected)await faceplate(parent);
          }catch(e){if(root.isConnected){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;}}
        },!writable||!port.renegotiate_configuration||!port.enabled||port.phy_pending);
        if(port.pair_map_recovery)button(action,'Recover copper wiring',async()=>{
          if(!confirm('Apply the PA-5220 cable-pair mapping to '+port.name+' and restart negotiation?'))return;
          root.querySelectorAll('button,select').forEach(node=>{node.disabled=true;});
          message.textContent='Recovering '+port.name+' copper wiring...';
          try {
            const result=await api(prefix+'/faceplate/set',{method:'POST',body:JSON.stringify({revision:data.revision,port:port.port,restore_pair_map:true})});
            if(result.activation!=='verified')throw new Error('Pair mapping command was not verified');
            if(root.isConnected)await faceplate(parent);
          }catch(e){if(root.isConnected){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;}}
        },!writable||!port.enabled||port.phy_pending);
      }
      const b=button(action,port.enabled?'Disable':'Enable',async()=>{
        root.querySelectorAll('button').forEach(node=>{node.disabled=true;});
        refresh.disabled=true;message.textContent='Applying and verifying '+port.name+'…';
        try {
          const result=await api(prefix+'/faceplate/set',{method:'POST',body:JSON.stringify({revision:data.revision,port:port.port,enabled:!port.enabled})});
          if(result.activation!=='verified') throw new Error('Hardware change was not verified');
          if(root.isConnected) await faceplate(parent);
        } catch(e) { if(root.isConnected){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;} }
      },!writable||!port.available||port.admin_configuration===false||port.phy_pending);
    }
  }
  async function phyPage(parent){
    parent.replaceChildren();const root=element('div',undefined,parent);element('h2','Copper PHYs',root);
    const message=element('p','Reading PHYs...',root);let data,user;
    try{[data,user]=await Promise.all([api(prefix+'/phy'),api('/api/auth/me')]);}catch(e){message.textContent=e.message;return;}
    if(!root.isConnected)return;message.textContent=data.warning;
    const writable=['admin','superuser'].includes(user.role)&&!data.saved?.pending;
    if(data.saved?.pending)element('p','Previous PHY change unresolved; inspect hardware before retrying.',root);
    const table=element('table',undefined,root);table.className='data-table';
    const headings=element('tr',undefined,element('thead',undefined,table));
    for(const title of ['PHY address / port','Identity','Firmware','Link','Negotiated speed','Advertise'])element('th',title,headings);
    const body=element('tbody',undefined,table);
    for(const phy of data.phys){
      const row=element('tr',undefined,body);
      for(const value of [String(phy.phy)+(phy.interface?' / '+phy.interface:' / mapping unverified'),phy.identified?'BCM84848':'Unknown',phy.ready?'Running 0x'+phy.firmware.toString(16):'Not ready',phy.link?'Up':'Down',phy.speed_mbps?phy.speed_mbps+' Mbps':'Unknown'])element('td',value,row);
      const cell=element('td',undefined,row),select=element('select',undefined,cell);
      for(const speed of ['auto',...(phy.supported_speeds||[]).map(String)]){const o=element('option',speed==='auto'?'Auto (100M/1G/10G)':speed+' Mbps only',select);o.value=speed;}
      select.value=phy.configured_speed||'auto';select.disabled=!writable||!phy.ready;
      select.setAttribute('aria-label','PHY '+phy.phy+' advertised speed');
      button(cell,'Apply PHY setting',async()=>{
        if(!confirm('Change PHY '+phy.phy+' advertisement? This can interrupt '+(phy.interface||'an unmapped copper port')+'.'))return;
        root.querySelectorAll('button,select').forEach(n=>{n.disabled=true;});
        try{const result=await api(prefix+'/phy/set',{method:'POST',body:JSON.stringify({revision:data.revision,phy:phy.phy,speed:select.value})});
          if(result.activation!=='verified')throw new Error('PHY configuration unverified');
          if(root.isConnected)await phyPage(parent);
        }catch(e){message.textContent=e.message+' Refresh before another change.';refresh.disabled=false;}
      },!writable||!phy.ready);
    }
    const refresh=button(root,'Refresh',()=>phyPage(parent));
  }
  window.ffnExtensions.registerPage('pa5200','phy',phyPage);
  async function bcmService(parent){
    parent.replaceChildren();const root=element('div',undefined,parent);
    element('h2','BCM Switch Service',root);
    const message=element('p','Loading service health...',root);
    let data,user;
    try{[data,user]=await Promise.all([api(prefix+'/bcm'),api('/api/auth/me')]);}
    catch(e){message.textContent=e.message;return;}
    if(!root.isConnected)return;
    message.textContent=data.warning;
    element('p','Service: '+data.service.ActiveState+' / '+data.service.SubState+'; PID '+data.service.MainPID,root);
    element('p','Switch: '+(data.chip?.state||'Unavailable')+'; forwarding has not been verified',root);
    if(data.request)element('p','Last request: '+data.request.operation+' ('+(data.operation_complete?'service transition complete':'pending')+')',root);
    if(data.configuration_reapply_required)element('p','Recommit configuration and verify port/forwarding state after startup.',root);
    const label=element('label',undefined,root);const ack=element('input',undefined,label);ack.type='checkbox';
    label.appendChild(document.createTextNode(' I acknowledge that this operation can interrupt all faceplate links.'));
    const writable=['admin','superuser'].includes(user.role)&&data.operation_complete;
    const buttons=[];
    for(const operation of ['start','stop','restart'])buttons.push(button(root,operation,async()=>{
      root.querySelectorAll('button,input').forEach(node=>{node.disabled=true;});
      try{
        const result=await api(prefix+'/bcm/set',{method:'POST',body:JSON.stringify({revision:data.revision,operation,acknowledge_link_outage:ack.checked})});
        message.textContent=result.message||'Operation submitted; refresh service health.';
      }catch(e){message.textContent=e.message;}
      refresh.disabled=false;
    },true));
    ack.onchange=()=>buttons.forEach(b=>{b.disabled=!writable||!ack.checked;});
    const refresh=button(root,'Refresh',()=>bcmService(parent));
  }
  window.ffnExtensions.registerPage('pa5200','bcm',bcmService);
  window.ffnExtensions.registerPage('pa5200', 'nif', faceplate);
})();

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
    if(writable){
      const wan=el('details',undefined,root);el('summary','WAN1 packet-path test',wan);
      el('p','Sends up to three DHCP Discover packets on copper port 1 while transport is stopped. Does not acquire a lease. The test redirect is removed afterward.',wan);
      const message=el('p','Read WAN status to check test availability.',wan);
      let observed;
      async function request(action,payload){
        const id=crypto.randomUUID();
        try{
          const response=await api('/api/system/planes',{method:'POST',body:JSON.stringify({v:1,id,resource:'wan-path',action,payload})});
          if(!response.ok)throw new Error(response.error||'Operation failed');
          return response.result;
        }catch(e){throw new Error(e.message+' · Request '+id);}
      }
      const read=el('button','Read WAN status',wan),probe=el('button','Test DHCP packet path',wan),recover=el('button','Recover WAN test',wan);
      for(const b of [read,probe,recover]){b.className='btn btn-sm';controls.push(b);}
      probe.disabled=true;recover.disabled=true;
      async function operate(operation){
        if(busy)return;busy=true;const disabled=controls.map(b=>b.disabled);controls.forEach(b=>b.disabled=true);
        message.textContent=operation==='recover'?'Recovering WAN test…':operation?'Testing WAN path…':'Reading WAN status…';
        try{
          observed=operation?await request('apply',{operation,revision:observed.revision,expected_boot_id:observed.dp.boot_id}):await request('status',{});
          if(operation)observed=await request('status',{});
          const report=(observed.qualification||{}).report;
          message.textContent=(observed.qualified?'DHCP Offer verified':report&&report.dhcp_offer_verified?'Historical DHCP Offer; retest required':report?'No valid DHCP Offer received':'WAN path not tested')+
            ' · Redirect '+(observed.hardware.enabled?'enabled':'disabled')+' · Internet forwarding not qualified'+
            (report?' · Discover sent: '+report.discover_sent+' · WAN frames received: '+((report.counters||{}).wan_rx||0):'');
        }catch(e){observed=null;message.textContent=e.message+' Read status before retrying.';}
        finally{
          busy=false;
          controls.forEach((b,i)=>b.disabled=disabled[i]);
          refresh.disabled=false;read.disabled=false;
          probe.disabled=!observed||!observed.dp.fabric_available||observed.state.enabled||!!observed.state.pending;
          recover.disabled=!observed||!observed.dp.fabric_available||!observed.state.pending;
        }
      }
      read.onclick=()=>operate();probe.onclick=()=>operate('probe');recover.onclick=()=>operate('recover');
    }
    const details=el('details',undefined,root);el('summary','Runtime counters and configuration',details);
    const pre=el('pre',JSON.stringify(state,null,2),details);pre.style.whiteSpace='pre-wrap';
  }
  window.ffnExtensions.registerPage('pa5200','vifs',render);
})();
