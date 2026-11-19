import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {cleanUrl,encodeCapture,readSelection} from "./capture.mjs";

const fixture = {title:" 合成页面 ",sourceUrl:"https://example.test/article?extra=hidden#section",capturedAt:"2026-11-19T01:02:03.456Z",text:"合成摘录\n<script>not executed</script>"};

test("exact v1 payload; strip query/fragment, preserve excerpt as data",()=>{
  assert.deepEqual(JSON.parse(encodeCapture(fixture)),{schema:"nas-ai-web-capture/v1",title:"合成页面",source_url:"https://example.test/article",captured_at:fixture.capturedAt,text:fixture.text,selection_only:true});
});

test("reject non-web URLs, ambiguous authority, and embedded credentials",()=>{
  for (const url of ["javascript:alert(1)","file:///a","https://alice:secret@example.test","https://@example.test","https://example.test\\@other.test","https://exam\nple.test"]) {
    assert.throws(()=>cleanUrl(url));
  }
});

test("bound fields and serialized UTF-8 body without truncating capture",()=>{
  for (const changes of [{title:"😀".repeat(301)},{text:"a".repeat(100001)},{text:"中".repeat(100000)},{text:"before\u0000after"},{sourceUrl:"https://example.test/"+"a".repeat(2048)},{capturedAt:"invalid"},{text:"  "}]) {
    assert.throws(()=>encodeCapture({...fixture,...changes}));
  }
  assert.equal(JSON.parse(encodeCapture({...fixture,text:"a".repeat(100000)})).text.length,100000);
});

test("MV3 declares only activeTab and scripting; no hosts/background",()=>{
  const manifest=JSON.parse(readFileSync(new URL("./manifest.json",import.meta.url),"utf8"));
  assert.equal(manifest.manifest_version,3);
  assert.deepEqual(manifest.permissions,["activeTab","scripting"]);
  assert.ok(!manifest.host_permissions&&!manifest.content_scripts&&!manifest.background);
});

test("isolated reader rejects form intersection and strips URL fields before returning",()=>{
  const ancestor = {nodeType:1,getRootNode:()=>globalThis.document,closest:()=>null,querySelectorAll:()=>[]};
  const selection = {rangeCount:1,isCollapsed:false,getRangeAt:()=>({commonAncestorContainer:ancestor,intersectsNode:()=>true}),toString:()=>"selected"};
  globalThis.document={activeElement:null,body:{contains:()=>true},title:"Fixture"};
  globalThis.window={getSelection:()=>selection};
  globalThis.location={href:"https://example.test/a?token=synthetic#hash"};
  try {
    assert.equal(readSelection().sourceUrl,"https://example.test/a");
    ancestor.querySelectorAll=()=>[{}];
    assert.equal(readSelection(),null);
    ancestor.querySelectorAll=()=>[];
    document.activeElement={closest:()=>({})};
    assert.equal(readSelection(),null);
    document.activeElement=null;
    selection.isCollapsed=true;
    assert.equal(readSelection(),null);
  } finally {
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.location;
  }
});
