"""Read the public date strip without depending on generated CSS hashes."""
from page_snapshot import READ_PAGE


DATE_BUTTONS = r'''() => [...document.querySelectorAll('[aria-label^="View timeslots for"]')].map(n => ({
  label: n.getAttribute('aria-label') || '',
  selected: n.hasAttribute('data-selected') && n.getAttribute('data-selected') !== 'false'
    || n.getAttribute('aria-pressed') === 'true' || n.getAttribute('aria-selected') === 'true',
  disabled: n.matches(':disabled') || n.getAttribute('aria-disabled') === 'true',
  visible: !!(n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden'
    && !n.closest('[hidden],[aria-hidden="true"]'))
}))'''

LOADING = r'''() => [...document.querySelectorAll('div,p,span,[role="status"],[role="progressbar"],[aria-busy="true"],.chakra-spinner')]
  .some(n => n.getClientRects().length && getComputedStyle(n).visibility !== 'hidden'
    && !n.closest('[hidden],[aria-hidden="true"]') &&
    (n.matches('[role="progressbar"],[aria-busy="true"],.chakra-spinner') ||
      !n.querySelector('input,script') && /^loading[\s.…]*$/i.test((n.textContent || '').trim())))'''

READ_DATES = '() => ({page: (' + READ_PAGE + ')(), dates: (' + DATE_BUTTONS + ')(), loading: (' + LOADING + ')()})'
