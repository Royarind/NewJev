(() => {
  // Structured snapshot of the CURRENT frame. Python merges frames and adds control actions.
  if (!document.body) return null;
  const cache = window.__jevFast ||= {ids: new WeakMap(), nodes: new Map(), next: 1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e); cache.nodes.set(id, e); return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);

  // querySelectorAll that also descends into open shadow roots
  const deepAll = (root, selector, out = []) => {
    for (const e of root.querySelectorAll(selector)) out.push(e);
    for (const host of root.querySelectorAll('*')) if (host.shadowRoot) deepAll(host.shadowRoot, selector, out);
    return out;
  };

  const safe = e => !['password', 'hidden'].includes(e.type);
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    (e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : e.offsetParent !== null);
  const byId = (e, id) => (e.getRootNode && e.getRootNode().getElementById ? e.getRootNode().getElementById(id) : null) || document.getElementById(id);
  const name = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map(id => name(byId(e, id), seen)).filter(Boolean).join(' ');
    return (referenced || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => name(l, seen)).filter(Boolean).join(' ') ||
      (['button', 'submit', 'reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n => n.nodeType === 3 ? n.textContent :
        n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? name(n, seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || e.getAttribute('data-tooltip') || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 160);
  };
  const roles = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemcheckbox',
    'menuitemradio', 'option', 'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton', 'treeitem', 'row'];
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],[contenteditable=""],' +
    roles.map(r => '[role="' + r + '"]').join(',');
  const role = e => {
    const explicit = e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (['checkbox', 'radio'].includes(e.type)) return e.type;
      if (['button', 'submit', 'reset', 'image', 'file'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text', 'email', 'url', 'tel', 'date', 'datetime-local', 'time', 'month', 'week', ''].includes(e.type)) return 'textbox';
    }
    return null;
  };

  const actions = [];
  const seenEditable = new Set();
  for (const e of deepAll(document, selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    // contenteditable children of an editable root are the same editor
    if (e.isContentEditable && e.parentElement && e.parentElement.isContentEditable) continue;
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2, rname = role(e);
    if (!rname || r.width <= 0 || r.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    if (rname === 'gridcell' && e.querySelector('button,[role="button"]')) continue;
    if (rname === 'row' && !e.hasAttribute('tabindex') && !e.onclick) continue;
    const base = {node: identity(e), role: rname, label: name(e) || rname,
      rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}};
    for (const key of ['checked', 'selected', 'expanded']) {
      const value = e.getAttribute('aria-' + key);
      if (value !== null) base[key] = value;
    }
    if (['checkbox', 'radio'].includes(e.type)) base.checked = String(e.checked);
    if (e.tagName === 'SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base, kind: 'select', value: o.value,
          current_value: [...e.selectedOptions].map(o => o.label).join(', '), label: base.label + ' → ' + o.label});
    } else {
      const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
        (['textbox', 'searchbox', 'spinbutton'].includes(rname) ||
          (rname === 'combobox' && (['INPUT', 'TEXTAREA'].includes(e.tagName) || e.isContentEditable)));
      const value = 'value' in e && e.tagName !== 'BUTTON' ? String(e.value) :
        e.isContentEditable || rname === 'combobox' ? e.innerText.trim().slice(0, 500) : '';
      if (editable) {
        if (seenEditable.has(e)) continue;
        seenEditable.add(e);
        actions.push({...base, kind: 'fill', value, multiline: e.tagName === 'TEXTAREA' || e.isContentEditable});
        actions.push({...base, kind: 'click', value, label: 'Open ' + base.label});
      } else if (e.type === 'file') {
        actions.push({...base, kind: 'click', value, label: base.label || 'Choose file / Upload', is_file: true});
      } else {
        actions.push({...base, kind: 'click', value});
      }
    }
  }

  // Visible text (viewport only), including open shadow roots
  const words = []; let length = 0;
  const range = document.createRange();
  const walk = root => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    let node;
    while ((node = walker.nextNode()) && length < 6000) {
      if (node.nodeType === 1) { if (node.shadowRoot) walk(node.shadowRoot); continue; }
      const value = node.textContent.trim(), parent = node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r = range.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
        words.push(value); length += value.length;
      }
    }
  };
  walk(document.body);
  const text = words.join('\n').slice(0, 6000);

  // Things only a human should handle
  const lower = (document.body.innerText || '').slice(0, 20000).toLowerCase();
  const passwordField = deepAll(document, 'input[type="password"]').some(visible);
  const textInputs = deepAll(document, 'input:not([type]),input[type="text"],input[type="tel"],input[type="number"]').filter(visible);
  const otpField = deepAll(document, 'input[autocomplete="one-time-code"]').some(visible) ||
    (textInputs.length > 0 && textInputs.length <= 8 &&
     /(enter (the|your) (verification )?code|2-step verification|two-factor authentication|one-time (pass)?code|code from your authenticator)/.test(lower));
  const captcha = [...document.querySelectorAll('iframe')].some(f =>
      /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha/i.test(f.src || '')) ||
    /(i'?m not a robot|verify you are (a )?human|are you a robot|complete the captcha|unusual traffic)/.test(lower);

  const height = document.documentElement.scrollHeight;
  const omitted_actions = Math.max(0, actions.length - 250);
  actions.splice(250);
  return {url: location.href, title: document.title, w: innerWidth, h: innerHeight, text,
    scroll: {y: Math.round(scrollY), height, can_down: scrollY + innerHeight < height - 2, can_up: scrollY > 0},
    actions, omitted_actions,
    sensitive: {password: passwordField, otp: otpField, captcha}};
})()
