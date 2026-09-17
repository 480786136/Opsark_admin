// Isolated browser + HTTP verification. Never uses the existing database or real model providers.
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
const python = join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const directory = mkdtempSync(join(tmpdir(), "opsark-accounts-"));
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
let browser;
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
  const providerResponse = await page.request.post(base + "/api/admin/v1/providers", { headers, data: {
    name: "Synthetic only", base_url: "https://upstream.example.test/v1", api_key: "synthetic-not-live",
  } });
  assert.equal(providerResponse.status(), 201);
  const provider = await providerResponse.json();
  const route = await page.request.post(base + "/api/admin/v1/routes", { headers, data: {
    alias: "trial-model", display_name: "官方体验模型", provider_id: provider.id, upstream_model: "synthetic-model",
  } });
  assert.equal(route.status(), 201);
  assert.equal((await page.request.post(base + "/api/admin/v1/routes", { headers, data: {
    alias: "staff-only-model", display_name: "未开放模型", provider_id: provider.id, upstream_model: "synthetic-private",
  } })).status(), 201);
  await page.getByRole("button", { name: "用户与额度", exact: true }).click();
  await page.getByRole("button", { name: "注册与模型策略", exact: true }).click();
  await page.getByRole("heading", { name: "注册与体验额度", exact: true }).waitFor();
  await page.getByLabel("开放注册").check();
  await page.getByLabel("新注册赠送 Token").fill("1000");
  await page.getByRole("checkbox", { name: "向 Core 开放 官方体验模型 (trial-model)", exact: true }).check();
  await page.getByRole("button", { name: "保存策略", exact: true }).click();
  await page.getByText("已保存。新额度仅用于后续注册；官方模型范围对所有用户生效。", { exact: true }).waitFor();
  assert.deepEqual((await (await fetch(base + "/api/core/v1/official-models")).json()).models,
    [{ id: "trial-model", name: "官方体验模型" }]);
  const registration = await page.request.post(base + "/api/core/v1/register", { data: { email: "first@example.test", password } });
  assert.equal(registration.status(), 201);
  const account = await registration.json();
  assert.equal(account.balance.available, 1000);
  const userHeaders = { Authorization: "Bearer " + account.access_token };
  await page.getByRole("button", { name: "用户列表", exact: true }).click();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: "first@example.test" });
  await expect(row).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "历史预留 Token", exact: true })).toHaveCount(0);
  await row.getByRole("button", { name: "管理用户", exact: true }).click();
  await expect(page.locator(".summary-strip")).toContainText("可用 Token");
  await expect(page.locator(".summary-strip")).not.toContainText("预留");
  await expect(page.getByRole("columnheader", { name: "历史预留变化", exact: true })).toHaveCount(0);
  // Legacy display fixtures are response-only; no accounting rows are changed.
  const detailPattern = base + "/api/admin/v1/users/" + account.user.id;
  const ledgerPattern = detailPattern + "/ledger";
  await page.route(detailPattern, async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), reserved: 80 } });
  });
  await page.route(ledgerPattern, async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: data.map(item => ({ ...item, reserved_delta: 80 })) });
  });
  await page.getByRole("button", { name: "刷新用户详情", exact: true }).click();
  await expect(page.locator(".summary-strip")).toContainText("历史预留 Token（待结算）");
  await expect(page.getByRole("columnheader", { name: "历史预留变化", exact: true })).toBeVisible();
  await page.unroute(detailPattern);
  await page.unroute(ledgerPattern);
  await page.getByRole("button", { name: "刷新用户详情", exact: true }).click();
  await expect(page.locator(".summary-strip")).not.toContainText("预留");
  await expect(page.getByRole("columnheader", { name: "历史预留变化", exact: true })).toHaveCount(0);
  await page.getByLabel("调整数量").fill("100");
  await page.getByLabel("原因", { exact: true }).fill("内测体验补充额度");
  const adjustment = page.waitForResponse(r => r.url().endsWith("/adjustments") && r.request().method() === "POST");
  await page.getByRole("button", { name: "提交额度调整", exact: true }).click();
  assert.equal((await adjustment).status(), 200);
  await page.getByText("额度已调整并记入账本。", { exact: true }).waitFor();
  const me = await (await page.request.get(base + "/api/core/v1/me", { headers: userHeaders })).json();
  assert.equal(me.balance.available, 1100);
  const ledger = await (await page.request.get(base + "/api/core/v1/usage", { headers: userHeaders })).json();
  assert.equal(ledger.length, 2);
  const models = await (await page.request.get(base + "/v1/models", { headers: userHeaders })).json();
  assert.deepEqual(models.data.map(m => m.id), ["trial-model"]);
  assert.equal(models.data[0].name, "官方体验模型");
  assert.equal(await page.locator(".empty-state").count(), 0);
  assert.equal(await page.locator(".list-pagination").count(), 0);
  const feedback = await page.request.post(base + "/api/core/v1/feedback", { headers: userHeaders, data: {
    mutation_id: randomBytes(16).toString("hex"), title: "Smoke task feedback", message: "Synthetic isolated feedback",
    category: "task", core_version: "0.3.0", consent: true,
    diagnostic: { taskId: "task-smoke", status: "failed", stepCount: 1, failedSteps: 1, completedSteps: 0 },
    log_excerpt: "operation=review; synthetic failure",
  } });
  assert.equal(feedback.status(), 201);
  const ticket = await feedback.json();
  await page.getByRole("button", { name: "联系与反馈", exact: true }).click();
  await page.getByRole("button", { name: "联系方式与兼容策略", exact: true }).click();
  await page.getByRole("heading", { name: "联系与客户端兼容策略", exact: true }).waitFor();
  await page.getByLabel("联系邮箱", { exact: true }).fill("support@example.test");
  await page.getByLabel("官方联系页面").fill("https://support.example.test");
  const savePolicy = page.waitForResponse(r => r.url().endsWith("/client-policy") && r.request().method() === "PUT");
  await page.getByRole("button", { name: "保存联系与兼容策略", exact: true }).click();
  assert.equal((await savePolicy).status(), 200);
  await page.getByRole("dialog", { name: "联系与兼容设置", exact: true }).getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "版本管理", exact: true }).click();
  await page.getByRole("button", { name: "发布系统版本", exact: true }).click();
  await page.getByLabel("版本", { exact: true }).fill("0.3.1");
  await page.getByLabel("官网下载地址（HTTPS）", { exact: true }).fill("https://downloads.example.test/core.dmg");
  await page.getByLabel("安装包 SHA-256").fill("a".repeat(64));
  await page.getByLabel("更新说明", { exact: true }).fill("Synthetic version guidance");
  const publish = page.waitForResponse(r => r.url().endsWith("/client-releases") && r.request().method() === "POST");
  await page.getByRole("button", { name: "发布版本引导", exact: true }).click();
  assert.equal((await publish).status(), 201);
  const info = await (await page.request.get(base + "/api/core/v1/client-info?platform=macos&arch=aarch64&version=0.3.0")).json();
  assert.equal(info.latest.version, "0.3.1"); assert.equal(info.support_email, "support@example.test");
  await page.getByRole("button", { name: "联系与反馈", exact: true }).click();
  await page.getByRole("button", { name: "查看工单", exact: true }).click();
  await page.getByRole("heading", { name: "Smoke task feedback", exact: true }).waitFor();
  await page.getByRole("button", { name: "处理与回复", exact: true }).click();
  await page.getByLabel("工单状态").selectOption("resolved");
  await page.getByLabel("工单回复（匿名反馈请另通过所留联系方式跟进）", { exact: true }).fill("Synthetic support response");
  const replyResponse = page.waitForResponse(r => r.url().endsWith("/feedback/" + ticket.id) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "保存回复与状态", exact: true }).click();
  assert.equal((await replyResponse).status(), 200);
  const myTicket = await (await page.request.get(base + "/api/core/v1/feedback/" + ticket.id, { headers: userHeaders })).json();
  assert.equal(myTicket.status, "resolved"); assert.equal(myTicket.content.replies[0].message, "Synthetic support response");
  await page.getByRole("dialog", { name: "反馈工单详情", exact: true }).getByRole("button", { name: "关闭", exact: true }).click();
  // Seed pagination fixtures directly into this script's isolated database, not the user's database.
  execFileSync(python, ["-c", 'from app.db import SessionLocal; from app.user_models import UserAccount,CreditAccount; db=SessionLocal(); [db.add(UserAccount(id=f"pagination-{i:02}",email=f"page-{i:02}@example.test",password_hash="unused",created_at=1000,disabled=False)) for i in range(25)]; db.flush(); [db.add(CreditAccount(user_id=f"pagination-{i:02}",available=0,reserved=0)) for i in range(25)]; db.commit(); db.close()'], { cwd: root, env, stdio: "pipe" });
  await page.getByRole("button", { name: "用户与额度", exact: true }).click();
  const pagination = page.getByRole("navigation", { name: "用户分页", exact: true });
  await expect(pagination).toContainText("第 1 / 2 页");
  await pagination.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(pagination).toContainText("第 2 / 2 页");
  await page.getByLabel("搜索用户", { exact: true }).fill("no-such-person");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.getByText("没有匹配用户，请调整搜索条件。", { exact: true }).waitFor();
  await page.getByLabel("搜索用户", { exact: true }).fill(account.user.id);
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "first@example.test" }).getByRole("button", { name: "管理用户", exact: true }).click();
  await page.getByRole("button", { name: "会话管理", exact: true }).click();
  const sessionUrl = base + "/api/admin/v1/users/" + account.user.id + "/sessions";
  const secondLogin = await (await page.request.post(base + "/api/core/v1/login", { data: { email: "first@example.test", password } })).json();
  const secondHeaders = { Authorization: "Bearer " + secondLogin.access_token };
  await page.getByRole("button", { name: "刷新会话", exact: true }).click();
  const originalSession = (await (await page.request.get(sessionUrl)).json()).items.at(-1);
  const sessionRow = page.getByRole("row").filter({ hasText: originalSession.id });
  await sessionRow.getByRole("button", { name: "撤销", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "确认撤销会话", exact: true })).toBeDisabled();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  assert.equal((await page.request.get(base + "/api/core/v1/me", { headers: userHeaders })).status(), 200);
  await sessionRow.getByRole("button", { name: "撤销", exact: true }).click();
  await dialog.getByLabel("操作原因", { exact: true }).fill("Synthetic single-session revocation");
  await dialog.getByRole("button", { name: "确认撤销会话", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(sessionRow).toContainText("已撤销");
  assert.equal((await page.request.get(base + "/api/core/v1/me", { headers: userHeaders })).status(), 401);
  assert.equal((await page.request.get(base + "/api/core/v1/me", { headers: secondHeaders })).status(), 200);
  await page.getByRole("button", { name: "撤销全部会话", exact: true }).click();
  await dialog.getByLabel("操作原因", { exact: true }).fill("Synthetic sign out everywhere");
  await dialog.getByRole("button", { name: "确认撤销全部会话", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  assert.equal((await page.request.get(base + "/api/core/v1/me", { headers: secondHeaders })).status(), 401);
  await page.getByRole("button", { name: "封禁用户", exact: true }).click();
  await dialog.getByLabel("操作原因", { exact: true }).fill("Synthetic account ban");
  await dialog.getByRole("button", { name: "确认封禁用户", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("button", { name: "解封用户", exact: true })).toBeVisible();
  assert.equal((await page.request.post(base + "/api/core/v1/login", { data: { email: "first@example.test", password } })).status(), 401);
  await page.getByRole("button", { name: "解封用户", exact: true }).click();
  await dialog.getByLabel("操作原因", { exact: true }).fill("Synthetic account restoration");
  await dialog.getByRole("button", { name: "确认解封用户", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const restored = await page.request.post(base + "/api/core/v1/login", { data: { email: "first@example.test", password } });
  assert.equal(restored.status(), 200); assert.equal((await restored.json()).balance.available, 1100);
  assert.equal((await page.request.get(base + "/api/core/v1/me", { headers: userHeaders })).status(), 401);
  await page.getByRole("button", { name: "刷新会话", exact: true }).click();
  await expect(page.locator(".user-panel")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByRole("navigation", { name: "会话分页", exact: true })).toContainText("共 3 条");
  await page.screenshot({ path: join(directory, "user-sessions.png"), fullPage: true });
  execFileSync(python, ["-c", 'import sys,time; from app.db import SessionLocal; from app.user_models import UserSession; db=SessionLocal(); [db.add(UserSession(id=f"paging-session-{i:02}",user_id=sys.argv[1],access_hash=f"unused-access-{i}",refresh_hash=f"unused-refresh-{i}",access_expires=time.time()-1,refresh_expires=time.time()+9000,revoked=False)) for i in range(23)]; db.commit(); db.close()', account.user.id], { cwd: root, env, stdio: "pipe" });
  await page.getByRole("button", { name: "刷新会话", exact: true }).click();
  const sessionPagination = page.getByRole("navigation", { name: "会话分页", exact: true });
  await expect(sessionPagination).toContainText("第 1 / 2 页");
  await sessionPagination.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(sessionPagination).toContainText("第 2 / 2 页");
  await page.getByLabel("会话状态", { exact: true }).selectOption("revoked");
  await expect(sessionPagination).toContainText("共 2 条 · 第 1 / 1 页");
  await page.getByLabel("会话状态", { exact: true }).selectOption("expired");
  await page.getByText("当前条件下没有会话。", { exact: true }).waitFor();
  // Filters and security operations must not silently discard an unsaved registration-policy edit.
  await page.getByRole("button", { name: "返回用户列表", exact: true }).click();
  await page.getByRole("button", { name: "注册与模型策略", exact: true }).click();
  await page.getByLabel("新注册赠送 Token").fill("2345");
  await page.getByRole("button", { name: "用户列表", exact: true }).click();
  await page.getByLabel("账号状态", { exact: true }).selectOption("disabled");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await page.getByText("没有匹配用户，请调整搜索条件。", { exact: true }).waitFor();
  await page.getByRole("button", { name: "注册与模型策略", exact: true }).click();
  await expect(page.getByLabel("新注册赠送 Token")).toHaveValue("2345");
  await page.getByRole("button", { name: "用户列表", exact: true }).click();
  await page.getByLabel("账号状态", { exact: true }).selectOption("all");
  await page.getByLabel("搜索用户", { exact: true }).fill("");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(pagination).toContainText("第 1 / 2 页");
  await page.locator(".list-scroll").evaluate(element => element.scrollTo(0, 0));
  await page.screenshot({ path: join(directory, "users-and-model-scope.png"), fullPage: true });
  await page.getByRole("button", { name: "注册与模型策略", exact: true }).click();
  const offering = page.getByRole("checkbox", { name: "向 Core 开放 官方体验模型 (trial-model)", exact: true });
  await offering.uncheck();
  await page.getByRole("button", { name: "保存策略", exact: true }).click();
  await page.getByText("已保存。新额度仅用于后续注册；官方模型范围对所有用户生效。", { exact: true }).waitFor();
  assert.deepEqual((await (await fetch(base + "/api/core/v1/official-models")).json()).models, []);
  assert.deepEqual(errors, []);
  console.log("PASS: isolated Admin registration/credits, Core model scope, user search/pagination, ban/unban, single/all session revocation, contact/releases/feedback; no browser errors or upstream calls.");
  console.log("Synthetic test database retained at: " + directory);
} finally {
  if (browser) await browser.close();
  if (child.exitCode === null && child.signalCode === null) {
    const stopped = new Promise(resolve => child.once("exit", resolve));
    child.kill();
    await stopped;
  }
}
