import argparse
import asyncio
import os
from pathlib import Path
import sys


async def check(visit=False):
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            page = await browser.new_page()
            await page.set_content('<h1>Browser ready</h1>')
            assert await page.locator('h1').inner_text() == 'Browser ready'
            print('Chromium startup: OK')
            if visit:
                await page.goto('https://activesg.gov.sg/facility-bookings/activities', wait_until='domcontentloaded', timeout=45000)
                await page.wait_for_timeout(2000)
                print('ActiveSG title:', await page.title())
                text = await page.locator('body').inner_text()
                if 'security verification' in text.lower() or 'Just a moment' in await page.title():
                    print('ActiveSG verification required; availability has NOT been read.')
                    await browser.close()
                    return 2
            await browser.close()
            return 0
    except Exception as error:
        print('Chromium startup failed. Check browser installation and native system libraries.')
        if not os.getenv('BROWSER_CDP_URL'):
            # Startup errors contain local library names, useful for native Render diagnostics.
            print(str(error)[:2500])
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--visit', action='store_true')
    args = parser.parse_args()
    sys.exit(asyncio.run(check(visit=args.visit)))