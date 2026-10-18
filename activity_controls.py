"""Public facility-list controls, including accordion headings without links."""

from dataclasses import dataclass
import json
import re
import unicodedata


LIST_SELECTOR = 'a[href],button,[role="button"],summary,[aria-expanded][aria-controls]'

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
  const isControl = n.matches('button,[role="button"],summary') || n.hasAttribute('aria-expanded');
  return {
    href: n.href || '', control: isControl,
    id: n.id || '', controls: n.getAttribute('aria-controls') || '',
    role: n.getAttribute('role') || (n.tagName === 'A' ? 'link' : 'button'),
    summary: n.tagName === 'SUMMARY',
    rendered: !!([n, ...n.children].some(c => {
      const box = c.getBoundingClientRect(); return box.width > 0 && box.height > 0;
    }) && getComputedStyle(n).visibility !== 'hidden' &&
      !n.closest('nav,header,footer,aside,dialog,[role="dialog"],[aria-modal="true"],[hidden],[aria-hidden="true"]') &&
      !n.closest('details:not([open]) > :not(summary)')),
    labels: [n.querySelector('h1,h2,h3,h4,[role="heading"]')?.textContent || '',
             n.getAttribute('aria-label') || '', ...titles, (n.textContent || '').slice(0, 1000)]
      .map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean)
  };
})'''


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

    @classmethod
    def from_item(cls, item, name):
        label = next(label for label in item['labels'] if normalized(label) == normalized(name))
        return cls(label, item['id'], item['controls'], item['role'], item['summary'])

    def locator(self, page):
        if self.element_id:
            return page.locator('[id=' + json.dumps(self.element_id, ensure_ascii=False) + ']:visible')
        if self.controls_id:
            return page.locator(LIST_SELECTOR).and_(page.locator(
                '[aria-controls=' + json.dumps(self.controls_id, ensure_ascii=False) + ']:visible'))
        pattern = r'^\s*' + r'\s+'.join(re.escape(word) for word in self.name.split()) + r'\s*$'
        if self.summary:
            return page.locator('summary:visible').filter(has_text=re.compile(pattern, re.I))
        return page.get_by_role(self.role, name=re.compile(pattern, re.I)).and_(page.locator(':visible'))
