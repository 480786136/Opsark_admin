<script setup>
import { computed, nextTick, onMounted, ref, watch } from "vue";
const props = defineProps({
  api: { type: Function, required: true },
  initialUserId: { type: String, default: "" },
});
const emit = defineEmits(["monitor"]);
const section = ref("users"),
  callSummary = ref({});
const users = ref([]),
  routes = ref([]),
  providers = ref([]),
  pending = ref([]),
  entries = ref([]);
const policy = ref({
  enabled: false,
  initial_tokens: 0,
  allowed_models: [],
  revision: 0,
});
const savedAllowedModels = ref([]);
const hasLegacyReservations = computed(() => users.value.some(user => user.reserved > 0));
const hasLegacyLedger = computed(() => entries.value.some(item => item.reserved_delta !== 0 && item.reserved_delta != null));
const selected = ref(null),
  delta = ref(0),
  reason = ref("");
const detailElement = ref(null);
const error = ref(""),
  notice = ref(""),
  busy = ref(false);
const query = ref(""),
  userStatus = ref("all"),
  page = ref(1),
  pageSize = ref(20),
  total = ref(0);
let appliedQuery = "",
  appliedStatus = "all";
const detailTab = ref("credits"),
  sessions = ref([]),
  sessionPage = ref(1),
  sessionTotal = ref(0);
const sessionStatus = ref("all"),
  revocableCount = ref(0);
const securityDialog = ref(null),
  securityAction = ref(null),
  securityReason = ref("");
