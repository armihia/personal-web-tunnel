"use strict";
let session = "", state = null, editing = null, updating = null, renderedRows = "", publicRefresh = 0;
const byId = id => document.getElementById(id);
const dialog = byId("editor");

async function request(path, method = "GET", body) {
  const response = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-Manager-Token": session }, body: body === undefined ? undefined : JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) { const error = new Error(result.error || "操作失败，请重试。"); error.status = response.status; throw error; }
  return result;
}
function notice(message) { byId("notice").textContent = message; byId("notice").hidden = !message; }
function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
function publicUrl(path) { let prefix = "/" + path.trim().replace(/^\/+|\/+$/g, ""); return "https://" + (state?.domain || "demo.example.com") + (prefix === "/" ? "/" : prefix + "/"); }
function domainSuffix() { return (state?.domain_suffix || (state?.domain || "demo.example.com").replace(/^[^.]+\./, "")).toLowerCase(); }
function fullHostname() { const label = byId("hostname").value.trim().toLowerCase(); return label ? label + "." + domainSuffix() : ""; }
function updatePreview() { const host = fullHostname(); byId("url-preview").textContent = host ? "https://" + host + "/" : publicUrl(byId("path").value); }
function updateHostname() { const active = !!byId("hostname").value.trim(); byId("path").disabled = active; if (active) byId("path").value = ""; updatePreview(); }

