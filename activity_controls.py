"""Public facility-list controls, including accordion headings without links."""

from dataclasses import dataclass
import json
import re
import unicodedata


# Actual ActiveSG disclosure row supplied from the public site's DOM. Match
# Chakra's component classes and direct children, never generated css-* hashes.
CHAKRA_ROW = ('.chakra-card > .chakra-card__body > .chakra-stack:has(> p):has(> svg)'
              ':not(a[href] *,button *,[role="button"] *,summary *)')
LIST_SELECTOR = 'a[href],button,[role="button"],summary,[aria-expanded][aria-controls],' + CHAKRA_ROW

READ_TARGETS = r'''nodes => nodes.map(n => {
  const walker = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
  let first;
  while ((first = walker.nextNode())) {
    if (first.textContent.trim() && !first.parentElement.closest(
        'script,style,svg,[hidden],[aria-hidden="true"]')) break;
  }
  const titles = first ? [first.textContent] : [];
  let parent = first?.parentElement;
  for (let i = 0; parent && parent !== n && i < 6; i++, parent = parent.parentElement) {
    if (parent.textContent.length <= 1000) titles.push(parent.textContent);
  }
  const chakraCard = n.matches(__CHAKRA_ROW__);
  let collapseVisible = true;
  for (let ancestor = n.parentElement; ancestor; ancestor = ancestor.parentElement) {
    if (!ancestor.classList.contains('chakra-collapse')) continue;
    const style = getComputedStyle(ancestor), box = ancestor.getBoundingClientRect();
    if (style.display === 'none' || Number(style.opacity) === 0 || box.height <= 1) {
      collapseVisible = false; break;
    }
  }
  const isControl = chakraCard || n.matches('button,[role="button"],summary') || n.hasAttribute('aria-expanded');
  return {
    href: n.href || '', raw: n.getAttribute('href'), control: isControl, chakra_card: chakraCard,
    id: n.id || '', controls: n.getAttribute('aria-controls') || '',
    role: n.getAttribute('role') || (n.tagName === 'A' ? 'link' : 'button'),
    summary: n.tagName === 'SUMMARY',
    rendered: !!([n, ...n.children].some(c => {
      const box = c.getBoundingClientRect(); return box.width > 0 && box.height > 0;
    }) && getComputedStyle(n).visibility !== 'hidden' &&
      !n.closest('nav,header,footer,aside,dialog,[role="dialog"],[aria-modal="true"],[hidden],[aria-hidden="true"]') &&
      !n.closest('details:not([open]) > :not(summary)') && collapseVisible),
    labels: [n.querySelector('h1,h2,h3,h4,[role="heading"]')?.textContent || '',
             n.getAttribute('aria-label') || '', ...titles, (n.textContent || '').slice(0, 1000)]
      .map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean)
  };
})'''.replace('__CHAKRA_ROW__', json.dumps(CHAKRA_ROW))


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip().casefold()


def matches(item, name):
    return any(normalized(label) == normalized(name) for label in item['labels'])


@dataclass(frozen=True)
class ActivityButton:
    name: str
    element_id: str = ''
    controls_id: str = ''
    role: str = 'button'
    summary: bool = False
    chakra_card: bool = False

    @classmethod
    def from_item(cls, item, name):
        label = next(label for label in item['labels'] if normalized(label) == normalized(name))
        return cls(label, item['id'], item['controls'], item['role'], item['summary'], item.get('chakra_card', False))

    def locator(self, page):
        if self.element_id:
            return page.locator('[id=' + json.dumps(self.element_id, ensure_ascii=False) + ']:visible')
        if self.controls_id:
            return page.locator(LIST_SELECTOR).and_(page.locator(
                '[aria-controls=' + json.dumps(self.controls_id, ensure_ascii=False) + ']:visible'))
        pattern = r'^\s*' + r'\s+'.join(re.escape(word) for word in self.name.split()) + r'\s*$'
        if self.chakra_card:
            title = page.locator('p').filter(has_text=re.compile(pattern, re.I))
            return page.locator(CHAKRA_ROW).filter(has=title).and_(page.locator(':visible'))
        if self.summary:
            return page.locator('summary:visible').filter(has_text=re.compile(pattern, re.I))
        return page.get_by_role(self.role, name=re.compile(pattern, re.I)).and_(page.locator(':visible'))


TARGET_EVIDENCE = r'''({name, selector}) => {
  const normalize = text => text.normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
  const target = normalize(name), found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let text;
  while ((text = walker.nextNode()) && found.length < 3) {
    if (normalize(text.textContent) !== target || text.parentElement.closest('script,style,svg,textarea')) continue;
    const chain = [];
    let node = text.parentElement;
    for (let depth = 0; node && depth < 4; depth++, node = node.parentElement) {
      const box = node.getBoundingClientRect();
      chain.push({tag:node.tagName.toLowerCase(), classes:[...node.classList].slice(0, 4).map(c => c.slice(0, 60)),
        role:(node.getAttribute('role') || '').slice(0, 30), selected:node.matches(selector),
        has_box:box.width > 0 && box.height > 0});
    }
    found.push(chain);
  }
  return found;
}'''
