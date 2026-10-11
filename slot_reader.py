"""Read booking groups without relying on a particular heading element."""

READ_SLOTS = r"""() => {
  const visible = n => !!(n.getClientRects().length &&
    getComputedStyle(n).visibility !== 'hidden' &&
    !n.closest('[hidden],[aria-hidden="true"]'));
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const kind = s => {
    const value = clean(s);
    if (/^instant(?:\s+bookings?)?(?:\s*\(\d+\))?$/i.test(value)) return 'instant';
    if (/^ballot(?:ing)?(?:\s+bookings?)?(?:\s*\(\d+\))?$/i.test(value)) return 'ballot';
    return '';
  };
  const inputs = [...document.querySelectorAll('input[type="checkbox"]')];
  const candidates = [...document.body.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],div,p,span,strong,b,legend')]
    .filter(n => visible(n) && !n.closest('nav,header,footer,a,button,label') &&
      !n.querySelector('input') && kind(n.textContent));
  // Keep the innermost label; wrappers are not additional section markers.
  const markers = candidates.filter(n => !candidates.some(other => other !== n && n.contains(other)));
  const preceding = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const branch = (scope, node) => {
    while (node.parentElement && node.parentElement !== scope) node = node.parentElement;
    return node;
  };
  const group = input => {
    for (let scope = input.parentElement; scope && scope !== document.body; scope = scope.parentElement) {
      const labelled = clean((scope.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || '').join(' '));
      if (kind(labelled)) return kind(labelled);
      const local = markers.filter(n => scope.contains(n));
      if (!local.length) continue;
      const types = new Set(local.map(n => kind(n.textContent)));
      if (types.size === 1) {
        const own = branch(scope, input);
        // A caption inside a different section with its own controls cannot
        // label this unmarked section, even when it is the page's only caption.
        const applicable = local.filter(n => {
          const captionBranch = branch(scope, n);
          return captionBranch === own || !captionBranch.querySelector('input[type="checkbox"]');
        });
        return applicable.some(n => preceding(n, input)) ? [...types][0] : '';
      }
      // Groups can have sibling captions and grids instead of sections.
      // Resolve only a preceding sibling caption. Never inherit the first
      // heading of a wrapper that contains both instant and ballot controls.
      let sibling = branch(scope, input).previousElementSibling;
      while (sibling) {
        const labels = local.filter(n => branch(scope, n) === sibling);
        if (labels.length && !sibling.querySelector('input[type="checkbox"]')) {
          const kinds = new Set(labels.map(n => kind(n.textContent)));
          return kinds.size === 1 ? [...kinds][0] : '';
        }
        if (sibling.matches('input[type="checkbox"]') || sibling.querySelector('input[type="checkbox"]')) return '';
        sibling = sibling.previousElementSibling;
      }
      return '';
    }
    return '';
  };
  const controls = inputs.map(n => {
    const label = n.closest('label') || (n.id ? document.querySelector('label[for="' + CSS.escape(n.id) + '"]') : null);
    const display = label || n;
    return {value: n.value, disabled: n.disabled || n.matches(':disabled'),
      ariaDisabled: n.getAttribute('aria-disabled') || display.getAttribute('aria-disabled'),
      visible: visible(display), bookingType: group(n)};
  });
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],legend')]
    .filter(visible).map(n => clean(n.textContent).slice(0, 80));
  const captions = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"],div,p,span,strong,b,legend')]
    .filter(n => visible(n) && !n.closest('nav,header,footer,a,button,label') &&
      !n.querySelector('input,script') && n.textContent.length <= 160 && /instant|ballot/i.test(n.textContent))
    .map(n => clean(n.textContent).slice(0, 80));
  const loading = [...document.querySelectorAll('div,p,span,[role="status"]')]
    .some(n => visible(n) && !n.querySelector('input,script') && /^loading[\s.…]*$/i.test(clean(n.textContent)));
  return {controls, loading, labels: [...new Set([...markers.map(n => clean(n.textContent)), ...captions])].slice(0, 12),
    headings: [...new Set(headings)].slice(0, 12)};
}"""
