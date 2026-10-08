/** Do not replace a backend failure with the browser's JSON parsing exception. */
export async function readAdminResponse(response) {
  if (response.status === 204) return null;
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(
      `后台接口返回 HTTP ${response.status}，响应不是有效 JSON。请检查 Admin 服务日志；更新代码后请确认已执行数据库迁移。`,
    );
  }
  if (!response.ok) {
    throw new Error(result?.error?.message || result?.error?.code || `请求失败（HTTP ${response.status}）`);
  }
  return result;
}

const resources = {
  config: "后台配置",
  providers: "供应商",
  routes: "模型路由",
  "model-keys": "访问 Key",
  "model-presets": "模型预设",
};

/** A failed route query must not discard a successfully loaded key-storage status. */
export async function loadModelManagement(api, assign) {
  const paths = Object.keys(resources);
  const results = await Promise.allSettled(paths.map(async path => {
    try {
      assign(path, await api(path));
    } catch (error) {
      assign(path, path === "config" ? {} : []);
      throw error;
    }
  }));
  const failures = results.flatMap((result, index) => result.status === "rejected"
    ? [`${resources[paths[index]]}加载失败：${result.reason?.message || "接口请求失败"}`] : []);
  if (failures.length) throw new Error(failures.join("；"));
}
