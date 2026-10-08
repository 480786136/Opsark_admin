import { test } from "node:test";
import assert from "node:assert/strict";
import { loadModelManagement, readAdminResponse } from "../src/adminApi.js";

test("plain-text backend 500 reports status without exposing response or a browser parsing error", async () => {
  await assert.rejects(readAdminResponse(new Response("Internal Server Error: private SQL", { status: 500 })), error => {
    assert.match(error.message, /HTTP 500/);
    assert.match(error.message, /数据库迁移/);
    assert.doesNotMatch(error.message, /private SQL|expected pattern/);
    return true;
  });
});

test("structured API errors and successful data retain their contracts", async () => {
  await assert.rejects(readAdminResponse(Response.json({ error: { message: "上游密钥无法解密" } }, { status: 503 })), /上游密钥无法解密/);
  assert.deepEqual(await readAdminResponse(Response.json({ encryption_configured: true })), { encryption_configured: true });
  assert.equal(await readAdminResponse(new Response(null, { status: 204 })), null);
});

test("a route failure preserves loaded encryption config and completes independent loads", async () => {
  const state = {};
  await assert.rejects(loadModelManagement(async path => {
    if (path === "routes") throw new Error("HTTP 500");
    if (path === "config") return { encryption_configured: true };
    await Promise.resolve();
    return [{ id: path }];
  }, (path, value) => { state[path] = value; }), /模型路由加载失败：HTTP 500/);
  assert.equal(state.config.encryption_configured, true);
  assert.deepEqual(state.routes, []);
  assert.deepEqual(state["model-presets"], [{ id: "model-presets" }]);
});

test("a config request failure becomes unknown rather than a missing master key", async () => {
  const state = { config: { encryption_configured: false } };
  await assert.rejects(loadModelManagement(async path => {
    if (path === "config") throw new Error("offline");
    return [];
  }, (path, value) => { state[path] = value; }), /后台配置加载失败/);
  assert.equal(state.config.encryption_configured, undefined);
});
