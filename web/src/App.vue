<script setup>
import { ref, reactive, computed, onMounted, nextTick, watch } from "vue";
import WorkDrawer from "./WorkDrawer.vue";
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
const providerDrawer = ref(null),
  routeDrawer = ref(null);
const editingProviderId = ref("");
const login = reactive({ username: "admin", password: "" });
const emptyParameters = () => ({
  thinking_type: "",
  reasoning_effort: "",
  temperature: null,
  top_p: null,
  max_tokens: null,
  presence_penalty: null,
  frequency_penalty: null,
});
const parametersFromApi = (value = {}) => ({
  ...emptyParameters(),
  ...value,
  thinking_type: value.thinking?.type || "",
});
const parametersToApi = (value) => {
  const result = {};
  if (value.thinking_type) result.thinking = { type: value.thinking_type };
  if (value.reasoning_effort) result.reasoning_effort = value.reasoning_effort;
  for (const name of [
    "temperature",
    "top_p",
    "max_tokens",
    "presence_penalty",
    "frequency_penalty",
  ]) {
    if (value[name] !== null && value[name] !== "") result[name] = value[name];
  }
  return result;
};
const parameterSummary = (value = {}) => {
  const labels = {
    thinking: "思考",
    reasoning_effort: "推理强度",
    temperature: "温度",
    top_p: "Top P",
    max_tokens: "最大 Token",
    presence_penalty: "话题惩罚",
    frequency_penalty: "重复惩罚",
  };
  const entries = Object.entries(value).map(([name, current]) =>
    name === "thinking"
      ? `${labels[name]}=${current.type === "disabled" ? "关闭" : "开启"}`
      : `${labels[name] || name}=${current}`,
  );
  return entries.length ? entries.join(" · ") : "未设置";
};
const parameterHelp = {
  thinking:
    "控制模型是否先进行深度推理。结构化提炼通常建议关闭，以免推理耗尽输出 Token。",
  reasoning:
    "设置推理强度。不同供应商支持范围可能不同；关闭思考时只能留空或选择 none。",
  maxTokens: "限制单次生成的最大 Token。开启思考时通常同时包含推理和最终答案。",
  temperature: "控制随机性，越低越稳定。结构化提炼建议使用 0.2 左右。",
  topP: "核采样范围。通常只调整 Temperature 或 Top P 其中一个。",
  presence: "正值倾向引入新主题。知识提炼通常保持为空。",
  frequency: "正值降低重复表达。知识提炼通常保持为空。",
};
function applyKnowledgeParameters(target) {
  Object.assign(target, emptyParameters(), {
    thinking_type: "disabled",
    reasoning_effort: "none",
    temperature: 0.2,
  });
}
function clearParameters(target) {
  Object.assign(target, emptyParameters());
}
async function openProviderEditor(row = null) {
  resetProvider();
  if (row) {
    Object.assign(provider, {
      id: row.id,
      name: row.name,
      base_url: row.base_url,
      api_key: "",
      enabled: row.enabled,
      timeout_seconds: row.timeout_seconds,
      parameters: parametersFromApi(row.parameter_defaults),
    });
  }
  await nextTick();
  providerDrawer.value?.showModal();
}
function closeProviderEditor() {
  providerDrawer.value?.close();
  resetProvider();
}
function editProviderInline(row) {
  resetProvider();
  Object.assign(provider, {
    id: row.id,
    name: row.name,
    base_url: row.base_url,
    api_key: "",
    enabled: row.enabled,
    timeout_seconds: row.timeout_seconds,
    parameters: parametersFromApi(row.parameter_defaults),
  });
  editingProviderId.value = row.id;
}
function cancelProviderInline() {
  editingProviderId.value = "";
  resetProvider();
}
async function openRouteEditor(row = null) {
  resetRoute();
  if (row) {
    Object.assign(route, {
      ...row,
      parameters: parametersFromApi(row.parameter_overrides),
    });
  }
  await nextTick();
  routeDrawer.value?.showModal();
}
function closeRouteEditor() {
  routeDrawer.value?.close();
  resetRoute();
}
const provider = reactive({
  id: "",
  name: "",
  base_url: "",
  api_key: "",
  enabled: true,
  timeout_seconds: 60,
  parameters: emptyParameters(),
});
const route = reactive({
  id: "",
  alias: "",
  provider_id: "",
  upstream_model: "",
  enabled: true,
  parameters: emptyParameters(),
});
const keyForm = reactive({
  owner: "",
  allowed_models: [],
  expires_days: 30,
  rpm: 30,
});
const baseURL = window.location.origin + "/v1";
const query = ref("");
const callDetail = ref(null),
  callDialog = ref(null),
  groupBy = ref("model"),
  requestLookup = ref("");