const pages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
);
const sessionPages = computed(() =>
  Math.max(1, Math.ceil(sessionTotal.value / 20)),
);
const stateLabels = {
  active: "有效",
  refresh_required: "可刷新",
  revoked: "已撤销",
  expired: "已过期",
  blocked: "账号已封禁",
};
const securityTitle = computed(
  () =>
    ({
      ban: "封禁用户",
      unban: "解封用户",
      revoke: "撤销会话",
      all: "撤销全部会话",
    })[securityAction.value?.kind] || "安全操作",
);
const visibleCount = computed(
  () =>
    routes.value.filter(
      (r) => policy.value.allowed_models.includes(r.alias) && available(r),
    ).length,
);
function available(route) {
  return (
    route.enabled &&
    providers.value.some((p) => p.id === route.provider_id && p.enabled)
  );
}
function routeStatus(route) {
  if (!route.enabled) return "路由已停用 · 不展示";
  if (!providers.value.some((p) => p.id === route.provider_id && p.enabled))
    return "供应商已停用 · 不展示";
  const checked = policy.value.allowed_models.includes(route.alias);
  const saved = savedAllowedModels.value.includes(route.alias);
  if (checked !== saved)
    return checked ? "待开放 · 保存生效" : "待关闭 · 保存生效";
  return saved ? "已向 Core 开放" : "不向 Core 开放";
}
function date(value) {
  return new Date(value * 1000).toLocaleString();
}
let adjustmentKey = "";
async function run(fn) {
  if (busy.value) return;
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
  await Promise.all([loadUsers(), loadPolicy(), loadPending()]);
}
async function loadPolicy() {
  [policy.value, routes.value, providers.value] = await Promise.all(
    ["registration-policy", "routes", "providers"].map((p) => props.api(p)),
  );
  savedAllowedModels.value = [...policy.value.allowed_models];
}
async function loadPending() {
  pending.value = await props.api("credit-reservations");
}
async function loadUsers() {
  const params = new URLSearchParams({
    q: appliedQuery,
    status: appliedStatus,
    page: String(page.value),
    page_size: String(pageSize.value),
  });
  const result = await props.api(`users?${params}`);
  users.value = result.items;
  total.value = result.total;
  if (page.value > pages.value) {
    page.value = pages.value;
    await loadUsers();
  }
}
async function search() {
  appliedQuery = query.value.trim();
  appliedStatus = userStatus.value;
  page.value = 1;
  selected.value = null;
  await loadUsers();
}
async function changePage(value) {
  page.value = value;
  selected.value = null;
  await loadUsers();
}
async function loadSessions() {
  if (!selected.value) return;
  const result = await props.api(
    `users/${selected.value.id}/sessions?page=${sessionPage.value}&page_size=20&status=${sessionStatus.value}`,
  );
  sessions.value = result.items;
  sessionTotal.value = result.total;
  revocableCount.value = result.revocable_count;
  if (sessionPage.value > sessionPages.value) {
    sessionPage.value = sessionPages.value;
    await loadSessions();
  }
}
async function refreshSelected() {
  if (!selected.value) return;
  selected.value = await props.api(`users/${selected.value.id}`);
  await Promise.all([
    loadSessions(),
    props
      .api(`call-monitor/requests?user_id=${selected.value.id}&page_size=1`)
      .then((data) => {
        callSummary.value = data.summary;
      }),
    props.api(`users/${selected.value.id}/ledger`).then((data) => {
      entries.value = data;
    }),
  ]);
}
async function open(user) {
  callSummary.value = {};
  selected.value = { ...user };
  delta.value = 0;
  reason.value = "";
  adjustmentKey = "";
  entries.value = [];
  sessions.value = [];
  sessionPage.value = 1;
  sessionStatus.value = "all";
  detailTab.value = "credits";
  await refreshSelected();
  await nextTick();
  detailElement.value?.scrollIntoView({ block: "start", behavior: "instant" });
}
async function savePolicy() {
  policy.value = await props.api("registration-policy", "PUT", policy.value);
  savedAllowedModels.value = [...policy.value.allowed_models];
  notice.value = "已保存。新额度仅用于后续注册；官方模型范围对所有用户生效。";
}
async function adjust() {
  if (
    !selected.value ||
    !Number.isSafeInteger(delta.value) ||
    !delta.value ||
    !reason.value.trim()
  ) {
    throw new Error("请填写非零整数和调整原因");
  }
  adjustmentKey ||= crypto.randomUUID();
  const updated = await props.api(
    `users/${selected.value.id}/adjustments`,
    "POST",
    {
      delta: delta.value,
      reason: reason.value,
      idempotency_key: adjustmentKey,
    },
  );
  Object.assign(selected.value, updated);
  entries.value = await props.api(`users/${selected.value.id}/ledger`);
  adjustmentKey = "";
  delta.value = 0;
  reason.value = "";
  await Promise.all([loadUsers(), loadPending()]);
  notice.value = "额度已调整并记入账本。";
}
async function confirmSecurity(kind, item = null) {
  if (busy.value || !selected.value) return;
  securityAction.value = {
    kind,
    userId: selected.value.id,
    email: selected.value.email,
    expectedDisabled: selected.value.disabled,
    sessionId: item?.id,
  };
  securityReason.value = "";
  error.value = "";
  notice.value = "";
  await nextTick();
  securityDialog.value.showModal();
}
function closeSecurity() {
  securityDialog.value.close();
  securityAction.value = null;
}
async function applySecurity() {
  const action = securityAction.value;
  if (!action || !securityReason.value.trim())
    throw new Error("请填写操作原因");
  const body = { reason: securityReason.value.trim() };
  if (action.kind === "ban" || action.kind === "unban") {
    await props.api(`users/${action.userId}/status`, "PUT", {
      ...body,
      disabled: action.kind === "ban",
      expected_disabled: action.expectedDisabled,
    });
  } else {
    const path =
      action.kind === "all" ? "revoke-all" : `${action.sessionId}/revoke`;
    await props.api(`users/${action.userId}/sessions/${path}`, "POST", body);
  }
  const title = securityTitle.value;
  closeSecurity();
  notice.value = `${title}已生效并记录审计。`;
  await Promise.all([refreshSelected(), loadUsers()]);
}
onMounted(() =>
  run(async () => {
    await refresh();
    if (props.initialUserId)
      await open(
        await props.api(`users/${encodeURIComponent(props.initialUserId)}`),
      );
  }),
);
watch(
  () => props.initialUserId,
  (id) => {
    if (!id) selected.value = null;
    else
      run(async () => open(await props.api(`users/${encodeURIComponent(id)}`)));
  },
);
</script>

