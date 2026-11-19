export function cleanUrl(value) {
  const input = String(value).trim();
  if (input.length > 2048 || /[\u0000-\u0020\u007f\\]/u.test(input) || !/^https?:\/\/[^/]/iu.test(input)) throw new Error("请填入不含凭据的 HTTP(S) 来源网址。");
  let url;
  try { url = new URL(input); } catch { throw new Error("来源网址格式不正确。"); }
  if (url.username || url.password || input.split("/",3)[2].includes("@")) throw new Error("来源网址不能含账号或密码。");
  url.search = "";
  url.hash = "";
  if (url.href.length > 2048) throw new Error("来源网址过长。");
  return url.href;
}

export function encodeCapture({title, sourceUrl, text, capturedAt}) {
  title = String(title).trim();
  text = String(text).trim();
  if (!title || [...title].length > 300 || /[\u0000-\u001f\u007f]/u.test(title)) throw new Error("标题须为不超过 300 字的单行文字。");
  if (!text || [...text].length > 100000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(text)) throw new Error("摘录须为不超过 100,000 字的纯文本。");
  if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/u.test(capturedAt) || !Number.isFinite(Date.parse(capturedAt))) throw new Error("抓取时间无效，请重新读取。");
  const result = JSON.stringify({schema:"nas-ai-web-capture/v1",title,source_url:cleanUrl(sourceUrl),captured_at:capturedAt,text,selection_only:true});
  if (new TextEncoder().encode(result).byteLength > 256 * 1024) throw new Error("导出超过 256 KiB，请缩短摘录。");
  return result;
}

// This function is serialized and runs in the isolated world of the top frame.
export function readSelection() {
  const blocked = "form,input,textarea,select,button,iframe,frame,[contenteditable]:not([contenteditable='false']),[role='textbox'],[role='searchbox']";
  const focused = document.activeElement;
  if (focused?.closest(blocked) || focused?.matches("iframe,frame") || focused?.shadowRoot) return null;
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const ancestor = range.commonAncestorContainer;
  if (!document.body?.contains(ancestor) || ancestor.getRootNode() !== document) return null;
  const parent = ancestor.nodeType === 1 ? ancestor : ancestor.parentElement;
  if (!parent || parent.closest(blocked)) return null;
  for (const node of parent.querySelectorAll(blocked)) if (range.intersectsNode(node)) return null;
  const raw = selection.toString();
  if (raw.length > 200000) return {tooLong:true};
  if (!raw.trim()) return null;
  const source = new URL(location.href);
  if (!["http:", "https:"].includes(source.protocol) || source.username || source.password) return null;
  source.search = "";
  source.hash = "";
  return {title:document.title.slice(0,600),sourceUrl:source.href,text:raw,capturedAt:new Date().toISOString()};
}