async function inspectCall(id, showDialog = true) {
  callDetail.value = await api("calls/" + encodeURIComponent(id));
  await nextTick();
  if (showDialog && !callDialog.value.open) callDialog.value.showModal();
}
const callGroups = computed(() => {
  const groups = new Map();
  for (const c of calls.value) {
    const label =
      groupBy.value === "provider_id"
        ? providers.value.find((p) => p.id === c.provider_id)?.name ||
          c.provider_id
        : c[groupBy.value];
    const row = groups.get(label) || {
      label,
      count: 0,
      success: 0,
      input: 0,
      output: 0,
      unknownInput: 0,
      unknownOutput: 0,
    };
    row.count++;
    row.success += c.status === "succeeded" ? 1 : 0;
    if (c.input_tokens == null) row.unknownInput++;
    else row.input += c.input_tokens;
    if (c.output_tokens == null) row.unknownOutput++;
    else row.output += c.output_tokens;
    groups.set(label, row);
  }
  return [...groups.values()];
});
const localPage = ref(0);
watch([tab, query], () => {
  localPage.value = 0;
});
const matchingRows = (rows, fields) =>
  rows.filter((row) =>
    fields.some((field) =>
      String(row[field] || "")
        .toLowerCase()
        .includes(query.value.toLowerCase()),
    ),
  );
