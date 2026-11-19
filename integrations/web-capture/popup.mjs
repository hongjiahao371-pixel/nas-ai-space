import {cleanUrl, encodeCapture, readSelection} from "./capture.mjs";
const byId = id => document.getElementById(id);
const read = byId("read"), save = byId("save"), preview = byId("preview");
const title = byId("title"), url = byId("url"), text = byId("text"), status = byId("status");
let capturedAt = null;

read.addEventListener("click", async () => {
  read.disabled = true;
  preview.hidden = true;
  capturedAt = null;
  title.value = url.value = text.value = "";
  status.textContent = "";
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    if (!Number.isInteger(tab?.id) || !/^https?:\/\//iu.test(tab.url ?? "")) {
      status.textContent = "请选择普通 HTTP(S) 网页。";
      return;
    }
    const [sample] = await chrome.scripting.executeScript({target:{tabId:tab.id,frameIds:[0]},func:readSelection});
    const data = sample?.result;
    if (!data) {
      status.textContent = "没有可读选区。请选中网页正文，不要选表单或嵌入页面。";
      return;
    }
    if (data.tooLong || [...data.text].length > 100000) {
      status.textContent = "选区超过 100,000 字，请缩小选区。";
      return;
    }
    const source = cleanUrl(data.sourceUrl);
    if (source !== cleanUrl(tab.url)) {
      status.textContent = "页面在读取时发生变化，请重新选中文字。";
      return;
    }
    title.value = [...data.title.trim().replace(/[\u0000-\u001f\u007f]/gu," ")].slice(0,300).join("") || "网页摘录";
    url.value = source;
    text.value = data.text;
    capturedAt = data.capturedAt;
    preview.hidden = false;
    status.textContent = "请核对摘录。尚未上传或保存。";
  } catch {
    status.textContent = "无法读取这张网页。浏览器保护页面或无访问权限时不读取。";
  } finally {
    read.disabled = false;
  }
});

save.addEventListener("click", () => {
  if (!capturedAt || preview.hidden) return;
  try {
    const payload = encodeCapture({title:title.value,sourceUrl:url.value,text:text.value,capturedAt});
    url.value = cleanUrl(url.value);
    const objectUrl = URL.createObjectURL(new Blob([payload],{type:"application/json"}));
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = "web-selection.nas-capture.json";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    status.textContent = "已请求浏览器保存。请到下载记录中核对文件，再到 NAS 导入。";
  } catch (error) {
    status.textContent = error.message;
  }
});
