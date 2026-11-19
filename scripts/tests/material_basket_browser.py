"""Optional synthetic UI regression; requires Playwright + Chromium, never contacts a NAS."""
from pathlib import Path
import os
import re
import shutil
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[2]
html=(ROOT/'app/static/index.html').read_text()
modal=re.search(r'<div class="modal" role="dialog" aria-modal="true" id="materialBasketModal">.*?</div></div>\n',html).group()
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or shutil.which('chromium'),args=['--no-sandbox'])
 for width in (1280,390):
  page=browser.new_page(viewport={'width':width,'height':900})
  page.set_content('<button id="materialBasketOpen">素材篮 <span id="materialBasketCount">0</span></button><div id="results"></div>'+modal)
  page.add_script_tag(path=str(ROOT/'app/static/material-basket.js'))
  page.evaluate('''() => {
   window.user={id:1}; window.saved=[]; window.opened=0; window.handoffs=0;
   const api=async (path,opt={})=>{
    if(path==='/api/projects') return [{id:7,name:'演示项目',access_role:'editor'},{id:8,name:'只读项目',access_role:'viewer'}];
    if(path.endsWith('/handoff')) {handoffs++;return {project_id:7,added:handoffs===1?saved.length:0,skipped:handoffs===1?0:saved.length};}
    if(opt.method==='POST') for(const id of JSON.parse(opt.body).file_ids) if(!saved.some(x=>x.id===id)) saved.push({id,available:true,name:'合成素材 '+id,relative_path:'demo/'+id,kind:'image',size:42});
    if(opt.method==='DELETE') saved=path==='/api/material-basket'?[]:saved.filter(x=>x.id!==Number(path.split('/').pop()));
    return {items:saved.map(x=>({...x})),total:saved.length};
   };
   MaterialBasket.init({api,user:()=>user,escape:s=>String(s).replaceAll('<','&lt;'),bytes:String,toast:()=>{},open:el=>el.hidden=false,close:el=>el.hidden=true,confirm:async()=>true,preview:id=>opened=id,project:id=>opened=id});
   MaterialBasket.userChanged();
  }''')
  page.evaluate("document.querySelector('#results').innerHTML=MaterialBasket.button(1)")
  page.locator('[data-basket-add="1"]').click()
  page.wait_for_function("document.querySelector('#materialBasketCount').textContent==='1'")
  # Simulate navigating to a different search/directory: old result nodes are gone.
  page.evaluate("document.querySelector('#results').innerHTML=MaterialBasket.button(2)")
  page.locator('[data-basket-add="2"]').click()
  page.locator('#materialBasketOpen').click()
  page.wait_for_function("document.querySelectorAll('.basket-item').length===2")
  assert page.locator('#materialBasketProject option').count()==2
  page.locator('#materialBasketProject').select_option('7')
  page.locator('#materialBasketHandoff').click()
  page.wait_for_function("document.querySelector('#materialBasketResult').textContent.includes('已加入 2')")
  page.locator('#materialBasketHandoff').click()
  page.wait_for_function("document.querySelector('#materialBasketResult').textContent.includes('已有 2')")
  page.locator('[data-basket-remove="1"]').click()
  page.wait_for_function("document.querySelectorAll('.basket-item').length===1")
  page.evaluate('saved[0].available=false;delete saved[0].name;delete saved[0].relative_path')
  page.locator('#materialBasketRefresh').click()
  page.wait_for_function("document.querySelector('#materialBasketStatus').textContent.includes('1 个不可用')")
  assert page.locator('#materialBasketHandoff').is_disabled()
  assert '合成素材 2' not in page.locator('#materialBasketItems').inner_text()
  page.evaluate('user=null;MaterialBasket.userChanged()')
  assert page.locator('#materialBasketCount').inner_text()=='0'
  assert page.locator('#materialBasketModal').is_hidden()
  print('PASS',width,'cross-view basket, handoff, retry, remove, revoke, logout')
  page.close()
 browser.close()
