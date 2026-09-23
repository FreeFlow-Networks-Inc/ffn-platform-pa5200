/* Restart the selected processor through MP controld; never activate a download. */
(() => {
  'use strict';
  const extension = window.ffnExtensions;
  extension.renderPlaneLifecycle = async parent => {
    const root = document.createElement('section'); parent.appendChild(root);
    function node(tag, text, into = root) {
      const el = document.createElement(tag); el.textContent = text; into.appendChild(el); return el;
    }
    node('h3', 'Processor restart');
    node('p', 'Restart the currently selected boot image. Downloaded images are not activated by these controls.');
    const notice = node('p', 'Loading restart availability…'); notice.setAttribute('role', 'status');
    const grid = node('div', ''); grid.className = 'grid grid-2';
    let state, pending = false, submitting = false;
    const cards = {};
    async function request(action, payload) {
      const reply = await extension.request('/api/system/planes', {method:'POST', body:JSON.stringify({
        v:1, id:crypto.randomUUID(), resource:'plane-lifecycle', action, payload
      })});
      if (!reply?.ok) throw Error(reply?.error || 'Processor restart controller unavailable');
      return reply.result;
    }
    for (const [role, title] of [['cp','Control Plane'], ['dp','Data Plane']]) {
      const card = node('section', '', grid); card.className = 'card'; node('h4', title, card);
      const status = node('p', '', card), reason = node('p', '', card);
      const button = node('button', 'Restart ' + title, card); button.className = 'btn btn-sm'; button.disabled = true;
      button.onclick = async () => {
        if (submitting || !state?.roles[role]?.restart_available) return;
        const selected = state.roles[role], revision = state.config.revision;
        const impact = role === 'cp'
          ? 'Control Plane restart interrupts switch control, cooling supervision and communication with the Data Plane. Traffic may stop.'
          : 'Data Plane restart interrupts traffic processing, NAT sessions and aggregate attachments.';
        if (!window.confirm('Restart ' + title + '?\n\n' + impact + '\n\nThis uses the selected boot image; it does not install the downloaded image.')) return;
        submitting = true;
        for (const card of Object.values(cards)) card.button.disabled = true;
        let failure;
        try {
          const job = await request('apply', {role, operation:'restart', revision,
            expected_boot_id:selected.boot_id, acknowledge_outage:true});
          notice.textContent = job.message || 'Restart queued';
        } catch (error) { failure = error.message; }
        finally { submitting = false; await refresh(); if (failure) notice.textContent = failure; }
      };
      cards[role] = {status, reason, button};
    }
    async function refresh() {
      if (!root.isConnected || pending || submitting) return;
      pending = true;
      try {
        state = await request('status', {});
        if (!root.isConnected) return;
        notice.textContent = state.job?.message || 'Each action targets one processor. Restarts are serialized because CP owns the DP control path.';
        for (const role of ['cp','dp']) {
          const item = state.roles[role], card = cards[role];
          card.status.textContent = (item.fresh ? (item.ready ? 'Agent ready' : 'Agent connected; not ready') : 'Agent observation unavailable or stale') + (item.boot_id ? ' · Boot ' + item.boot_id : '');
          card.reason.textContent = item.reason || 'Restart available';
          card.button.disabled = submitting || !item.restart_available;
        }
      } catch (error) {
        notice.textContent = error.message;
        for (const card of Object.values(cards)) card.button.disabled = true;
      } finally { pending = false; }
    }
    await refresh();
    const timer = setInterval(async () => {
      if (!root.isConnected) { clearInterval(timer); return; }
      await refresh();
    }, 3000);
  };
})();
