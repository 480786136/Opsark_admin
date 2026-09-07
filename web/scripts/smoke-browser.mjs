import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({
    viewport: { width: 1360, height: 900 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(process.env.SMOKE_URL);
  await page.getByLabel("管理员账号").fill("smoke");
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.SMOKE_PASSWORD);
  await page.getByRole("button", { name: "登录管理平台" }).click();
  await page.getByRole("heading", { name: "工作概览" }).waitFor();
  await page.getByRole("button", { name: "知识文档", exact: true }).click();
  await page
    .getByRole("heading", { name: "Nginx 配置检查", exact: true })
    .waitFor();
  await mkdir("test-results", { recursive: true });
  await page.screenshot({
    path: "test-results/knowledge-documents.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "检索调试", exact: true }).click();
  await page.getByLabel("查询").fill("Nginx");
  await page.getByRole("button", { name: "检索已发布知识" }).click();
  await page
    .getByRole("heading", { name: "[K1] Nginx 配置检查", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await page.getByRole("button", { name: "登录管理平台" }).waitFor();
  if (errors.length) throw new Error(errors.join("\n"));
} finally {
  await browser.close();
}
