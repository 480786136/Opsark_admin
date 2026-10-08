// Isolated DB, synthetic keys, private build directory. Never invokes model endpoints.
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium, expect } = require("@playwright/test");
const root = fileURLToPath(new URL("../", import.meta.url));
const python = join(root, ".venv", "bin/python");
const directory = mkdtempSync(join(tmpdir(), "opsark-presets-"));
const password = randomBytes(24).toString("hex");
const server = createServer();
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;
await new Promise(resolve => server.close(resolve));
const base = `http://127.0.0.1:${port}`;
const env = { ...process.env, DATABASE_URL: `sqlite:///${join(directory, "test.db")}`,
  TEST_PASSWORD: password, COOKIE_SECURE: "false", ALLOWED_ORIGINS: base,
  MODEL_KEY_ENCRYPTION_KEY: randomBytes(32).toString("base64url") + "=",
  KNOWLEDGE_SERVICE_TOKEN: "", GITHUB_CLIENT_ID: "", GITHUB_CLIENT_SECRET: "", GITHUB_CALLBACK_URL: "",
  SMOKE_DIST: join(directory, "dist"), SMOKE_PORT: String(port) };
execFileSync("npm", ["run", "build", "--", "--outDir", env.SMOKE_DIST], { cwd: join(root, "web"), env, stdio: "pipe" });
execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: root, env, stdio: "pipe" });
execFileSync(python, ["-c", 'import os; from app.db import SessionLocal; from app.models import Admin; from argon2 import PasswordHasher; db=SessionLocal(); db.add(Admin(username="smoke",password_hash=PasswordHasher().hash(os.environ["TEST_PASSWORD"]))); db.commit(); db.close()'], { cwd: root, env, stdio: "pipe" });
const bootstrap = `
import os
import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
from app.main import app
# Fail closed if an accidental model connection test is added to this smoke.
async def no_upstream(*args, **kwargs):
    raise RuntimeError("Upstream networking forbidden in preset smoke")
httpx.AsyncClient.send = no_upstream
app.router.routes = [route for route in app.router.routes if getattr(route, "name", None) != "web"]
app.mount("/", StaticFiles(directory=os.environ["SMOKE_DIST"], html=True), name="smoke-web")
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["SMOKE_PORT"]), log_level="error")
`;
const child = spawn(python, ["-c", bootstrap], { cwd: root, env, stdio: "ignore" });
let browser;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base + "/health/live")).ok) { ready = true; break; } } catch { /* startup */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, "isolated Admin startup failed");
  browser = await chromium.launch({ headless: true,
    ...(process.env.OPSARK_SMOKE_BROWSER ? { channel: process.env.OPSARK_SMOKE_BROWSER } : {}) });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(base);
  await page.getByLabel("管理员账号").fill("smoke");
  await page.getByLabel("密码", { exact: true }).fill(password);
  // A pre-migration backend failure must not masquerade as a missing master key.
  await page.route("**/api/admin/v1/routes", route => route.fulfill({ status: 500, contentType: "text/plain", body: "Internal Server Error" }));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator(".list-scroll > p[role='alert']").filter({ hasText: "模型路由加载失败：后台接口返回 HTTP 500" })).toBeVisible();
  await expect(page.getByText("尚未配置 MODEL_KEY_ENCRYPTION_KEY", { exact: false })).toHaveCount(0);
  await page.unroute("**/api/admin/v1/routes");
  await page.reload();
  await page.getByRole("button", { name: "新增供应商", exact: true }).click();
  const provider = page.locator("#provider-editor");
  await provider.getByLabel("供应商地址模板").selectOption("qwen-chat-v1");
  await expect(provider.getByLabel("API base URL（HTTP / HTTPS）")).toHaveValue("https://dashscope.aliyuncs.com/compatible-mode/v1");
  await provider.getByLabel("上游 API Key").fill("synthetic-not-live");
  await provider.getByRole("button", { name: "保存供应商", exact: true }).click();
  await expect(page.locator("#provider-editor")).not.toBeVisible();
  await page.getByRole("button", { name: "模型路由", exact: true }).click();
  await page.getByRole("button", { name: "新增模型路由", exact: true }).click();
  const form = page.locator("#route-editor");
  await form.getByLabel("模型接入预设").selectOption("qwen-chat-v1");
  await expect(form.getByLabel("上游真实模型名")).toHaveValue("qwen-plus");
  await form.getByLabel("对外模型名", { exact: true }).fill("preset-test");
  const providers = await (await page.request.get(base + "/api/admin/v1/providers")).json();
  await form.getByLabel("供应商", { exact: true }).selectOption(providers[0].id);
  await form.locator("summary").click();
  await form.getByLabel("max_completion_tokens", { exact: false }).fill("777");
  await form.getByRole("button", { name: "预览参数（不调用模型）" }).click();
  await expect(form.locator("pre")).toContainText('"enable_thinking": false');
  await expect(form.locator("pre")).toContainText('"max_tokens": 777');
  await form.getByRole("button", { name: "保存路由", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(directory, "preset-preview.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await form.getByRole("button", { name: "保存路由", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(directory, "preset-preview-narrow.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await form.getByRole("button", { name: "保存路由", exact: true }).click();
  await expect(form).not.toBeVisible();
  let routes = await (await page.request.get(base + "/api/admin/v1/routes")).json();
  assert.equal(routes[0].preset_id, "qwen-chat-v1");
  assert.equal(routes[0].parameter_overrides.max_completion_tokens, 777);
  // Edit, retain native parameters, switch to custom and enter an unlisted model.
  await page.getByRole("button", { name: "preset-test", exact: true }).click();
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(form.getByLabel("max_completion_tokens", { exact: false })).toHaveValue("777");
  await form.getByLabel("模型接入预设").selectOption("openai-gpt41-v1");
  await expect(form.getByLabel("上游真实模型名")).toHaveValue("gpt-4.1");
  await form.getByLabel("模型接入预设").selectOption("deepseek-chat-v1");
  await expect(form.getByLabel("上游真实模型名")).toHaveValue("deepseek-flash");
  await form.getByLabel("模型接入预设").selectOption("custom");
  await form.getByLabel("上游真实模型名").fill("my-unlisted-model");
  await form.getByRole("button", { name: "保存路由", exact: true }).click();
  await expect(form).not.toBeVisible();
  routes = await (await page.request.get(base + "/api/admin/v1/routes")).json();
  assert.equal(routes[0].preset_id, "custom");
  assert.equal(routes[0].upstream_model, "my-unlisted-model");
  assert.equal(routes[0].parameter_overrides.max_completion_tokens, 777);
  // Explicit Responses declaration and budget preview remain local, with no model call.
  // The record drawer remains open after the preceding editor closes.
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await form.getByLabel("API 协议").selectOption("responses");
  await form.getByLabel("已确认的输出能力").selectOption("json_schema");
  await form.getByLabel("生成预算计数").selectOption("total_output");
  await form.locator("details.advanced-panel").evaluate((node) => { node.open = true; });
  await form.getByRole("button", { name: "清空", exact: true }).click();
  await form.getByLabel("最大生成预算（max_output_tokens）").fill("777");
  await form.getByRole("button", { name: "预览参数（不调用模型）", exact: true }).click();
  await expect(form.locator("pre")).toContainText('"max_output_tokens": 777');
  await expect(form.locator("pre")).not.toContainText('"max_tokens"');
  await page.screenshot({ path: join(directory, "responses-preview.png"), fullPage: true });
  await form.getByRole("button", { name: "保存路由", exact: true }).click();
  await expect(form).not.toBeVisible();
  routes = await (await page.request.get(base + "/api/admin/v1/routes")).json();
  assert.equal(routes[0].api_protocol, "responses");
  assert.equal(routes[0].structured_output, "json_schema");
  assert.equal(routes[0].parameter_overrides.max_output_tokens, 777);
  assert.deepEqual(await (await page.request.get(base + "/api/admin/v1/calls")).json(), []);
  assert.deepEqual(errors, []);
  console.log("PASS: three presets, custom model, native field round-trip, Responses declaration and free parameter preview; no upstream calls.");
  console.log("Synthetic artifacts retained at: " + directory);
} finally {
  if (browser) await browser.close();
  if (child.exitCode === null && child.signalCode === null) {
    const stopped = new Promise(resolve => child.once("exit", resolve));
    child.kill();
    await stopped;
  }
}
