/* Local-only bounded preparation. The existing server remains the authority. */
(function (root) {
  const MAX_FILES = 10, MAX_BYTES = 256 * 1024, MAX_TOTAL = 1024 * 1024;
  function parse(text) {
    const d = JSON.parse(text);
    // JSON.parse alone silently keeps duplicate keys. Reject them before canonicalizing.
    const keys=new Set();
    for(let i=0;i<text.length;i++) {
      if(text[i] !== '"')continue;
      const start=i++;
      for(;i<text.length;i++){if(text[i]==='\\'){i++;continue;}if(text[i]==='"')break;}
      let next=i+1;while(/\s/.test(text[next]||'x'))next++;
      if(text[next]===':'){const key=JSON.parse(text.slice(start,i+1));if(keys.has(key))throw Error('重复字段');keys.add(key);}
    }
    if (!d || Object.keys(d).sort().join(',') !== 'captured_at,schema,selection_only,source_url,text,title' || d.schema !== 'nas-ai-web-capture/v1' || d.selection_only !== true) throw Error('不支持的摘录格式');
    for (const [k, max] of [['title',300],['source_url',2048],['captured_at',40],['text',100000]]) {
      if (typeof d[k] !== 'string' || !d[k].trim() || [...d[k]].length > max || [...d[k]].some(c => { const n=c.codePointAt(0); return n>=0xd800 && n<=0xdfff || n===127 || n<32 && !(k==='text' && '\t\r\n'.includes(c)); })) throw Error('字段为空、超限或含无效字符');
    }
    const u = new URL(d.source_url);
    if (!['http:','https:'].includes(u.protocol) || !u.hostname || u.username || u.password || /^[a-z]+:\/\/[^/?#]*@/i.test(d.source_url) || u.search || u.hash || /[\\\s]/u.test(d.source_url) || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|\+00:00)$/.test(d.captured_at) || !Number.isFinite(Date.parse(d.captured_at))) throw Error('来源 URL 或 UTC 时间无效（不得含凭据、查询或片段）');
    // Canonicalizing removes ambiguous JSON representations before upload.
    return Object.fromEntries(Object.keys(d).sort().map(k => [k,d[k]]));
  }
  class Batch {
    constructor(active, notify) { this.active=active; this.notify=notify; this.rows=[]; this.generation=0; this.busy=false; this.preparing=false; }
    stop() { this.generation++; this.busy=false; this.preparing=false; this.rows=[]; this.notify(this); }
    async prepare(files) {
      if (this.busy || this.preparing) return;
      const g=++this.generation; this.preparing=true;
      // Check count before iterating FileList or constructing any per-file DOM/data.
      if (files.length>MAX_FILES) {
        this.rows=[{name:`已选择 ${files.length} 份`,status:'invalid',selected:false,detail:'整批拒绝：最多 10 份；没有读取或导入任何文件。请重新选择。'}];
        this.preparing=false;this.notify(this);return;
      }
      const bounded=Array.from(files);
      this.rows=bounded.map(f => ({name:f.name,status:'loading',selected:false}));
      if (bounded.reduce((n,f)=>n+f.size,0)>MAX_TOTAL) {
        this.rows.forEach(r=>{r.status='invalid';r.detail='整批拒绝：总大小最多 1 MiB；没有读取或导入任何文件。';}); this.preparing=false; this.notify(this); return;
      }
      this.notify(this);
      const seen=new Set();
      for (let i=0;i<bounded.length;i++) {
        if (g!==this.generation || !this.active()) return;
        const f=bounded[i], r=this.rows[i];
        try {
          if (!f.name.endsWith('.nas-capture.json') || !f.size || f.size>MAX_BYTES) throw Error('需 .nas-capture.json，单份 1–262144 字节');
          const data=parse(new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer()));
          if (g!==this.generation || !this.active()) return;
          r.data=data;r.body=JSON.stringify(data);
          r.status=seen.has(r.body)?'duplicate':'ready';r.selected=r.status==='ready';seen.add(r.body);
          if(r.status==='duplicate')r.detail='本批相同内容，仅保留首份；不发送';
        } catch(e) { if(g!==this.generation || !this.active())return; r.status='invalid';r.detail=e.message; }
        this.notify(this);
      }
      if(g===this.generation && this.active()){this.preparing=false;this.notify(this);}
    }
    async run(send) {
      if(this.busy || this.preparing || !this.active())return;
      const g=this.generation, rows=this.rows.filter(r=>r.status==='ready' && r.selected);
      this.busy=true;this.notify(this);
      try {
        for(const r of rows) {
          if(g!==this.generation || !this.active())break;
          r.status='sending';this.notify(this);
          try {
            const result=await send(r.body);
            if(g!==this.generation || !this.active())break;
            if(result.saved!==true)throw Error('服务端未确认保存');
            r.status=result.created?'success':'existing';r.detail=result.task_id?'单文件索引已排队':'已保存；未确认新索引任务，请查看素材状态';
          } catch(e) { if(g!==this.generation || !this.active())break; r.status='failed';r.detail='未确认：'+e.message+'。可能已经保存，请查看项目；不自动重试。'; }
          this.notify(this);
        }
      } finally {if(g===this.generation){this.busy=false;if(this.active())this.notify(this);}}
    }
  }
  const exported={Batch,parse,MAX_FILES,MAX_BYTES,MAX_TOTAL};
  if(typeof module!=='undefined')module.exports=exported; else root.CaptureBatch=exported;
})(globalThis);