function edit(item = null) {
  editing = item?.id || null;
  byId("editor-title").textContent = item ? "编辑映射" : "添加映射";
  byId("name").value = item?.name || "";
  byId("port").value = item?.port || "";
  byId("scheme").value = item?.scheme || "http";
  byId("ca-file").value = item?.tls_ca_file || "";
  byId("ca-file-label").hidden = byId("scheme").value !== "https";
  byId("path").value = item?.hostname ? "" : (item?.path || "");
  const suffix = "." + domainSuffix();
  byId("hostname-suffix").textContent = suffix;
  byId("hostname").value = item?.hostname?.endsWith(suffix) ? item.hostname.slice(0, -suffix.length) : "";
  byId("hostname").disabled = !state?.subdomain_supported;
  byId("hostname").placeholder = state?.subdomain_supported ? "例如 xxx" : "请先退出后台服务并重新启动管理器";
  byId("strip").checked = item?.strip_prefix ?? true;
  byId("enabled").checked = item?.enabled ?? true;
  byId("form-error").hidden = true;
  updateHostname(); dialog.showModal(); byId("name").focus();
}
async function remove(item) {
  if (!window.confirm(`删除「${item.name}」的 ${item.hostname || item.path} 映射？本地网站会继续运行。`)) return;
  try { await request("/api/mappings/" + item.id, "DELETE"); await refresh(true); } catch (error) { notice(error.message); }
}
async function toggle(item) {
  try { await request("/api/mappings/" + item.id, "PUT", { ...item, enabled: !item.enabled }); await refresh(true); } catch (error) { notice(error.message); }
}
function render() {
  byId("domain").textContent = state.domain;
  byId("server").textContent = state.server;
  byId("count").textContent = state.mappings.length;
  byId("connection-dot").className = "dot" + (state.connected ? " online" : !state.desired ? " offline" : "");
  byId("connection-title").textContent = state.local_only ? "本地预览" : state.connected ? "隧道已连接" : state.desired ? "正在连接" : "隧道已停止";
  byId("connection-description").textContent = state.local_only ? "可管理映射，公网隧道尚未启动" : state.distributed && state.desired ? "协作模式已自动启用，正在逐条维护映射" : state.connected ? "新增映射后直接生效" : state.desired ? "正在建立服务器连接，失败会自动重试" : "映射已保存，可随时重新连接";
  byId("connect").textContent = state.desired ? "停止隧道" : "连接隧道";
  byId("connect").disabled = state.local_only;
  notice(state.error || "");
  byId("shared-test-status").hidden = !state.shared_test?.enabled;
  byId("shared-test-status").textContent = state.shared_test?.message || "";
  const rowData = JSON.stringify({ connected: state.connected, mappings: state.mappings });
  if (rowData !== renderedRows) {
  renderedRows = rowData;
  const rows = byId("rows"); rows.replaceChildren();
  for (const item of state.mappings) {
    const row = element("tr");
    const name = element("td"); name.append(element("span", item.name, "site-name"), element("span", item.hostname ? (state.automatic_subdomains_supported ? "独立域名：一次配置后自动生效" : "独立域名：请重启管理器以启用自动配置") : item.strip_prefix ? "转发时去掉前缀" : "转发时保留前缀", "site-detail"));
    const port = element("td"); port.append(element("span", String(item.port), "port"));
    const address = element("td"), link = element("a", item.url.replace("https://", "")); const target = new URL(item.url); if (publicRefresh) target.searchParams.set("_tunnel_refresh", String(publicRefresh)); link.href = target.href; link.target = "_blank"; link.rel = "noopener noreferrer"; address.append(link);
    const status = element("td"), published = item.published ?? (state.connected && item.enabled && item.local_up); const label = !item.enabled ? (item.disabled_reason?.includes("占用") || item.disabled_reason === "地址已由其他电脑使用" ? "冲突已停用" : item.disabled_reason ? "待核对" : "已停用") : !item.local_up ? "本地未监听" : item.coordination_error ? "待核对" : published ? (item.hostname ? "隧道已连接" : "已发布") : state.shared_test?.enabled && !item.hostname && item.path.toLowerCase() === "/test" ? "入口未注册" : state.distributed ? "正在申请" : "本地可用"; status.append(element("span", label, "badge " + (!item.enabled ? "gray" : published ? "green" : "amber")));
    const actions = element("td"), buttons = element("div", undefined, "actions");
    for (const [label, action, className] of [["编辑", () => edit(item), ""], [item.enabled ? "停用" : "启用", () => toggle(item), ""], ["删除", () => remove(item), "danger"]]) { const button = element("button", label, className); button.type = "button"; button.addEventListener("click", action); buttons.append(button); }
    actions.append(buttons); row.append(name, port, address, status, actions); rows.append(row);
  }
  }
  byId("empty").hidden = state.mappings.length !== 0;
  byId("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}
async function refresh(force = false) {
  if (updating) { await updating; if (!force) return; }
  const pending = (async () => {
    try { state = await request("/api/status"); render(); }
    catch (error) { notice("管理服务暂不可用。若已退出，可再次双击启动管理器。"); }
  })();
  updating = pending;
  try { await pending; } finally { if (updating === pending) updating = null; }
}
byId("refresh").onclick = async () => {
  const button = byId("refresh"); button.disabled = true; button.textContent = "正在刷新…";
  byId("refresh-result").hidden = true;
  try {
    session = (await request("/api/session")).token;
    await request("/api/refresh", "POST", {});
    publicRefresh = Date.now(); renderedRows = "";
    await refresh(true);
    byId("refresh-result").textContent = "映射已刷新。请点击公网地址重新打开；若仍显示旧内容，请在网站页面按 Ctrl+F5。";
    byId("refresh-result").hidden = false;
  } catch (error) { notice(error.status === 404 ? "刷新功能已更新，请先退出后台服务，再重新双击启动管理器。" : error.message); }
  finally { button.disabled = false; button.textContent = "刷新映射"; }
};
byId("add").onclick = () => edit(); byId("empty-add").onclick = () => edit();
byId("cancel").onclick = () => dialog.close(); byId("cancel-x").onclick = () => dialog.close(); byId("path").oninput = updatePreview;
byId("hostname").oninput = updateHostname;
byId("scheme").onchange = () => { byId("ca-file-label").hidden = byId("scheme").value !== "https"; };
byId("mapping-form").onsubmit = async event => {
  event.preventDefault(); byId("save").disabled = true;
  try { const hostname = fullHostname(); await request(editing ? "/api/mappings/" + editing : "/api/mappings", editing ? "PUT" : "POST", { name: byId("name").value, port: Number(byId("port").value), scheme: byId("scheme").value, tls_ca_file: byId("scheme").value === "https" ? byId("ca-file").value.trim() : "", path: hostname ? "/" : byId("path").value, hostname, strip_prefix: byId("strip").checked, enabled: byId("enabled").checked }); dialog.close(); await refresh(true); }
  catch (error) { byId("form-error").textContent = error.message; byId("form-error").hidden = false; }
  finally { byId("save").disabled = false; }
};
byId("connect").onclick = async () => { byId("connect").disabled = true; try { await request(state.desired ? "/api/tunnel/stop" : "/api/tunnel/start", "POST", {}); await refresh(true); } catch (error) { notice(error.message); } finally { byId("connect").disabled = !!state?.local_only; } };
byId("exit").onclick = async () => { if (!window.confirm("退出后台服务并停止穿透？映射列表会保留。")) return; try { await request("/api/shutdown", "POST", {}); byId("connection-title").textContent = "后台服务已退出"; notice("再次双击启动管理器即可恢复保存的映射。"); } catch (error) { notice(error.message); } };
(async () => { try { session = (await request("/api/session")).token; await refresh(true); setInterval(refresh, 3000); } catch (error) { notice(error.message); } })();
