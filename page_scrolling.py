"""Bounded scrolling of public activity/venue lists; no clicks or field access."""

SCROLL_LIST = r"""reset => {
  const visible = n => {
    const box = n.getBoundingClientRect(), style = getComputedStyle(n);
    return box.width >= 200 && box.height >= 80 && style.visibility !== 'hidden' &&
      box.bottom > 0 && box.top < innerHeight && box.right > 0 && box.left < innerWidth &&
      !n.closest('nav,header,footer,aside,dialog,[role="dialog"],[aria-modal="true"],[hidden],[aria-hidden="true"]');
  };
  const documentScroller = document.scrollingElement;
  const panels = [...document.querySelectorAll('main,[role="main"],section,div')]
    .filter(n => n !== documentScroller && visible(n) &&
      /^(auto|scroll|overlay)$/.test(getComputedStyle(n).overflowY) &&
      n.scrollHeight > n.clientHeight + 4)
    .sort((a, b) => (b.clientHeight * b.clientWidth) - (a.clientHeight * a.clientWidth)).slice(0, 4);
  const nodes = [...new Set([documentScroller, ...panels].filter(Boolean))];
  let moved = 0, distance = 0;
  for (const node of nodes) {
    const before = node.scrollTop;
    node.scrollTo({top: reset ? 0 : before + Math.max(200, node.clientHeight * .75), behavior: 'instant'});
    const delta = Math.abs(node.scrollTop - before);
    if (delta > 1) { moved++; distance += delta; }
  }
  return {moved, distance: Math.round(distance), containers: panels.length,
    at_bottom: nodes.every(n => n.scrollTop + n.clientHeight >= n.scrollHeight - 4)};
}"""

TARGET_POSITION = r"""node => {
  const box = node.getBoundingClientRect();
  return {in_view: box.width > 0 && box.height > 0 && box.bottom > 0 &&
    box.top < innerHeight && box.right > 0 && box.left < innerWidth};
}"""
