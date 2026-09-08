<script setup>
import { ref, reactive, computed, onMounted } from "vue";
const user = ref(null),
  csrf = ref(""),
  busy = ref(false),
  error = ref(""),
  notice = ref(""),
  tab = ref("providers");
const providers = ref([]),
  routes = ref([]),
  keys = ref([]),
  calls = ref([]),
  config = ref({});
const freshKey = ref(""),
  offset = ref(0);
const login = reactive({ username: "admin", password: "" });
const provider = reactive({
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  enabled: true,
  timeout_seconds: 60,
});
const route = reactive({
  id: "",
  alias: "",
  provider_id: "",
  upstream_model: "",
  enabled: true,
});
const keyForm = reactive({
  owner: "",
  allowed_models: [],
  expires_days: 30,
  rpm: 30,
});
const baseURL = window.location.origin + "/v1";
const sample = computed(() => ({
  total: calls.value.length,
  success: calls.value.filter((c) => c.status === "succeeded").length,
  failed: calls.value.filter((c) => c.status === "failed").length,
}));
async function api(path, method = "GET", body) {
  const response = await fetch("/api/admin/v1/" + path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf.value },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "session") user.value = null;
    throw new Error(result.error?.message || result.error?.code || "请求失败");
  }
  return result;
}
async function action(fn) {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    await fn();
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function refresh() {
  [config.value, providers.value, routes.value, keys.value, calls.value] =
    await Promise.all(
      [
        "config",
        "providers",
        "routes",
        "model-keys",
        "calls?offset=" + offset.value,
      ].map((p) => api(p)),
    );
}
async function signIn() {
  const s = await api("session", "POST", login);
  user.value = s.username;
  csrf.value = s.csrf;
  login.password = "";
  await refresh();
}
async function signOut() {
  await api("session", "DELETE");
  user.value = null;
  csrf.value = "";
  freshKey.value = "";
  provider.api_key = "";
  keys.value = [];
  calls.value = [];
}
function resetProvider() {
  Object.assign(provider, {
    id: "",
    name: "",
    base_url: "",
    api_key: "",
    enabled: true,
    timeout_seconds: 60,
  });
}
function resetRoute() {
  Object.assign(route, {
    id: "",
    alias: "",
    provider_id: "",
    upstream_model: "",
    enabled: true,
  });
}
async function saveProvider() {
  const { id, ...body } = provider;
  await api("providers" + (id ? "/" + id : ""), id ? "PUT" : "POST", body);
  resetProvider();
  await refresh();
  notice.value = "供应商已保存";
}
async function saveRoute() {
  const { id, ...body } = route;
  await api("routes" + (id ? "/" + id : ""), id ? "PUT" : "POST", body);
  resetRoute();
  await refresh();
}
async function issueKey() {
  freshKey.value = "";
  const r = await api("model-keys", "POST", keyForm);
  freshKey.value = r.api_key;
  await refresh();
}
onMounted(() =>
  action(async () => {
    try {
      const s = await api("session");
      user.value = s.username;
      csrf.value = s.csrf;
    } catch {
      return;
    }
    await refresh();
  }),
);
</script>
<template>
  <main v-if="!user" class="login">
    <div class="brand">O<span>Opsark</span></div>
    <h1>模型接入管理</h1>
    <p>第三方 API 配置、模型转发与调用监控。无需启动知识服务。</p>
    <form @submit.prevent="action(signIn)">
      <label
        >管理员账号<input
          v-model="login.username"
          autocomplete="username"
          required /></label
      ><label
        >密码<input
          v-model="login.password"
          type="password"
          autocomplete="current-password"
          required /></label
      ><button :disabled="busy">登录</button>
    </form>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <small>首次运行 python -m app.bootstrap 创建本平台管理员。</small>
  </main>
  <div v-else class="shell">
    <aside>
      <div class="brand">O<span>Opsark</span></div>
      <div class="subtitle">MODEL CONSOLE</div>
      <nav>
        <button
          v-for="[id, label] in [
            ['providers', '供应商 API'],
            ['routes', '模型路由'],
            ['keys', '用户模型 Key'],
            ['calls', '调用监控'],
          ]"
          :key="id"
          :class="{ active: tab === id }"
          @click="
            tab = id;
            freshKey = '';
            provider.api_key = '';
          "
        >
          {{ label }}
        </button>
      </nav>
      <div class="account">
        {{ user
        }}<button class="secondary" @click="action(signOut)">退出</button>
      </div>
    </aside>
    <section class="workspace">
      <header>
        <div>
          <small>独立模型平台</small>
          <h1>
            {{
              {
                providers: "供应商 API",
                routes: "模型路由",
                keys: "用户模型 Key",
                calls: "调用监控",
              }[tab]
            }}
          </h1>
        </div>
        <button :disabled="busy" @click="action(refresh)">刷新</button>
      </header>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <article>
        <p>
          core 模型 base URL：<code>{{ baseURL }}</code>
        </p>
        <p>
          只接受平台签发的模型 Key；不是上游 Key、知识 Key
          或管理员密码。首版支持 Chat Completions 兼容 API，其他协议暂不支持。
        </p>
        <p v-if="!config.encryption_configured" class="error">
          尚未配置 MODEL_KEY_ENCRYPTION_KEY，不能保存上游 Key。请按 README
          初始化，不能丢失或随意替换。
        </p>
        <p>
          允许上游主机：{{
            config.allowed_hosts?.join(", ") ||
            "未配置 MODEL_ALLOWED_HOSTS；禁止向任意地址转发"
          }}
        </p>
      </article>
      <template v-if="tab === 'providers'"
        ><article>
          <h2>{{ provider.id ? "编辑" : "新增" }}供应商</h2>
          <form @submit.prevent="action(saveProvider)">
            <label
              >名称<input
                v-model="provider.name"
                required
                maxlength="100" /></label
            ><label
              >API base URL（HTTPS）<input
                v-model="provider.base_url"
                required
                placeholder="https://provider.example/v1" /></label
            ><label
              >上游 API Key<input
                v-model="provider.api_key"
                type="password"
                autocomplete="new-password"
                :required="!provider.id"
                placeholder="编辑留空保留；更换地址必须重输"
            /></label>
            <label
              >超时秒数<input
                v-model.number="provider.timeout_seconds"
                type="number"
                min="5"
                max="300"
                required /></label
            ><label
              ><input v-model="provider.enabled" type="checkbox" />启用</label
            ><button :disabled="busy">保存</button
            ><button type="button" class="secondary" @click="resetProvider">
              取消编辑
            </button>
          </form>
        </article>
        <article v-for="p in providers" :key="p.id">
          <h2>{{ p.name }} · {{ p.enabled ? "启用" : "停用" }}</h2>
          <p>{{ p.base_url }} · {{ p.timeout_seconds }} 秒</p>
          <button
            @click="
              Object.assign(provider, {
                id: p.id,
                name: p.name,
                base_url: p.base_url,
                api_key: '',
                enabled: p.enabled,
                timeout_seconds: p.timeout_seconds,
              })
            "
          >
            编辑 / 启停
          </button>
          <button
            :disabled="busy || !p.enabled"
            @click="
              action(async () => {
                const r = await api('providers/' + p.id + '/test', 'POST');
                notice =
                  '模型列表探测：HTTP ' +
                  r.http_status +
                  ' / ' +
                  r.duration_ms +
                  'ms；不代表生成能力已验证';
              })
            "
          >
            测试连接
          </button>
        </article></template
      >
      <template v-if="tab === 'routes'"
        ><article>
          <h2>{{ route.id ? "编辑" : "新增" }}模型路由</h2>
          <form @submit.prevent="action(saveRoute)">
            <label
              >对外模型名<input
                v-model="route.alias"
                :disabled="!!route.id"
                required
                maxlength="128" /></label
            ><label
              >供应商<select v-model="route.provider_id" required>
                <option value="">请选择</option>
                <option v-for="p in providers" :key="p.id" :value="p.id">
                  {{ p.name }}
                </option>
              </select></label
            ><label
              >上游真实模型名<input
                v-model="route.upstream_model"
                required
                maxlength="200" /></label
            ><label><input v-model="route.enabled" type="checkbox" />启用</label
            ><button :disabled="busy">保存路由</button
            ><button type="button" class="secondary" @click="resetRoute">
              取消
            </button>
          </form>
        </article>
        <article v-for="r in routes" :key="r.id">
          <h2>{{ r.alias }} · {{ r.enabled ? "启用" : "停用" }}</h2>
          <p>
            {{ providers.find((p) => p.id === r.provider_id)?.name }} /
            {{ r.upstream_model }}
          </p>
          <button @click="Object.assign(route, r)">编辑 / 切换渠道</button>
        </article></template
      >
      <template v-if="tab === 'keys'"
        ><article>
          <h2>签发用户模型 Key</h2>
          <form @submit.prevent="action(issueKey)">
            <label
              >用户/客户标识<input
                v-model="keyForm.owner"
                maxlength="128"
                required /></label
            ><label
              >允许模型（可多选）<select
                v-model="keyForm.allowed_models"
                multiple
                required
              >
                <option
                  v-for="r in routes.filter((r) => r.enabled)"
                  :key="r.id"
                  :value="r.alias"
                >
                  {{ r.alias }}
                </option>
              </select></label
            ><label
              >有效天数<input
                v-model.number="keyForm.expires_days"
                type="number"
                min="1"
                max="365"
                required /></label
            ><label
              >每分钟请求上限<input
                v-model.number="keyForm.rpm"
                type="number"
                min="1"
                max="600"
                required /></label
            ><button :disabled="busy">生成 Key</button>
          </form>
          <div v-if="freshKey">
            <p>完整 Key 仅显示本次，请安全保存，不放进日志或截图。</p>
            <pre>{{ freshKey }}</pre>
            <button @click="freshKey = ''">隐藏</button>
          </div>
        </article>
        <article v-for="k in keys" :key="k.id">
          <h2>{{ k.owner }} · {{ k.prefix }}…</h2>
          <p>
            {{ k.allowed_models.join(", ") }} · {{ k.rpm }} RPM · 到期
            {{ new Date(k.expires * 1000).toLocaleString() }}
          </p>
          <p v-if="k.revoked">已撤销</p>
          <button
            v-else
            :disabled="busy"
            @click="
              action(async () => {
                if (!confirm('撤销后此 Key 不能发起新请求，确认？')) return;
                await api('model-keys/' + k.id, 'DELETE');
                await refresh();
              })
            "
          >
            撤销 Key
          </button>
        </article></template
      >
      <template v-if="tab === 'calls'"
        ><article>
          <h2>当前页统计（最多 100 条）</h2>
          <p>
            请求 {{ sample.total }} · 成功 {{ sample.success }} · 失败
            {{ sample.failed }} · 成功比例
            {{
              sample.total
                ? Math.round((sample.success / sample.total) * 100)
                : 0
            }}%
          </p>
          <p>
            只保存请求元数据，不保存对话/密钥。Token “未知”不是
            0，不可直接当作计费账本。running
            表示尚无完成记录，进程异常退出也可能留下此状态。
          </p>
          <button
            :disabled="busy || offset === 0"
            @click="
              action(async () => {
                offset = Math.max(0, offset - 100);
                await refresh();
              })
            "
          >
            上一页</button
          ><button
            :disabled="busy || calls.length < 100"
            @click="
              action(async () => {
                offset += 100;
                await refresh();
              })
            "
          >
            下一页
          </button>
        </article>
        <article v-for="c in calls" :key="c.id">
          <h2>{{ c.model }} · {{ c.status }}</h2>
          <p>
            {{ c.owner }} ·
            {{
              providers.find((p) => p.id === c.provider_id)?.name ||
              c.provider_id
            }}
            · {{ new Date(c.started_at * 1000).toLocaleString() }}
          </p>
          <p>
            HTTP {{ c.http_status ?? "未知" }} ·
            {{ c.duration_ms ?? "未知" }} ms · 输入
            {{ c.input_tokens ?? "未知" }} / 输出
            {{ c.output_tokens ?? "未知" }}
          </p>
          <p v-if="c.error_code">{{ c.error_code }}</p>
          <small>请求 ID：{{ c.id }}</small>
        </article></template
      >
    </section>
  </div>
</template>
