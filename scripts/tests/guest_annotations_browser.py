"""Synthetic-only browser regression: python scripts/tests/guest_annotations_browser.py.
Requires Playwright Python and Chromium. Never connects to an installed NAS.
"""
import base64
import os
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'data:image/svg+xml;base64,' + base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="navy"/></svg>').decode()
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'), args=['--no-sandbox'])
    for mobile in (False, True):
        context = browser.new_context(viewport={'width': 390 if mobile else 1200, 'height': 850}, has_touch=mobile, is_mobile=mobile)
        page = context.new_page()
        page.set_content(f'''<style>.public-asset-stage {{position:relative;height:400px;display:grid;place-items:center}} img {{max-width:100%;max-height:100%}}</style><article class="public-asset"><div class="public-asset-stage"><img src="{IMAGE}"></div><form class="public-comment-form"><input name="time_start"></form><button data-public-annotation="9">Replay</button></article>''')
        page.add_style_tag(path=str(ROOT/'app/static/styles.css'))
        page.add_script_tag(path=str(ROOT/'app/static/guest-annotations.js'))
        page.evaluate("""() => { window.card = document.querySelector('article'); window.version={id:1}; window.comments=[{id:9,version_id:1,time_start:null,drawing:[{points:[{x:.1,y:.2},{x:.5,y:.7}]}]}]; GuestAnnotations.mount(card,version,comments,true); }""")
        page.wait_for_function('document.querySelector("img").naturalWidth > 0')
        page.locator('[data-guest-draw=start]').click()
        rect = page.locator('svg').bounding_box()
        page.mouse.move(rect['x']+rect['width']*.2, rect['y']+rect['height']*.3)
        page.mouse.down(); page.mouse.move(rect['x']+rect['width']*.7,rect['y']+rect['height']*.8,steps=5); page.mouse.up()
        payload=page.evaluate('GuestAnnotations.get(card).payload()')
        assert len(payload['drawing']) == 1 and abs(payload['drawing'][0]['points'][0]['x']-.2)<.01
        assert payload['time_start'] is None
        page.locator('[data-guest-draw=undo]').click()
        assert page.evaluate('GuestAnnotations.get(card).payload().drawing.length') == 0
        page.locator('[data-guest-draw=cancel]').click()
        assert not page.evaluate('GuestAnnotations.get(card).hasDraft()')
        page.locator('[data-public-annotation]').click()
        assert page.locator('svg polyline').count() == 1
        page.evaluate('GuestAnnotations.mount(card,version,comments,false)')
        assert page.locator('[data-guest-draw=start]').count() == 0
        page.locator('[data-public-annotation]').click()
        assert page.locator('svg polyline').count() == 1
        page.evaluate('GuestAnnotations.disposeAll(document)')
        assert page.locator('.guest-annotation-layer').count() == 0
        print('PASS', 'mobile' if mobile else 'desktop', 'coordinates, undo, cancel, replay, read-only, disposal')
        context.close()
    browser.close()
