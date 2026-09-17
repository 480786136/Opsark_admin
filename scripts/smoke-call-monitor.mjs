// Fresh database/browser only. Synthetic call rows; never dispatches a paid model request.
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
const directory = mkdtempSync(join(tmpdir(), "opsark-call-monitor-"));
const password = randomBytes(24).toString("hex");
const portServer = createServer();
await new Promise(resolve => portServer.listen(0, "127.0.0.1", resolve));
const port = portServer.address().port;
await new Promise(resolve => portServer.close(resolve));
const base = `http://127.0.0.1:${port}`;
const env = { ...process.env, DATABASE_URL: `sqlite:///${join(directory, "monitor.db")}`,
  TEST_PASSWORD: password, COOKIE_SECURE: "false", ALLOWED_ORIGINS: base, KNOWLEDGE_SERVICE_TOKEN: "",
  MODEL_KEY_ENCRYPTION_KEY: randomBytes(32).toString("base64url") + "=", GITHUB_CLIENT_ID: "", GITHUB_CLIENT_SECRET: "" };
execFileSync(python, ["-m", "alembic", "upgrade", "head"], { cwd: root, env, stdio: "pipe" });
execFileSync(python, ["-c", `
import json,os,time
from argon2 import PasswordHasher
from app.db import SessionLocal
from app.models import Admin,ModelCall,ModelCallDetail
from app.call_details import snapshot
from app.gateway import cipher
from app.user_models import UserAccount,CreditAccount
db=SessionLocal()
db.add(Admin(username="smoke",password_hash=PasswordHasher().hash(os.environ["TEST_PASSWORD"])))
for uid in ("member-a","member-b"):
    db.add(UserAccount(id=uid,email=f"{uid}@example.test",password_hash="unused",disabled=False,created_at=time.time()-3600))
    db.flush()
    db.add(CreditAccount(user_id=uid,available=12500,reserved=0))
for i in range(25):
    db.add(ModelCall(id=f"req-a-{i:02}",key_id=f"session-{i%2}",owner="member-a",user_id="member-a",task_id="task-deploy-synthetic",
        round_id="round-1",step_id=f"step-{i:02}",phase_index=1,operation="review",client_request_id=f"core-request-{i:02}",
        model="trial-model",provider_id="synthetic-provider",started_at=time.time()-1000+i,status="failed" if i==24 else "succeeded",
        input_tokens=10 if i!=24 else None,output_tokens=5 if i!=24 else None,duration_ms=250,http_status=503 if i==24 else 200))
db.add(ModelCall(id="req-other-user",key_id="other-session",owner="member-b",user_id="member-b",task_id="task-deploy-synthetic",model="trial-model",provider_id="synthetic-provider",started_at=time.time()-100,status="succeeded"))
db.add(ModelCall(id="req-old-unlinked",key_id="old-key",owner="legacy integration",model="trial-model",provider_id="synthetic-provider",started_at=time.time()-50,status="succeeded"))
db.commit()
for index in (0,24):
    detail = {"input": snapshot({"messages": [{"role": "user", "content": "synthetic official prompt"}]}),
        "output": snapshot({"error": {"message": "synthetic provider rejection"}} if index == 24 else
            {"choices": [{"message": {"content": "synthetic official answer"}}]}),
        "stream": False,"finish_reasons": [] if index == 24 else ["stop"]}
    db.add(ModelCallDetail(call_id=f"req-a-{index:02}",
        encrypted_content=cipher().encrypt(json.dumps(detail).encode()).decode(),expires_at=time.time()+7*86400))
db.commit()
db.close()
`], { cwd: root, env, stdio: "pipe" });
const child = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], { cwd: root, env, stdio: "ignore" });
let browser, page;
try {
  let ready = false;
  for (let i=0; i<100; i++) {
    try { if ((await fetch(base + "/health/live")).ok) { ready=true; break; } } catch { /* startup */ }
    await new Promise(resolve => setTimeout(resolve,100));
  }
  assert.ok(ready);
  browser = await chromium.launch({ headless: true, ...(process.env.OPSARK_SMOKE_BROWSER ? { channel: process.env.OPSARK_SMOKE_BROWSER } : {}) });
  page = await browser.newPage({ viewport: { width: 1512, height: 1000 } });
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.goto(base);
  await page.getByLabel("管理员账号").fill("smoke");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "调用监控", exact: true }).click();
  const monitor = page.locator(".call-monitor");
  await expect(monitor).toHaveAttribute("aria-busy", "false");
  await expect(monitor.getByLabel("筛选范围统计")).toContainText("27");
  await expect(monitor.getByRole("row").filter({ hasText: "req-old-unli" })).toContainText("未关联任务");
  await page.screenshot({ path: join(directory,"monitor-requests.png"), fullPage:true });
  await monitor.getByLabel("按 Admin 请求 ID 定位").fill("req-a-24");
  await monitor.getByRole("button", { name: "打开请求", exact:true }).click();
  const dialog = page.getByRole("dialog", { name: "模型请求详情", exact:true });
  await expect(dialog).toContainText("core-request-24");
  await expect(dialog).toContainText("member-a@example.test");
  await expect(dialog).toContainText("synthetic official prompt");
  await expect(dialog).toContainText("脱敏快照保留至");
  await dialog.getByRole("button", { name:"模型输出",exact:true }).click();
  await expect(dialog).toContainText("synthetic provider rejection");
  await expect(dialog).toContainText("未知 / 未知");
  await page.screenshot({ path: join(directory,"request-detail.png"), fullPage:true });
  await dialog.getByRole("button", { name: "task-deploy-synthetic", exact:true }).click();
  await expect(dialog).not.toBeVisible();
  const pager = monitor.getByRole("navigation", { name: "调用分页", exact:true });
  await expect(pager).toContainText("共 25 条");
  await expect(monitor.locator(".data-grid tbody tr").first()).toContainText("#1");
  await expect(monitor.locator(".data-grid tbody tr").first()).toContainText("req-a-00");
  await pager.getByRole("button", { name: "下一页", exact:true }).click();
  await expect(pager).toContainText("第 2 / 2 页");
  await expect(monitor.locator(".data-grid tbody tr")).toHaveCount(5);
  await expect(monitor.locator(".data-grid")).not.toContainText("member-b");
  await page.screenshot({ path: join(directory,"task-calls.png"), fullPage:true });
  await monitor.locator(".detail-breadcrumb").getByRole("button", { name:"member-a@example.test", exact:true }).click();
  const userDetail = page.getByRole("region", { name: "用户详情", exact:true });
  await expect(userDetail).toContainText("member-a@example.test");
  await expect(page.getByLabel("搜索用户", { exact:true })).not.toBeVisible();
  await expect(page.getByLabel("新注册赠送 Token")).not.toBeVisible();
  await page.screenshot({ path: join(directory,"user-detail.png"), fullPage:true });
  await userDetail.getByRole("button", { name: "查看任务与调用", exact:true }).click();
  await expect(monitor).toHaveAttribute("aria-busy","false");
  await expect(monitor.locator(".data-grid tbody tr")).toHaveCount(1);
  await expect(monitor.locator(".data-grid")).not.toContainText("member-b");
  await monitor.getByRole("button", { name:"查看任务",exact:true }).click();
  await expect(pager).toContainText("共 25 条");
  await monitor.getByRole("button", { name:"返回调用监控",exact:true }).click();
  await monitor.getByRole("button", { name:"调用请求",exact:true }).click();
  await monitor.getByLabel("按 Admin 请求 ID 定位").fill("req-a-00");
  await monitor.getByRole("button", { name:"打开请求",exact:true }).click();
  await expect(dialog).toContainText("synthetic official prompt");
  await dialog.getByRole("button", { name:"模型输出",exact:true }).click();
  await expect(dialog).toContainText("synthetic official answer");
  await dialog.getByRole("button", { name:"关闭",exact:true }).click();
  await monitor.getByLabel("按 Admin 请求 ID 定位").fill("req-old-unlinked");
  await monitor.getByRole("button", { name:"打开请求",exact:true }).click();
  await expect(dialog).toContainText("没有可用正文");
  await expect(dialog).toContainText("历史未采集的正文无法补回");
  await expect(dialog).not.toContainText("synthetic official answer");
  await dialog.getByRole("button", { name:"关闭",exact:true }).click();
  await monitor.getByLabel("搜索请求").fill("no-such-request");
  await monitor.getByRole("button", { name:"查询",exact:true }).click();
  await expect(monitor.getByText(/没有符合条件的调用请求/)).toBeVisible();
  await monitor.getByLabel("按 Admin 请求 ID 定位").fill("missing-call");
  await monitor.getByRole("button", { name:"打开请求",exact:true }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog).not.toContainText("core-request-24");
  await dialog.getByRole("button", { name:"关闭",exact:true }).click();
  await page.setViewportSize({ width:1024,height:768 });
  await page.locator(".list-scroll").evaluate(element => element.scrollTo(0,0));
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({ path:join(directory,"monitor-compact.png"),fullPage:true });
  assert.deepEqual(errors,[]);
  console.log("PASS: request filters, task grouping/pagination/order, user → task → request navigation, official input/output snapshots, historical empty details and error states.");
  console.log("Screenshots and isolated database: " + directory);
} catch (error) {
  if (page) await page.screenshot({ path: join(directory,"failure.png"),fullPage:true });
  console.log("Failure artifacts: " + directory); throw error;
} finally {
  if (browser) await browser.close();
  if (child.exitCode === null && child.signalCode === null) { const stopped = new Promise(resolve => child.once("exit",resolve)); child.kill(); await stopped; }
}
