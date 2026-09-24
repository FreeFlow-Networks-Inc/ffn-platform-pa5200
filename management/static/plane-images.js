/* Independent MP-owned image pulls for the selected PA-5200 submodule. */
(() => {
  'use strict';
  const extension = window.ffnExtensions;
  extension.renderPlaneImageCards = async parent => {
    const root = document.createElement('section');
    parent.replaceChildren(root);
    function node(tag, text, into = root) {
      const value = document.createElement(tag);
      if (text !== undefined) value.textContent = text;
      into.appendChild(value); return value;
    }
    node('h3', 'PA-5200 processor images');
    node('p', 'Check and download Control Plane and Data Plane images independently. Downloads preserve the running images and do not restart either processor.');
    const notice = node('p', 'Loading processor image state…'); notice.setAttribute('role', 'status');
    const panels = node('div'); panels.className = 'grid grid-2';
    const cards = {};
    let state, stopped = false, pending = false;
    async function request(action, payload) {
      const result = await extension.request('/api/system/planes', {method:'POST', body:JSON.stringify({
        v:1, id:crypto.randomUUID(), resource:'plane-images', action, payload
      })});
      if (!result || !result.ok) throw Error(result?.error || 'Image controller unavailable');
      return result.result;
    }
    for (const [role, title] of [['cp','Control Plane'], ['dp','Data Plane']]) {
      const card = node('section', undefined, panels); card.className = 'card';
      node('h4', title, card);
      const available = node('p', 'Available: not checked', card);
      const staged = node('p', 'Downloaded: none', card);
      const qualification = node('p', '', card);
      const digest = node('code', '', card); digest.style.overflowWrap = 'anywhere';
      const job = node('p', '', card); job.setAttribute('role','status');
      const check = node('button', 'Check ' + role.toUpperCase(), card);
      const download = node('button', 'Download ' + role.toUpperCase(), card);
      check.className = download.className = 'btn btn-sm';
      check.disabled = download.disabled = true;
      async function run(operation) {
        check.disabled = download.disabled = true;
        try {
          const selected = state.roles[role];
          const result = await request('apply', {role, operation, revision:selected.revision,
            sha256:operation === 'download' ? selected.available.sha256 : ''});
          job.textContent = result.status + ' · ' + result.id;
          await refresh();
        } catch (error) {
          await refresh();
          notice.textContent = error.message;
        }
      }
      check.onclick = () => run('check'); download.onclick = () => run('download');
      cards[role] = {available, staged, qualification, digest, job, check, download};
    }
    async function refresh() {
      if (!root.isConnected) { stopped = true; return; }
      if (pending) return;
      pending = true;
      try {
        state = await request('status', {});
        if (!root.isConnected) return;
        notice.textContent = state.configuration_error || (state.public_key_present ? 'Signed image catalog · ' + state.server : 'Provision the update server public key before downloading images.');
        for (const role of ['cp','dp']) {
          const item = state.roles[role], view = cards[role], active = item.job || {};
          const busy = ['queued','running'].includes(active.status);
          view.available.textContent = 'Available: ' + (item.available?.version || 'No published image checked');
          view.staged.textContent = 'Downloaded: ' + (item.staged?.version || 'None');
          view.qualification.textContent = item.available ? (item.available.hardware_boot_verified ? 'Hardware boot qualification recorded' : 'Build candidate — hardware boot qualification pending') : '';
          view.digest.textContent = item.available?.sha256 || '';
          view.job.textContent = (active.status || 'Idle') + (active.message ? ' · ' + active.message : '');
          view.check.disabled = busy || !state.public_key_present || !state.server;
          view.download.disabled = busy || !state.public_key_present || !state.server || !item.available || item.staged?.sha256 === item.available.sha256;
        }
      } catch (error) {
        notice.textContent = error.message;
        for (const view of Object.values(cards)) view.check.disabled = view.download.disabled = true;
      } finally { pending = false; }
    }
    await refresh();
    const timer = setInterval(async () => { await refresh(); if (stopped) clearInterval(timer); }, 3000);
  };
})();
