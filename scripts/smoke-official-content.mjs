// Isolated browser + HTTP verification. Never uses the existing database or real model providers.
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { randomBytes, createHash } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const root = fileURLToPath(new URL("../", import.meta.url));
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const directory = mkdtempSync(join(tmpdir(), "opsark-official-content-"));
const password = randomBytes(24).toString("hex");
const portServer = createServer();
await new Promise(resolve => portServer.listen(0, "127.0.0.1", resolve));
const port = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const base = `http://127.0.0.1:${port}`;
const env = { ...process.env, DATABASE_URL: `sqlite:///${join(directory, "accounts.db").replaceAll("\\", "/")}`,
  TEST_PASSWORD: password, COOKIE_SECURE: "false", ALLOWED_ORIGINS: base,
  KNOWLEDGE_SERVICE_TOKEN: "", MODEL_KEY_ENCRYPTION_KEY: randomBytes(32).toString("base64url") + "=",
  GITHUB_CLIENT_ID: "", GITHUB_CLIENT_SECRET: "", GITHUB_CALLBACK_URL: "", RELEASE_ALLOWED_HOSTS: "downloads.example.test" };
execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: root, env, stdio: "pipe" });
execFileSync(python, ["-c", 'import os; from app.db import SessionLocal; from app.models import Admin; from argon2 import PasswordHasher; db=SessionLocal(); db.add(Admin(username="smoke",password_hash=PasswordHasher().hash(os.environ["TEST_PASSWORD"]))); db.commit(); db.close()'], { cwd: root, env, stdio: "pipe" });
const child = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: root, env, stdio: "ignore" });
let browser, coreChild;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base + "/health/live")).ok) { ready = true; break; } } catch { /* startup */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, "isolated Admin did not start");
  browser = await chromium.launch({ headless: true,
    ...(process.env.OPSARK_SMOKE_BROWSER ? { channel: process.env.OPSARK_SMOKE_BROWSER } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(base);
  await page.getByLabel("管理员账号").fill("smoke");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("heading", { name: "供应商 API", exact: true }).waitFor();
  const session = await (await page.request.get(base + "/api/admin/v1/session")).json();
  const headers = { "X-CSRF-Token": session.csrf };
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "系统 Skill", exact: true }).click();
  await page.getByRole("button", { name: "保存草稿", exact: true }).waitFor();
  assert.equal(await page.locator(".content-panel .data-grid tbody tr").count(), 7);
  await page.locator(".content-panel .data-grid tbody tr").first().getByRole("button", { name: "编辑详情", exact: true }).click();
  await page.getByLabel("名称", { exact: true }).fill("官方工作流浏览器验证");
  await page.getByRole("button", { name: "保存目录草稿", exact: true }).click();
  await page.getByRole("dialog", { name: "系统 Skill 详情", exact: true }).getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByText("草稿已保存，发布后 Core 才会获取。", { exact: true }).waitFor();
  const infoURL = base + "/api/core/v1/client-info?platform=macos&arch=aarch64&content_protocol=2&version=0.3.0";
  assert.equal((await (await fetch(infoURL)).json()).system_skills, null);
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await page.getByLabel("发布说明", { exact: true }).fill("UI skill publication");
  await page.getByRole("button", { name: "保存并发布", exact: true }).click();
  await page.getByText("已发布。Core 下次检查更新时自动同步；运行中任务保留 Skill 快照，工具配置对后续派发生效。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "发布历史", exact: true }).click();
  let info = await (await fetch(infoURL)).json();
  assert.equal(info.system_skills.version, 1);
  let content = await (await fetch(base + info.system_skills.download_path)).json();
  assert.equal(JSON.parse(content.content).items[0].name, "官方工作流浏览器验证");
  await page.getByRole("button", { name: "查看内容", exact: true }).click();
  await page.getByRole("heading", { name: "发布内容 v1", exact: true }).waitFor();
  await page.screenshot({ path: join(directory, "system-skills.png"), fullPage: true });
  await page.getByRole("button", { name: "复制到草稿", exact: true }).click();
  await page.getByRole("button", { name: "工具管理", exact: true }).click();
  await page.locator(".content-panel .data-grid tbody tr").first().waitFor();
  assert.equal(await page.locator(".content-panel .data-grid tbody tr").count(), 13);
  await page.locator(".content-panel .data-grid tbody tr").first().getByRole("button", { name: "编辑详情", exact: true }).click();
  const toggle = page.getByRole("checkbox", { name: "启用此工具", exact: true });
  await toggle.uncheck();
  const editor = page.getByRole("dialog", { name: "工具配置详情", exact: true });
  await editor.getByRole("button", { name: "关闭", exact: true }).click();
  const fileTableRow = page.locator(".content-panel .data-grid tbody tr").filter({ hasText: "files.read_content" });
  await fileTableRow.getByRole("button", { name: "编辑详情", exact: true }).click();
  const fileRow = editor;
  await fileRow.getByLabel("工具名称", { exact: true }).fill("读取文件参数验证");
  const schemaInput = fileRow.getByLabel("参数协议（JSON）", { exact: true });
  const schema = JSON.parse(await schemaInput.inputValue());
  Object.assign(schema.properties.maxBytes, { default: 4096, maximum: 16384 });
  await schemaInput.fill("{broken");
  await page.getByRole("button", { name: "保存目录草稿", exact: true }).click();
  await editor.getByRole("alert").filter({ hasText: "参数 JSON 格式不正确" }).waitFor();
  await schemaInput.fill(JSON.stringify(schema, null, 2));
  await page.getByRole("button", { name: "保存目录草稿", exact: true }).click();
  await page.getByText("草稿已保存，发布后 Core 才会获取。", { exact: true }).waitFor();
  await editor.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "刷新草稿", exact: true }).click();
  await page.getByRole("button", { name: "保存草稿", exact: true }).waitFor({ state: "visible" });
  await fileTableRow.getByRole("button", { name: "编辑详情", exact: true }).click();
  assert.equal(JSON.parse(await schemaInput.inputValue()).properties.maxBytes.default, 4096);
  assert.equal(await fileRow.getByLabel("工具名称", { exact: true }).inputValue(), "读取文件参数验证");
  await fileRow.screenshot({ path: join(directory, "tool-parameters.png") });
  assert.equal((await (await fetch(infoURL)).json()).tools, null);
  await editor.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  await page.getByLabel("发布说明", { exact: true }).fill("UI tool parameters and switching");
  await page.getByRole("button", { name: "保存并发布", exact: true }).click();
  await page.getByText("已发布。Core 下次检查更新时自动同步；运行中任务保留 Skill 快照，工具配置对后续派发生效。", { exact: true }).waitFor();
  info = await (await fetch(infoURL)).json();
  assert.equal(info.tools.version, 1);
  content = await (await fetch(base + info.tools.download_path)).json();
  assert.equal(JSON.parse(content.content).items[0].enabled, false);
  assert.equal(JSON.parse(content.content).schema_version, 2);
  assert.equal(JSON.parse(content.content).items.find(t => t.id === "files.read_content").inputSchema.properties.maxBytes.default, 4096);
  assert.equal((await (await fetch(infoURL.replace("content_protocol=2&", ""))).json()).tools, null);
  await page.screenshot({ path: join(directory, "tools.png"), fullPage: true });
  // Run Core against these real isolated public APIs using a synthetic native bridge.
  const coreRoot = fileURLToPath(new URL("../../Opsark_core/", import.meta.url));
  const reservation = createServer();
  await new Promise(resolve => reservation.listen(0, "127.0.0.1", resolve));
  const corePort = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const coreBase = `http://127.0.0.1:${corePort}`;
  coreChild = spawn(process.execPath, ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", String(corePort), "--strictPort"], { cwd: coreRoot, stdio: "ignore" });
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(coreBase)).ok) break; } catch { /* dev startup */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const core = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  core.on("pageerror", error => errors.push(error.message));
  core.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  const cache = [], pending = new Map();
  await core.exposeFunction("officialBridge", async (command, args) => {
    if (command === "cloud_request" && args.operation === "client_info") return (await fetch(infoURL)).json();
    assert.equal(command, "official_content_request");
    if (args.operation === "cache") return cache;
    if (args.operation === "download") {
      const response = await fetch(`${base}/api/core/v1/official-content/${args.kind}/${args.releaseId}`);
      if (!response.ok) throw new Error("download unavailable");
      const value = await response.json();
      assert.equal(createHash("sha256").update(value.content).digest("hex"), value.sha256);
      pending.set(value.id, value); return value;
    }
    assert.equal(args.operation, "activate");
    cache.push(pending.get(args.releaseId)); pending.delete(args.releaseId); return { ok: true };
  });
  await core.goto(coreBase + "/#/support");
  try { await core.getByRole("heading", { name: "联系、反馈与更新", exact: true }).waitFor({ timeout: 12000 }); }
  catch (error) { console.log("Core startup diagnostics:", errors, (await core.locator("body").innerText()).slice(0, 1500)); throw error; }
  const synced = await core.evaluate(async () => {
    window.__TAURI_INTERNALS__ = { invoke: window.officialBridge };
    const { useUpdateStore } = await import("/src/features/support/updateStore.ts");
    const { useOpsStore } = await import("/src/stores/ops.ts");
    const { hydrateOfficialContent } = await import("/src/features/support/officialContent.ts");
    const { defaultToolCatalog } = await import("/src/features/tools/toolCatalog.ts");
    const { buildToolContext } = await import("/src/features/tools/toolContext.ts");
    const { parseToolCommand, executeToolCall } = await import("/src/features/tools/toolExecutor.ts");
    const ops = useOpsStore(), updates = useUpdateStore();
    const userSkill = ops.addSkill(); userSkill.instructions = "Personal content stays local.";
    await updates.check();
    const tool = buildToolContext(defaultToolCatalog).find(t => t.id === "files.read_content");
    const call = parseToolCommand('opsark-tool files.read_content {"path":"/tmp/synthetic"}', "configured");
    const dispatched = [];
    const dependencies = { getRemoteFileStructure: async () => ({}), readRemoteFileContent: async request => {
      dispatched.push(request); return { content: "synthetic", truncated: false };
    } };
    const executed = await executeToolCall({ ...call, arguments: { path: "/tmp/synthetic" } }, defaultToolCatalog, dependencies);
    const rejected = await executeToolCall({ ...call, arguments: { path: "/tmp/synthetic", maxBytes: 16385 } }, defaultToolCatalog, dependencies);
    const online = { versions: updates.contentVersions, error: updates.contentError,
      name: ops.skills.find(s => s.builtIn).name, user: ops.skills.find(s => s.id === userSkill.id).instructions,
      enabled: ops.tools.find(t => t.id === "server.basic_info").enabled,
      toolName: tool.name, configurationVersion: tool.configurationVersion, defaultBytes: call.arguments.maxBytes,
      executed: executed.success, rejected: rejected.success, dispatched };
    await hydrateOfficialContent(); ops.refreshOfficialContent();
    return { online, cached: ops.skills.find(s => s.builtIn).name,
      cachedBytes: parseToolCommand('opsark-tool files.read_content {"path":"/tmp/synthetic"}', "cached").arguments.maxBytes };
  });
  assert.deepEqual(synced.online.versions, { skills: 1, tools: 1 });
  assert.equal(synced.online.error, "");
  assert.equal(synced.online.name, "官方工作流浏览器验证");
  assert.equal(synced.online.user, "Personal content stays local.");
  assert.equal(synced.online.enabled, false);
  assert.equal(synced.online.toolName, "读取文件参数验证");
  assert.equal(synced.online.configurationVersion, 1);
  assert.equal(synced.online.defaultBytes, 4096);
  assert.equal(synced.online.executed, true);
  assert.equal(synced.online.rejected, false);
  assert.deepEqual(synced.online.dispatched, [{ path: "/tmp/synthetic", maxBytes: 4096 }]);
  assert.equal(synced.cachedBytes, 4096);
  assert.equal(synced.cached, synced.online.name);
  await core.screenshot({ path: join(directory, "core-sync.png"), fullPage: true });
  await core.close();

  await page.getByRole("button", { name: "发布历史", exact: true }).click();
  await page.getByRole("button", { name: "撤回", exact: true }).click();
  await page.getByText("已撤回，停止后续分发。", { exact: true }).waitFor();
  assert.equal((await (await fetch(infoURL)).json()).tools, null);
  assert.equal((await fetch(base + info.tools.download_path)).status, 410);
  await page.getByRole("button", { name: "版本管理", exact: true }).click();
  await page.getByRole("button", { name: "发布系统版本", exact: true }).click();
  await page.getByLabel("版本", { exact: true }).fill("0.3.1");
  await page.getByLabel("官网下载地址（HTTPS）", { exact: true }).fill("https://downloads.example.test/core");
  await page.getByLabel("安装包 SHA-256", { exact: true }).fill("a".repeat(64));
  await page.getByLabel("更新说明", { exact: true }).fill("UI system release");
  await page.getByRole("button", { name: "发布版本引导", exact: true }).click();
  await page.getByText("已发布更新引导。客户端会检查平台/架构，用户手动下载安装。", { exact: true }).waitFor();
  info = await (await fetch(infoURL)).json();
  assert.equal(info.latest.version, "0.3.1");
  await page.screenshot({ path: join(directory, "versions.png"), fullPage: true });
  await page.getByRole("button", { name: "撤回引导", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "0.3.1" }).getByText("已撤回", { exact: true }).waitFor();
  assert.equal((await (await fetch(infoURL)).json()).latest, null);
  assert.deepEqual(errors, []);
  console.log("PASS: Admin Skill editing/publication/history, tool parameter editing/JSON errors/draft reload/publication/withdrawal, system releases; legacy protocol compatibility; Core context/defaults/dispatch constraints/cache reload and personal Skill preservation verified with a synthetic native bridge.");
  console.log("Screenshots and isolated database: " + directory);
} finally {
  if (coreChild && coreChild.exitCode === null && coreChild.signalCode === null) {
    const stopped = new Promise(resolve => coreChild.once("exit", resolve)); coreChild.kill(); await stopped;
  }
  if (browser) await browser.close();
  if (child.exitCode === null && child.signalCode === null) {
    const stopped = new Promise(resolve => child.once("exit", resolve));
    child.kill();
    await stopped;
  }
}
