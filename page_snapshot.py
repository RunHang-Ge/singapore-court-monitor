"""Read page classification evidence in one remote browser operation."""

READ_PAGE = r'''() => ({
  url: location.href,
  title: document.title,
  text: (document.body?.innerText || '').slice(0, 20000),
  markers: {
    challenge_options: typeof window._cf_chl_opt === 'object',
    challenge_script: [...document.querySelectorAll('script[src]')].some(
      n => n.src.includes('/cdn-cgi/challenge-platform/')),
    widget: [...document.querySelectorAll('iframe[src]')].some(
      n => {try {return new URL(n.src).hostname === 'challenges.cloudflare.com'} catch {return false}})
  }
})'''