const currentRows = computed(() =>
  matchingRows(
    { providers: providers.value, routes: routes.value, keys: keys.value }[
      tab.value
    ] || [],
    {
      providers: ["name", "base_url"],
      routes: ["alias", "upstream_model"],
      keys: ["owner", "prefix"],
    }[tab.value] || [],
  ),
);
const filterRows = (rows, fields) =>
  matchingRows(rows, fields).slice(
    localPage.value * 20,
    localPage.value * 20 + 20,
  );
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
    parameters: emptyParameters(),
  });
}
function resetRoute() {
  Object.assign(route, {
    id: "",
    alias: "",
    provider_id: "",
    upstream_model: "",
    enabled: true,
    parameters: emptyParameters(),
  });
}
async function saveProvider() {
  const { id, parameters, ...body } = provider;
  body.parameter_defaults = parametersToApi(parameters);
  await api("providers" + (id ? "/" + id : ""), id ? "PUT" : "POST", body);
  if (editingProviderId.value) cancelProviderInline();
  else closeProviderEditor();
  await refresh();
  notice.value = "供应商已保存";
}
async function saveRoute() {
  const { id, parameters, ...body } = route;
  body.parameter_overrides = parametersToApi(parameters);
  await api("routes" + (id ? "/" + id : ""), id ? "PUT" : "POST", body);
  closeRouteEditor();
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
  <a v-if="user" class="skip-link" href="#main-content">跳到主要内容</a>
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
            query = '';
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
    <section id="main-content" class="workspace">
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
          <p class="page-description">
            {{
              {
                providers: "连接模型供应商，安全管理上游接口与凭据。",
                routes: "将上游模型映射为统一名称，客户端无需感知渠道变化。",
                keys: "为客户端签发访问凭据，控制可用模型与调用额度。",
                calls: "了解请求状态与耗时，快速定位调用问题。",
              }[tab]
            }}
          </p>
        </div>
        <button :disabled="busy" @click="action(refresh)">刷新</button>
      </header>
      <div class="list-scroll">
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <p v-if="notice" role="status">{{ notice }}</p>
        <div
          v-if="tab !== 'calls'"
          class="setup-path"
          aria-label="模型接入流程"
        >
          <button
            class="secondary"
            @click="
              tab = 'providers';
              query = '';
            "
          >
            连接供应商</button
          ><span>→</span
          ><button
            class="secondary"
            @click="
              tab = 'routes';
              query = '';
            "
          >
            映射模型</button
          ><span>→</span
          ><button
            class="secondary"
            @click="
              tab = 'keys';
              query = '';
            "
          >
            签发访问 Key
          </button>
        </div>
        <article v-if="tab === 'providers'" class="connection-card">
          <p>
            core 模型 base URL：<code>{{ baseURL }}</code>
          </p>
          <p v-if="!config.encryption_configured" class="error" role="alert">
            尚未配置 MODEL_KEY_ENCRYPTION_KEY，暂不能保存上游 Key。请按 README
            完成初始化。
          </p>
          <details>
            <summary>接口说明</summary>
            <p>
              只接受平台签发的模型 Key；不是上游 Key、知识 Key
              或管理员密码。首版支持 Chat Completions 兼容
              API，其他协议暂不支持。
            </p>
            <p>支持 HTTP/HTTPS、内网 IP 和自定义端口，无需配置主机白名单。</p>
          </details>
        </article>
        <div v-if="tab !== 'calls'" class="filter-bar">
          <label
            >搜索当前列表<input
              v-model="query"
              type="search"
              placeholder="输入名称、地址或用户标识" /></label
          ><span
            >{{
              {
                providers: providers.length,
                routes: routes.length,
                keys: keys.length,
              }[tab]
            }}
            条已加载记录</span
          >
          <button v-if="tab === 'providers'" @click="openProviderEditor()">
            新增供应商
          </button>
          <button v-if="tab === 'routes'" @click="openRouteEditor()">
            新增模型路由
          </button>
        </div>
        <template v-if="tab === 'providers'"
          ><dialog
            ref="providerDrawer"
            class="side-drawer"
            @cancel="resetProvider"
          >
            <section class="drawer-sheet">
              <p v-if="error" class="error" role="alert">{{ error }}</p>
              <header class="drawer-header">
                <div>
                  <small>供应商 API</small>
                  <h2>{{ provider.id ? "编辑供应商" : "新增供应商" }}</h2>
                </div>
                <button
                  type="button"
                  class="icon-button"
                  aria-label="关闭"
                  @click="closeProviderEditor"
                >
                  ×
                </button>
              </header>
              <form id="provider-editor" @submit.prevent="action(saveProvider)">
                <label
                  >名称<input
                    v-model="provider.name"
                    required
                    maxlength="100" /></label
                ><label
                  >API base URL（HTTP / HTTPS）<input
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
                    required
                /></label>
                <details class="advanced-panel">
                  <summary>
                    <span>高级参数默认值 <small>可选</small></span>
                    <small>{{
                      parameterSummary(parametersToApi(provider.parameters))
                    }}</small>
                  </summary>
                  <div class="parameter-toolbar">
                    <p>调用请求未指定时生效；通常无需修改。</p>
                    <button
                      type="button"
                      class="secondary"
                      @click="applyKnowledgeParameters(provider.parameters)"
                    >
                      应用 Knowledge 提炼推荐值
                    </button>
                    <button
                      type="button"
                      class="text-button"
                      @click="clearParameters(provider.parameters)"
                    >
                      清空
                    </button>
                  </div>
                  <fieldset class="advanced-parameters">
                    <legend>高级参数默认值</legend>
                    <label
                      ><span class="field-title"
                        >思考模式
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.thinking"
                          aria-label="思考模式说明"
                          >?</span
                        ></span
                      ><select v-model="provider.parameters.thinking_type">
                        <option value="">由调用请求决定</option>
                        <option value="disabled">关闭</option>
                        <option value="enabled">开启</option>
                      </select></label
                    >
                    <label
                      ><span class="field-title"
                        >推理强度
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.reasoning"
                          aria-label="推理强度说明"
                          >?</span
                        ></span
                      ><select v-model="provider.parameters.reasoning_effort">
                        <option value="">由调用请求决定</option>
                        <option value="none">none</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                        <option value="max">max</option>
                      </select></label
                    >
                    <label
                      ><span class="field-title"
                        >最大输出 Token
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.maxTokens"
                          aria-label="最大输出 Token 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="provider.parameters.max_tokens"
                        type="number"
                        min="1"
                        max="131072"
                        placeholder="由调用请求决定"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Temperature
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.temperature"
                          aria-label="Temperature 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="provider.parameters.temperature"
                        type="number"
                        min="0"
                        max="2"
                        step="0.1"
                        placeholder="由调用请求决定"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Top P
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.topP"
                          aria-label="Top P 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="provider.parameters.top_p"
                        type="number"
                        min="0.01"
                        max="1"
                        step="0.01"
                        placeholder="由调用请求决定"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Presence penalty
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.presence"
                          aria-label="Presence penalty 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="provider.parameters.presence_penalty"
                        type="number"
                        min="-2"
                        max="2"
                        step="0.1"
                        placeholder="由调用请求决定"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Frequency penalty
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.frequency"
                          aria-label="Frequency penalty 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="provider.parameters.frequency_penalty"
                        type="number"
                        min="-2"
                        max="2"
                        step="0.1"
                        placeholder="由调用请求决定"
                    /></label>
                  </fieldset>
                </details>
                <div class="form-actions">
                  <label
                    ><input
                      v-model="provider.enabled"
                      type="checkbox"
                    />启用</label
                  >
                  <button :disabled="busy">保存供应商</button>
                  <button
                    type="button"
                    class="secondary"
                    @click="closeProviderEditor"
                  >
                    取消编辑
                  </button>
                </div>
              </form>
            </section>
          </dialog>
          <div class="card-grid">
            <WorkDrawer
              :error="error"
              v-for="p in filterRows(providers, ['name', 'base_url'])"
              :key="p.id"
              :title="p.name"
              :summary="p.base_url"
              :status="p.enabled ? '启用' : '停用'"
              @close="editingProviderId === p.id && cancelProviderInline()"
            >
              <form
                v-if="editingProviderId === p.id"
                class="inline-detail-form"
                @submit.prevent="action(saveProvider)"
              >
                <div class="detail-heading">
                  <div>
                    <small>供应商 API</small>
                    <h2>编辑 {{ p.name }}</h2>
                  </div>
                  <span class="badge">编辑中</span>
                </div>
                <div class="detail-grid">
                  <label
                    >名称<input
                      v-model="provider.name"
                      required
                      maxlength="100"
                  /></label>
                  <label
                    >超时秒数<input
                      v-model.number="provider.timeout_seconds"
                      type="number"
                      min="5"
                      max="300"
                      required
                  /></label>
                  <label class="full"
                    >API base URL（HTTP / HTTPS）<input
                      v-model="provider.base_url"
                      required
                  /></label>
                  <label class="full"
                    >上游 API Key<input
                      v-model="provider.api_key"
                      type="password"
                      autocomplete="new-password"
                      placeholder="留空保留现有 Key"
                  /></label>
                  <label
                    ><span class="field-title"
                      >思考模式
                      <span
                        class="help-tip"
                        tabindex="0"
                        :data-tip="parameterHelp.thinking"
                        >?</span
                      ></span
                    ><select v-model="provider.parameters.thinking_type">
                      <option value="">由请求决定</option>
                      <option value="disabled">关闭</option>
                      <option value="enabled">开启</option>
                    </select></label
                  >
                  <label
                    ><span class="field-title"
                      >推理强度
                      <span
                        class="help-tip"
                        tabindex="0"
                        :data-tip="parameterHelp.reasoning"
                        >?</span
                      ></span
                    ><select v-model="provider.parameters.reasoning_effort">
                      <option value="">由请求决定</option>
                      <option
                        v-for="value in [
                          'none',
                          'low',
                          'medium',
                          'high',
                          'max',
                        ]"
                        :value="value"
                      >
                        {{ value }}
                      </option>
                    </select></label
                  >
                  <label
                    v-for="field in [
                      ['max_tokens', '最大输出 Token'],
                      ['temperature', 'Temperature'],
                      ['top_p', 'Top P'],
                      ['presence_penalty', 'Presence penalty'],
                      ['frequency_penalty', 'Frequency penalty'],
                    ]"
                    :key="field[0]"
                    ><span>{{ field[1] }}</span
                    ><input
                      v-model.number="provider.parameters[field[0]]"
                      type="number"
                      placeholder="由请求决定"
                  /></label>
                </div>
                <div class="form-actions">
                  <label
                    ><input
                      v-model="provider.enabled"
                      type="checkbox"
                    />启用</label
                  ><button :disabled="busy">保存修改</button
                  ><button
                    type="button"
                    class="secondary"
                    @click="cancelProviderInline"
                  >
                    取消
                  </button>
                </div>
              </form>
              <template v-else>
                <div class="detail-heading">
                  <div>
                    <small>供应商 API</small>
                    <h2>{{ p.name }}</h2>
                  </div>
                  <span
                    :class="['badge', p.enabled ? 'succeeded' : 'disabled']"
                    >{{ p.enabled ? "已启用" : "已停用" }}</span
                  >
                </div>
                <dl class="detail-list">
                  <div>
                    <dt>API base URL</dt>
                    <dd>{{ p.base_url }}</dd>
                  </div>
                  <div>
                    <dt>请求超时</dt>
                    <dd>{{ p.timeout_seconds }} 秒</dd>
                  </div>
                  <div>
                    <dt>上游 API Key</dt>
                    <dd>已加密保存（不可回显）</dd>
                  </div>
                  <div>
                    <dt>默认高级参数</dt>
                    <dd>{{ parameterSummary(p.parameter_defaults) }}</dd>
                  </div>
                </dl>
                <section class="detail-section">
                  <h3>高级参数完整值</h3>
                  <pre>{{
                    JSON.stringify(p.parameter_defaults || {}, null, 2)
                  }}</pre>
                </section>
                <div class="card-actions">
                  <button @click="editProviderInline(p)">编辑</button>
                  <button
                    class="secondary"
                    :disabled="busy || !p.enabled"
                    @click="
                      action(async () => {
                        const r = await api(
                          'providers/' + p.id + '/test',
                          'POST',
                        );
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
                </div>
              </template>
            </WorkDrawer>
          </div></template
        >
        <template v-if="tab === 'routes'"
          ><dialog ref="routeDrawer" class="side-drawer" @cancel="resetRoute">
            <section class="drawer-sheet">
              <header class="drawer-header">
                <div>
                  <small>模型路由</small>
                  <h2>{{ route.id ? "编辑模型路由" : "新增模型路由" }}</h2>
                </div>
                <button
                  type="button"
                  class="icon-button"
                  aria-label="关闭"
                  @click="closeRouteEditor"
                >
                  ×
                </button>
              </header>
              <form id="route-editor" @submit.prevent="action(saveRoute)">
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
                    maxlength="200"
                /></label>
                <details class="advanced-panel">
                  <summary>
                    <span>路由强制参数 <small>可选</small></span>
                    <small>{{
                      parameterSummary(parametersToApi(route.parameters))
                    }}</small>
                  </summary>
                  <div class="parameter-toolbar">
                    <p>优先级最高。Knowledge 专用路由建议使用推荐值。</p>
                    <button
                      type="button"
                      class="secondary"
                      @click="applyKnowledgeParameters(route.parameters)"
                    >
                      应用 Knowledge 提炼推荐值
                    </button>
                    <button
                      type="button"
                      class="text-button"
                      @click="clearParameters(route.parameters)"
                    >
                      清空
                    </button>
                  </div>
                  <fieldset class="advanced-parameters">
                    <legend>路由强制参数</legend>
                    <label
                      ><span class="field-title"
                        >思考模式
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.thinking"
                          aria-label="思考模式说明"
                          >?</span
                        ></span
                      ><select v-model="route.parameters.thinking_type">
                        <option value="">不强制</option>
                        <option value="disabled">关闭</option>
                        <option value="enabled">开启</option>
                      </select></label
                    >
                    <label
                      ><span class="field-title"
                        >推理强度
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.reasoning"
                          aria-label="推理强度说明"
                          >?</span
                        ></span
                      ><select v-model="route.parameters.reasoning_effort">
                        <option value="">不强制</option>
                        <option value="none">none</option>
                        <option value="low">low</option>
                        <option value="medium">medium</option>
                        <option value="high">high</option>
                        <option value="max">max</option>
                      </select></label
                    >
                    <label
                      ><span class="field-title"
                        >最大输出 Token
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.maxTokens"
                          aria-label="最大输出 Token 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="route.parameters.max_tokens"
                        type="number"
                        min="1"
                        max="131072"
                        placeholder="不强制"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Temperature
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.temperature"
                          aria-label="Temperature 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="route.parameters.temperature"
                        type="number"
                        min="0"
                        max="2"
                        step="0.1"
                        placeholder="不强制"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Top P
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.topP"
                          aria-label="Top P 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="route.parameters.top_p"
                        type="number"
                        min="0.01"
                        max="1"
                        step="0.01"
                        placeholder="不强制"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Presence penalty
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.presence"
                          aria-label="Presence penalty 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="route.parameters.presence_penalty"
                        type="number"
                        min="-2"
                        max="2"
                        step="0.1"
                        placeholder="不强制"
                    /></label>
                    <label
                      ><span class="field-title"
                        >Frequency penalty
                        <span
                          class="help-tip"
                          tabindex="0"
                          :data-tip="parameterHelp.frequency"
                          aria-label="Frequency penalty 说明"
                          >?</span
                        ></span
                      ><input
                        v-model.number="route.parameters.frequency_penalty"
                        type="number"
                        min="-2"
                        max="2"
                        step="0.1"
                        placeholder="不强制"
                    /></label>
                  </fieldset>
                </details>
                <div class="form-actions">
                  <label
                    ><input
                      v-model="route.enabled"
                      type="checkbox"
                    />启用</label
                  >
                  <button :disabled="busy">保存路由</button>
                  <button
                    type="button"
                    class="secondary"
                    @click="closeRouteEditor"
                  >
                    取消编辑
                  </button>
                </div>
              </form>
            </section>
          </dialog>
          <div class="card-grid">
            <WorkDrawer
              :error="error"
              v-for="r in filterRows(routes, ['alias', 'upstream_model'])"
              :key="r.id"
              :title="r.alias"
              :summary="r.upstream_model"
              :status="r.enabled ? '启用' : '停用'"
            >
              <h2>{{ r.alias }} · {{ r.enabled ? "启用" : "停用" }}</h2>
              <p>
                {{ providers.find((p) => p.id === r.provider_id)?.name }} /
                {{ r.upstream_model }}
              </p>
              <p
                v-if="Object.keys(r.parameter_overrides || {}).length"
                class="parameter-summary"
              >
                <strong>强制参数</strong>
                {{ parameterSummary(r.parameter_overrides) }}
              </p>
              <div class="card-actions">
                <button @click="openRouteEditor(r)">编辑</button>
              </div>
            </WorkDrawer>
          </div></template
        >
        <template v-if="tab === 'keys'"
          ><WorkDrawer
            :error="error"
            create
            title="签发用户模型 Key"
            @close="freshKey = ''"
          >
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
          </WorkDrawer>
          <WorkDrawer
            :error="error"
            v-for="k in filterRows(keys, ['owner', 'prefix'])"
            :key="k.id"
            :title="k.owner"
            :summary="k.prefix + '… · ' + k.rpm + ' RPM'"
            :status="k.revoked ? '已撤销' : '有效'"
          >
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
          </WorkDrawer></template
        >
        <template v-if="tab === 'calls'"
          ><WorkDrawer create title="分类统计与请求查询" :error="error" wide>
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
              新调用可查看经过敏感信息过滤的输入输出快照，仅管理员可访问，默认保留
              7 天。历史、关闭采集或已过期的正文不可查看。 Token “未知”不是
              0，不可直接当作计费账本。接口成功不代表 AI 提炼通过校验。running
              表示尚无完成记录，进程异常退出也可能留下此状态。
            </p>
            <label
              >当前页分类统计
              <select v-model="groupBy">
                <option value="model">按模型</option>
                <option value="owner">按调用方</option>
                <option value="provider_id">按供应商</option>
                <option value="status">按状态</option>
              </select>
            </label>
            <div class="usage-table">
              <table>
                <thead>
                  <tr>
                    <th>分类</th>
                    <th>请求 / 接口成功</th>
                    <th>已知输入 Token</th>
                    <th>已知输出 Token</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="g in callGroups" :key="g.label">
                    <td>{{ g.label }}</td>
                    <td>{{ g.count }} / {{ g.success }}</td>
                    <td>
                      {{ g.input
                      }}<small v-if="g.unknownInput"
                        >（{{ g.unknownInput }} 条未知）</small
                      >
                    </td>
                    <td>
                      {{ g.output
                      }}<small v-if="g.unknownOutput"
                        >（{{ g.unknownOutput }} 条未知）</small
                      >
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <form
              @submit.prevent="action(() => inspectCall(requestLookup.trim()))"
              class="filter-bar"
            >
              <input
                v-model="requestLookup"
                placeholder="输入 Knowledge 日志中的请求 ID"
                aria-label="请求 ID"
                required
              />
              <button :disabled="busy">查询调用详情</button>
            </form>
          </WorkDrawer>
          <WorkDrawer
            :error="error"
            v-for="c in calls"
            :key="c.id"
            :title="'请求 ' + c.id"
            :summary="
              c.model +
              ' · ' +
              c.owner +
              ' · ' +
              (c.duration_ms == null ? '耗时未知' : c.duration_ms + ' ms') +
              ' · 输入 ' +
              (c.input_tokens ?? '未知') +
              ' / 输出 ' +
              (c.output_tokens ?? '未知')
            "
            :status="c.status"
            @open="action(() => inspectCall(c.id, false))"
          >
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
            <p v-if="busy && callDetail?.call?.id !== c.id" role="status">
              正在加载请求快照…
            </p>
            <template v-if="callDetail?.call?.id === c.id">
              <section class="detail-section">
                <h3>响应概要</h3>
                <p v-if="callDetail.detail">
                  结束原因：{{
                    callDetail.detail.finish_reasons.join(", ") || "未返回"
                  }}
                  ·
                  {{ callDetail.detail.stream ? "流式（合并输出）" : "非流式" }}
                </p>
                <p
                  v-if="callDetail.detail?.finish_reasons.includes('length')"
                  class="error"
                  role="alert"
                >
                  输出达到模型长度上限，结果可能不完整。
                </p>
                <p v-if="callDetail.detail">
                  脱敏快照保留至
                  {{
                    new Date(callDetail.expires_at * 1000).toLocaleString()
                  }}。
                </p>
              </section>
              <div v-if="callDetail.detail" class="call-panes">
                <section v-for="side in ['input', 'output']" :key="side">
                  <h3>
                    {{ side === "input" ? "请求输入" : "模型输出" }}
                    <small v-if="callDetail.detail[side].truncated"
                      >（快照已截短）</small
                    >
                  </h3>
                  <pre>{{ callDetail.detail[side].text }}</pre>
                </section>
              </div>
              <p v-else>
                没有可用正文：可能是历史调用、未启用采集、已过期或快照保存失败。
              </p>
            </template>
          </WorkDrawer></template
        >
        <dialog ref="callDialog" class="call-dialog work-drawer wide">
          <template v-if="callDetail">
            <header>
              <h2>调用详情</h2>
              <button class="secondary" @click="callDialog.close()">
                关闭
              </button>
            </header>
            <p>
              请求 ID：{{ callDetail.call.id }} · {{ callDetail.call.status }} ·
              HTTP {{ callDetail.call.http_status ?? "未知" }}
            </p>
            <p>
              输入 {{ callDetail.call.input_tokens ?? "未知" }} / 输出
              {{ callDetail.call.output_tokens ?? "未知" }} Token ·
              {{ callDetail.call.duration_ms ?? "未知" }} ms
            </p>
            <p v-if="callDetail.call.error_code">
              错误码：{{ callDetail.call.error_code }}
            </p>
            <template v-if="callDetail.detail">
              <p>
                结束原因：{{
                  callDetail.detail.finish_reasons.join(", ") || "未返回"
                }}
                · {{ callDetail.detail.stream ? "流式（合并输出）" : "非流式" }}
              </p>
              <p
                v-if="callDetail.detail.finish_reasons.includes('length')"
                role="alert"
              >
                输出达到模型长度上限，可能被截断；HTTP 200
                不表示结果完整。请检查输出预算和提示词长度。
              </p>
              <p>
                快照经过自动过滤（不保证覆盖所有业务敏感信息），加密保存至
                {{
                  new Date(callDetail.expires_at * 1000).toLocaleString()
                }}。仅用于排障，请勿对外分享。
              </p>
              <div class="call-panes">
                <section v-for="side in ['input', 'output']" :key="side">
                  <h3>
                    {{ side === "input" ? "请求输入" : "模型输出" }}
                    <small v-if="callDetail.detail[side].truncated"
                      >（快照超过 64 KiB，展示已截短）</small
                    >
                  </h3>
                  <pre>{{ callDetail.detail[side].text }}</pre>
                </section>
              </div>
            </template>
            <p v-else>
              没有可用正文：可能是历史调用、未启用采集、已过期，或快照保存失败。请结合服务端日志排查。
            </p>
          </template>
        </dialog>
        <p
          v-if="tab === 'calls' ? !calls.length : !currentRows.length"
          class="empty-state"
        >
          暂无匹配记录，请调整筛选或新增记录。
        </p>
      </div>
      <footer v-if="tab === 'calls'" class="list-pagination">
        <span>第 {{ offset / 100 + 1 }} 页 · 本页 {{ calls.length }} 条</span>
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
      </footer>
      <footer v-if="tab !== 'calls'" class="list-pagination">
        <span>{{ currentRows.length }} 条 · 第 {{ localPage + 1 }} 页</span
        ><button
          class="secondary"
          :disabled="localPage === 0"
          @click="localPage--"
        >
          上一页</button
        ><button
          class="secondary"
          :disabled="(localPage + 1) * 20 >= currentRows.length"
          @click="localPage++"
        >
          下一页
        </button>
      </footer>
    </section>
  </div>
</template>