<template>
  <section class="user-panel management-panel" :aria-busy="busy">
    <div v-if="!selected" class="subnav" aria-label="用户管理视图">
      <button
        :disabled="busy"
        :aria-pressed="section === 'users'"
        @click="section = 'users'"
      >
        用户列表
      </button>
      <button
        :disabled="busy"
        :aria-pressed="section === 'policy'"
        @click="section = 'policy'"
      >
        注册与模型策略
      </button>
      <button
        :disabled="busy"
        :aria-pressed="section === 'pending'"
        @click="section = 'pending'"
      >
        待核对调用
      </button>
    </div>
    <div v-else class="detail-breadcrumb">
      <button
        class="text-link"
        :disabled="busy"
        @click="
          selected = null;
          section = 'users';
        "
      >
        返回用户列表</button
      ><span>/ 用户详情 / {{ selected.email }}</span>
    </div>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <form
      v-show="section === 'policy' && !selected"
      class="policy-form"
      @submit.prevent="run(savePolicy)"
    >
      <h2>注册与体验额度</h2>
      <p>
        内测注册默认关闭。额度按输入 + 输出 Token
        计量；公开注册前需完成邮箱验证及找回流程。
      </p>
      <fieldset :disabled="busy">
        <label class="check-label"
          ><input v-model="policy.enabled" type="checkbox" /> 开放注册</label
        >
        <label
          >新注册赠送 Token
          <input
            v-model.number="policy.initial_tokens"
            type="number"
            min="0"
            max="1000000000"
            required
        /></label>
      </fieldset>
      <h3>向 Core 用户开放的官方模型</h3>
      <p>
        勾选后保存：未登录用户可以查看，登录后才能调用。取消勾选将同时移出公开目录和后续调用许可；不会影响独立的平台模型
        Key。
      </p>
      <fieldset :disabled="busy" class="model-scope">
        <legend class="sr-only">官方模型范围</legend>
        <div v-if="routes.length" class="user-table">
          <table>
            <thead>
              <tr>
                <th>开放</th>
                <th>Core 显示名称</th>
                <th>模型标识</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="route in routes" :key="route.id">
                <td>
                  <input
                    v-model="policy.allowed_models"
                    type="checkbox"
                    :value="route.alias"
                    :aria-label="`向 Core 开放 ${route.display_name} (${route.alias})`"
                  />
                </td>
                <td>{{ route.display_name || route.alias }}</td>
                <td>
                  <code>{{ route.alias }}</code>
                </td>
                <td>
                  <span :class="{ muted: !available(route) }">{{
                    routeStatus(route)
                  }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-else class="empty-message">
          暂无模型路由。请先在“模型路由”中配置模型及 Core 显示名称。
        </p>
      </fieldset>
      <p class="muted">
        当前选择 {{ policy.allowed_models.length }} 个，其中
        {{ visibleCount }} 个满足路由和供应商启用条件。未选择模型时，Core
        官方模型目录为空。Core 在下次进入模型页时更新缓存。
      </p>
      <div class="actions">
        <button type="submit" :disabled="busy">保存策略</button>
        <button
          type="button"
          class="secondary"
          :disabled="busy"
          @click="run(loadPolicy)"
        >
          重新加载策略
        </button>
      </div>
    </form>
    <template v-if="section === 'users' && !selected">
      <div class="section-heading">
        <h2>用户管理</h2>
        <span class="muted">共 {{ total }} 位用户</span>
      </div>
      <form class="search-form" role="search" @submit.prevent="run(search)">
        <fieldset :disabled="busy">
          <label class="search-query"
            >搜索用户<input
              v-model="query"
              type="search"
              placeholder="邮箱或用户 ID"
              maxlength="254"
          /></label>
          <label
            >账号状态<select v-model="userStatus" aria-label="账号状态">
              <option value="all">全部状态</option>
              <option value="active">正常</option>
              <option value="disabled">已封禁</option>
            </select></label
          >
          <button type="submit">搜索</button
          ><button
            type="button"
            class="secondary"
            @click="
              run(async () => {
                await loadUsers();
                await refreshSelected();
              })
            "
          >
            刷新
          </button>
        </fieldset>
      </form>
      <p v-if="busy" role="status" class="muted">正在处理，请稍候…</p>
      <p v-if="!busy && !users.length" class="empty-message">
        {{
          appliedQuery || appliedStatus !== "all"
            ? "没有匹配用户，请调整搜索条件。"
            : "暂无注册用户。开放注册后，新用户将在这里显示。"
        }}
      </p>
      <div class="user-table">
        <table v-if="users.length">
          <thead>
            <tr>
              <th>用户</th>
              <th>状态</th>
              <th>注册时间</th>
              <th>可用 Token</th>
              <th v-if="hasLegacyReservations">历史预留 Token</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="user in users"
              :key="user.id"
              :class="{ selected: selected?.id === user.id }"
            >
              <td>
                {{ user.email }}<small class="user-id">{{ user.id }}</small>
              </td>
              <td>
                <span
                  class="status-badge"
                  :class="{ blocked: user.disabled }"
                  >{{ user.disabled ? "已封禁" : "正常" }}</span
                >
              </td>
              <td>{{ date(user.created_at) }}</td>
              <td>{{ user.available.toLocaleString() }}</td>
              <td v-if="hasLegacyReservations">{{ user.reserved.toLocaleString() }}</td>
              <td>
                <button
                  class="secondary"
                  :disabled="busy"
                  @click="run(() => open(user))"
                >
                  管理用户
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <nav class="pagination" aria-label="用户分页">
        <label
          >每页<select
            v-model.number="pageSize"
            aria-label="每页用户数"
            :disabled="busy"
            @change="run(() => changePage(1))"
          >
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
          </select></label
        >
        <span>第 {{ page }} / {{ pages }} 页</span
        ><button
          class="secondary"
          :disabled="busy || page <= 1"
          @click="run(() => changePage(page - 1))"
        >
          上一页</button
        ><button
          class="secondary"
          :disabled="busy || page >= pages"
          @click="run(() => changePage(page + 1))"
        >
          下一页
        </button>
      </nav>
    </template>
    <section
      v-if="selected"
      ref="detailElement"
      class="user-detail"
      aria-label="用户详情"
    >
      <div class="section-heading">
        <div>
          <h3>{{ selected.email }}</h3>
          <code class="muted">{{ selected.id }}</code>
        </div>
        <div class="actions">
          <span class="status-badge" :class="{ blocked: selected.disabled }">{{
            selected.disabled ? "已封禁" : "正常"
          }}</span>
          <button
            class="secondary"
            :disabled="busy"
            @click="confirmSecurity(selected.disabled ? 'unban' : 'ban')"
          >
            {{ selected.disabled ? "解封用户" : "封禁用户" }}
          </button>
          <button
            class="secondary"
            :disabled="busy"
            @click="run(refreshSelected)"
          >
            刷新用户详情
          </button>
          <button class="secondary" :disabled="busy" @click="selected = null">
            关闭详情
          </button>
        </div>
      </div>
      <div class="summary-strip">
        <div>
          <span>可用 Token</span>
          <strong>{{ selected.available.toLocaleString() }}</strong>
        </div>
        <div v-if="selected.reserved > 0">
          <span>历史预留 Token（待结算）</span>
          <strong>{{ selected.reserved.toLocaleString() }}</strong>
        </div>
        <div>
          <span>模型请求</span><strong>{{ callSummary.total ?? 0 }}</strong>
        </div>
        <div>
          <span>失败请求</span><strong>{{ callSummary.failed ?? 0 }}</strong>
        </div>
        <div>
          <span>账号状态</span
          ><strong>{{ selected.disabled ? "已封禁" : "正常" }}</strong>
        </div>
      </div>
      <div class="detail-tabs" aria-label="用户详情视图">
        <button
          class="secondary"
          :aria-pressed="detailTab === 'credits'"
          @click="detailTab = 'credits'"
        >
          额度与流水</button
        ><button
          class="secondary"
          :aria-pressed="detailTab === 'sessions'"
          @click="detailTab = 'sessions'"
        >
          会话管理
        </button>
        <button
          class="secondary"
          @click="emit('monitor', { user_id: selected.id })"
        >
          查看任务与调用
        </button>
      </div>
      <form
        v-if="detailTab === 'credits'"
        class="credit-form"
        @submit.prevent="run(adjust)"
      >
        <p>
          可用 {{ selected.available.toLocaleString() }} Token
          <span v-if="selected.reserved > 0"> · 历史预留 {{ selected.reserved.toLocaleString() }} Token（待结算）</span>
        </p>
        <p>正数增加、负数扣减可用额度。新调用按实际用量直接扣余额，不冻结额度。</p>
        <p v-if="selected.reserved > 0">该用户仍有旧计费策略产生的历史预留，请核对对应调用后结算；调整可用余额不会改变历史预留。</p>
        <fieldset :disabled="busy">
          <label
            >调整数量
            <input
              v-model.number="delta"
              type="number"
              min="-1000000000"
              max="1000000000"
              required
              @input="adjustmentKey = ''"
          /></label>
          <label
            >原因
            <input
              v-model="reason"
              maxlength="500"
              required
              @input="adjustmentKey = ''"
          /></label>
          <button type="submit">提交额度调整</button>
        </fieldset>
        <div class="user-table">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>可用变化</th>
                <th v-if="hasLegacyLedger">历史预留变化</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in entries" :key="item.id">
                <td>{{ date(item.created_at) }}</td>
                <td>{{ item.kind }}</td>
                <td>{{ item.delta }}</td>
                <td v-if="hasLegacyLedger">{{ item.reserved_delta }}</td>
                <td>{{ item.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted">显示最近 200 条流水。</p>
      </form>
      <section v-else aria-label="用户会话">
        <p>
          “可刷新”表示访问令牌已过期，但仍能刷新登录。撤销后访问令牌和刷新令牌均不可再用；已通过鉴权的在途请求可能继续完成。这里仅管理用户会话，不影响管理员登录。
        </p>
        <div class="session-toolbar">
          <label
            >会话状态<select
              v-model="sessionStatus"
              aria-label="会话状态"
              :disabled="busy"
              @change="
                run(async () => {
                  sessionPage = 1;
                  await loadSessions();
                })
              "
            >
              <option value="all">全部会话</option>
              <option value="active">有效 / 可刷新</option>
              <option value="revoked">已撤销</option>
              <option value="expired">已过期</option>
            </select></label
          >
          <button class="secondary" :disabled="busy" @click="run(loadSessions)">
            刷新会话</button
          ><button
            class="secondary danger"
            :disabled="busy || !revocableCount"
            @click="confirmSecurity('all')"
          >
            撤销全部会话
          </button>
        </div>
        <div class="user-table">
          <table v-if="sessions.length">
            <thead>
              <tr>
                <th>会话 ID</th>
                <th>状态</th>
                <th>访问令牌到期</th>
                <th>刷新会话到期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in sessions" :key="item.id">
                <td>
                  <code>{{ item.id }}</code>
                </td>
                <td>{{ stateLabels[item.status] }}</td>
                <td>{{ date(item.access_expires) }}</td>
                <td>{{ date(item.refresh_expires) }}</td>
                <td>
                  <button
                    class="secondary"
                    :disabled="busy || !item.can_revoke"
                    @click="confirmSecurity('revoke', item)"
                  >
                    撤销
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!busy && !sessions.length" class="empty-message">
          当前条件下没有会话。
        </p>
        <nav class="pagination" aria-label="会话分页">
          <span
            >共 {{ sessionTotal }} 条 · 第 {{ sessionPage }} /
            {{ sessionPages }} 页</span
          ><button
            class="secondary"
            :disabled="busy || sessionPage <= 1"
            @click="
              run(async () => {
                sessionPage--;
                await loadSessions();
              })
            "
          >
            上一页</button
          ><button
            class="secondary"
            :disabled="busy || sessionPage >= sessionPages"
            @click="
              run(async () => {
                sessionPage++;
                await loadSessions();
              })
            "
          >
            下一页
          </button>
        </nav>
      </section>
    </section>
    <dialog
      ref="securityDialog"
      class="security-dialog"
      aria-labelledby="security-title"
      @cancel="busy ? $event.preventDefault() : (securityAction = null)"
    >
      <form @submit.prevent="run(applySecurity)">
        <h2 id="security-title">{{ securityTitle }}</h2>
        <p>{{ securityAction?.email }}</p>
        <p v-if="securityAction?.kind === 'ban'">
          封禁将阻止登录和后续官方服务访问，并撤销该用户全部会话。用户额度、本地功能及自有模型不受影响。
        </p>
        <p v-else-if="securityAction?.kind === 'unban'">
          解封后用户可以重新登录。此前撤销的会话不会恢复，也不会重新发放注册额度。
        </p>
        <p v-else>
          撤销{{
            securityAction?.kind === "all" ? "该用户的全部会话" : "此会话"
          }}后，需要重新登录。账号仍可正常使用，额度不变。
        </p>
        <code v-if="securityAction?.sessionId" class="user-id">{{
          securityAction.sessionId
        }}</code>
        <p v-if="error" role="alert" class="error">{{ error }}</p>
        <fieldset :disabled="busy">
          <label
            >操作原因<textarea
              v-model="securityReason"
              rows="3"
              maxlength="500"
              required
              autofocus
              placeholder="记录处理依据，供后续审计"
            />
          </label>
          <div class="actions">
            <button type="submit" :disabled="!securityReason.trim()">
              确认{{ securityTitle }}</button
            ><button type="button" class="secondary" @click="closeSecurity">
              取消
            </button>
          </div>
        </fieldset>
      </form>
    </dialog>
    <section v-if="section === 'pending' && !selected">
      <div class="panel-toolbar">
        <h2>调用中与待核对用量</h2>
        <button class="secondary" :disabled="busy" @click="run(loadPending)">
          刷新待核对
        </button>
      </div>
      <p>
        新调用不预留额度。缺少用量或余额不足结算时保留待核对记录，可凭调用 ID
        核对供应商用量；余额不足时补充后再结算。历史预留仍按原规则处理。
      </p>
      <div v-for="item in pending" :key="item.id" class="reservation">
        <button class="text-link" @click="emit('monitor', { q: item.id })">
          {{ item.id }}
        </button>
        · {{ item.state }} · 实际用量 {{ item.actual ?? '待确认' }} Token
        <span v-if="item.amount"> · 历史预留 {{ item.amount }} Token</span>
      </div>
      <p v-if="!pending.length">没有待核对调用</p>
    </section>
  </section>
</template>

<style scoped>
.user-panel {
  max-width: 100%;
  font-variant-numeric: tabular-nums;
}
.policy-form,
.user-detail {
  margin: 12px 0;
  padding: 18px;
  border: 1px solid #d4dae3;
  border-radius: 10px;
  background: #fff;
}
.user-panel fieldset {
  display: flex;
  align-items: end;
  flex-wrap: wrap;
  gap: 16px;
  border: 0;
  padding: 0;
  min-width: 0;
}
.user-panel label {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.user-panel label.check-label {
  flex-direction: row;
  align-items: center;
  align-self: center;
}
.user-panel input[type="checkbox"] {
  width: 16px;
  height: 16px;
  min-height: 0;
  margin: 0;
  accent-color: #087f68;
}
.user-panel p {
  font-size: 13px;
  line-height: 1.7;
}
.user-panel h3 {
  margin: 24px 0 10px;
}
.user-panel .muted,
.user-id {
  color: #66758a;
}
.user-table {
  overflow: auto;
  width: 100%;
}
.user-table table {
  width: 100%;
  text-align: left;
  border-collapse: collapse;
  font-size: 13px;
}
.user-table th,
.user-table td {
  padding: 9px 10px;
  border-bottom: 1px solid #e3e8ed;
  white-space: nowrap;
}
.user-table th {
  color: #54647a;
  font-weight: 500;
  background: #f7f9fb;
}
.user-table td:last-child {
  white-space: normal;
}
.user-id {
  display: block;
  font-size: 11px;
  margin-top: 5px;
  overflow-wrap: anywhere;
}
.user-table tr.selected {
  background: #f0f8f5;
}
.actions,
.section-heading,
.session-toolbar,
.pagination {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.section-heading {
  justify-content: space-between;
}
.section-heading h2,
.section-heading h3 {
  margin: 0;
}
.search-form {
  margin: 16px 0;
}
.search-query {
  flex: 1;
  min-width: 220px;
}
.pagination {
  justify-content: flex-end;
  margin: 16px 0;
}
.pagination label {
  flex-direction: row;
  align-items: center;
}
.pagination button,
.pagination select {
  width: auto;
  margin: 0;
}
.status-badge {
  display: inline-block;
  background: #eaf5ef;
  color: #25644c;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
}
.status-badge.blocked {
  background: #fbeeea;
  color: #924735;
}
.detail-tabs {
  display: flex;
  gap: 8px;
  margin: 24px 0 16px;
  border-bottom: 1px solid #e3e8ed;
  padding-bottom: 12px;
}
.detail-tabs button[aria-pressed="true"] {
  background: #e8f4ef;
  color: #21604b;
  border-color: #80b09b;
}
.credit-form .user-table {
  margin-top: 20px;
}
.session-toolbar {
  align-items: end;
  margin: 18px 0;
}
.empty-message {
  padding: 20px;
  background: #f7f9fb;
  border-radius: 6px;
  color: #54647a;
}
.reservation {
  padding: 10px 0;
  overflow-wrap: anywhere;
}
.security-dialog {
  width: min(520px, calc(100vw - 32px));
  box-sizing: border-box;
  border: 1px solid #d4dae3;
  border-radius: 12px;
  padding: 28px;
}
.security-dialog::backdrop {
  background: rgb(24 38 50 / 45%);
}
.security-dialog fieldset,
.security-dialog label {
  width: 100%;
}
.security-dialog textarea {
  box-sizing: border-box;
  width: 100%;
  resize: vertical;
}
.user-panel button {
  transition:
    background-color 0.18s,
    border-color 0.18s;
}
.user-panel button:not(:disabled):hover {
  filter: brightness(0.96);
}
.user-panel button:not(:disabled):active {
  transform: translateY(1px);
}
.user-panel :is(button, input, select, textarea):focus-visible {
  outline: 2px solid #087f68;
  outline-offset: 3px;
}
.user-panel .danger {
  color: #924735;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}
.model-scope {
  margin: 16px 0;
}
@media (max-width: 700px) {
  .policy-form,
  .user-detail {
    padding: 16px;
  }
  .section-heading {
    align-items: flex-start;
  }
  .pagination {
    justify-content: flex-start;
  }
  .search-query {
    min-width: 100%;
  }
  .user-panel fieldset {
    gap: 12px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .user-panel button {
    transition: none;
  }
  .user-panel button:not(:disabled):active {
    transform: none;
  }
}
</style>
