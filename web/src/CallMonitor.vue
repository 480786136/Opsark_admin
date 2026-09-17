<script setup>
import { computed, reactive, ref, watch } from "vue";
import DetailDialog from "./DetailDialog.vue";
const props = defineProps({
  api: { type: Function, required: true },
  scope: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["user"]);
const mode = ref("requests"),
  page = ref(1),
  pageSize = ref(20),
  total = ref(0),
  items = ref([]),
  summary = ref({});
const busy = ref(false),
  error = ref(""),
  task = ref(null),
  lookup = ref("");
const filters = reactive({
  q: "",
  user_id: "",
  model: "",
  status: "all",
  association: "all",
  after: "",
  before: "",
});
let applied = {};
let generation = 0;
const detailOpen = ref(false),
  detailBusy = ref(false),
  detailError = ref(""),
  detail = ref(null),
  contentTab = ref("input");
let detailGeneration = 0;
const pages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize.value)),
);
const operationLabels = {
  plan: "计划生成",
  stage_decision: "阶段决策",
  requirement: "需求理解",
  model_check: "模型测试",
  summary: "执行总结",
  review: "结果复核",
  other: "其他调用",
};
const statusLabels = { running: "进行中", succeeded: "成功", failed: "失败" };
const number = (value) =>
  value == null ? "未知" : Number(value).toLocaleString();
const time = (value) => new Date(value * 1000).toLocaleString();
const owner = (item) =>
  item.user_email ||
  (item.user_id ? item.user_id : `模型 Key · ${item.owner || item.principal}`);
async function load() {
  const current = ++generation;
  busy.value = true;
  error.value = "";
  items.value = [];
  summary.value = {};
  total.value = 0;
  const params = task.value
    ? {
        task_id: task.value.task_id,
        principal_id: task.value.principal,
        order: "asc",
      }
    : { ...applied };
  const query = new URLSearchParams({
    ...params,
    page: page.value,
    page_size: pageSize.value,
  });
  try {
    const isTasks = mode.value === "tasks" && !task.value;
    const [result, stats] = await Promise.all([
      props.api(`call-monitor/${isTasks ? "tasks" : "requests"}?${query}`),
      isTasks
        ? props.api(
            `call-monitor/requests?${new URLSearchParams({ ...params, page_size: 1 })}`,
          )
        : null,
    ]);
    if (current !== generation) return;
    items.value = result.items;
    total.value = result.total;
    summary.value = (stats || result).summary;
    if (page.value > pages.value) {
      page.value = pages.value;
      await load();
    }
  } catch (e) {
    if (current === generation) {
      error.value = e.message;
      items.value = [];
    }
  } finally {
    if (current === generation) busy.value = false;
  }
}
function search() {
  applied = Object.fromEntries(
    Object.entries(filters).filter(
      ([key, value]) => value && !["after", "before"].includes(key),
    ),
  );
  for (const [field, key] of [
    ["after", "started_after"],
    ["before", "started_before"],
  ]) {
    if (filters[field])
      applied[key] = new Date(filters[field]).getTime() / 1000;
  }
  page.value = 1;
  void load();
}
function switchMode(value) {
  mode.value = value;
  page.value = 1;
  void load();
}
function openTask(item) {
  task.value = { ...item };
  page.value = 1;
  detailOpen.value = false;
  void load();
}
function back() {
  task.value = null;
  page.value = 1;
  void load();
}
function goUser(item) {
  detailOpen.value = false;
  emit("user", item.user_id);
}
async function inspect(id) {
  if (!id.trim()) return;
  const current = ++detailGeneration;
  detail.value = null;
  detailError.value = "";
  detailOpen.value = true;
  detailBusy.value = true;
  contentTab.value = "input";
  try {
    const result = await props.api(`calls/${encodeURIComponent(id.trim())}`);
    if (current === detailGeneration) detail.value = result;
  } catch (e) {
    if (current === detailGeneration) detailError.value = e.message;
  } finally {
    if (current === detailGeneration) detailBusy.value = false;
  }
}
watch(
  () => props.scope,
  (value) => {
    Object.assign(
      filters,
      {
        q: "",
        user_id: "",
        model: "",
        status: "all",
        association: "all",
        after: "",
        before: "",
      },
      value,
    );
    task.value = null;
    mode.value = value.user_id ? "tasks" : "requests";
    search();
  },
  { deep: true, immediate: true },
);
</script>
<template>
  <section class="management-panel call-monitor" :aria-busy="busy">
    <div v-if="task" class="detail-breadcrumb">
      <button class="text-link" @click="back">返回调用监控</button><span>/</span
      ><button v-if="task.user_id" class="text-link" @click="goUser(task)">
        {{ owner(task) }}</button
      ><span v-else>{{ owner(task) }}</span
      ><span>/ 任务详情</span>
    </div>
    <div class="panel-toolbar">
      <div>
        <h2>{{ task ? "任务调用记录" : "请求与任务" }}</h2>
        <p v-if="task">
          <code>{{ task.task_id }}</code>
        </p>
        <p v-else>从用户追踪任务，再定位每一次模型请求。</p>
      </div>
      <button class="secondary" :disabled="busy" @click="load">刷新记录</button>
    </div>
    <div v-if="!task" class="subnav" aria-label="监控视图">
      <button
        :aria-pressed="mode === 'requests'"
        @click="switchMode('requests')"
      >
        调用请求</button
      ><button :aria-pressed="mode === 'tasks'" @click="switchMode('tasks')">
        任务视图
      </button>
    </div>
    <form v-if="!task" class="compact-filter" @submit.prevent="search">
      <fieldset :disabled="busy">
        <label class="grow"
          >搜索请求<input
            v-model="filters.q"
            placeholder="用户邮箱、任务 ID、请求 ID、Core 请求 ID"
            maxlength="254"
        /></label>
        <label
          >用户 ID<input
            v-model="filters.user_id"
            maxlength="64"
            placeholder="全部用户 / Key"
        /></label>
        <label
          >模型<input
            v-model="filters.model"
            maxlength="128"
            placeholder="模型标识（精确）"
        /></label>
        <label
          >调用状态<select v-model="filters.status" aria-label="调用状态">
            <option value="all">全部</option>
            <option value="succeeded">成功</option>
            <option value="failed">失败</option>
            <option value="running">进行中</option>
          </select></label
        >
        <label
          >任务关联<select v-model="filters.association" aria-label="任务关联">
            <option value="all">全部</option>
            <option value="linked">已关联任务</option>
            <option value="unlinked">未关联任务</option>
          </select></label
        >
        <label
          >开始时间<input
            v-model="filters.after"
            type="datetime-local" /></label
        ><label
          >结束时间<input v-model="filters.before" type="datetime-local"
        /></label>
        <button>查询</button>
      </fieldset>
    </form>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="summary-strip" aria-label="筛选范围统计">
      <div>
        <span>请求总数</span><strong>{{ number(summary.total ?? 0) }}</strong>
      </div>
      <div>
        <span>成功 / 失败 / 进行中</span
        ><strong
          >{{ number(summary.succeeded ?? 0) }} /
          {{ number(summary.failed ?? 0) }} /
          {{ number(summary.running ?? 0) }}</strong
        >
      </div>
      <div>
        <span>已知输入 / 输出 Token</span
        ><strong
          >{{ number(summary.input_tokens ?? 0) }} /
          {{ number(summary.output_tokens ?? 0) }}</strong
        >
      </div>
      <div>
        <span>用量未知请求</span
        ><strong>{{ number(summary.unknown_usage ?? 0) }}</strong>
      </div>
    </div>
    <p class="table-note">
      {{
        task
          ? "按调用时间升序展示该用户/Key 的此任务全部请求。任务标识由客户端上报，不代表 Admin 已获知任务最终执行结果。"
          : "统计覆盖当前筛选范围，不仅是本页。任务视图仅包含已上报任务 ID 的请求；旧客户端和模型测试可能未关联任务。"
      }}
      未知用量不是 0；调用成功不等于任务成功。
    </p>
    <div class="data-grid">
      <table v-if="mode === 'tasks' && !task">
        <thead>
          <tr>
            <th>任务</th>
            <th>用户 / 调用方</th>
            <th>请求数</th>
            <th>成功 / 失败</th>
            <th>最近调用</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.principal + item.task_id">
            <td>
              <code>{{ item.task_id }}</code>
            </td>
            <td>
              <button
                v-if="item.user_id"
                class="text-link"
                @click="goUser(item)"
              >
                {{ owner(item) }}</button
              ><span v-else>{{ owner(item) }}</span>
            </td>
            <td>{{ item.total }}</td>
            <td>{{ item.succeeded }} / {{ item.failed }}</td>
            <td>{{ time(item.last_call_at) }}</td>
            <td>
              <button class="secondary" @click="openTask(item)">
                查看任务
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <table v-else>
        <thead>
          <tr>
            <th>{{ task ? "顺序 / 时间" : "时间 / 请求" }}</th>
            <th>用户 / 调用方</th>
            <th>所属任务 / 步骤</th>
            <th>类型 / 模型</th>
            <th>状态 / 耗时</th>
            <th>输入 / 输出 Token</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in items" :key="item.id">
            <td>
              <span v-if="task"
                >#{{ (page - 1) * pageSize + index + 1 }} · </span
              >{{ time(item.started_at)
              }}<small :title="item.id">{{ item.id.slice(0, 12) }}</small>
            </td>
            <td>
              <button
                v-if="item.user_id"
                class="text-link"
                @click="goUser(item)"
              >
                {{ owner(item) }}</button
              ><span v-else>{{ owner(item) }}</span>
            </td>
            <td>
              <button
                v-if="item.task_id"
                class="text-link id-link"
                :title="item.task_id"
                @click="openTask(item)"
              >
                {{ item.task_id }}</button
              ><span v-else class="muted">未关联任务</span
              ><small v-if="item.step_id">步骤 {{ item.step_id }}</small
              ><small v-else-if="item.round_id">轮次 {{ item.round_id }}</small>
            </td>
            <td>
              {{ operationLabels[item.operation] || "未上报类型"
              }}<small>{{ item.model }}</small>
            </td>
            <td>
              <span class="status-label" :data-status="item.status">{{
                statusLabels[item.status] || item.status
              }}</span
              ><small>{{ number(item.duration_ms) }} ms</small>
            </td>
            <td>
              {{ number(item.input_tokens) }} / {{ number(item.output_tokens) }}
            </td>
            <td>
              <button class="secondary" @click="inspect(item.id)">
                请求详情
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="busy" role="status">正在读取调用记录…</p>
    <p v-else-if="!items.length && !error" class="panel-empty">
      没有符合条件的{{
        mode === "tasks" && !task ? "关联任务" : "调用请求"
      }}。可调整筛选，或检查 Core 是否已升级并使用官方模型。
    </p>
    <nav class="panel-pagination" aria-label="调用分页">
      <span>共 {{ total }} 条 · 第 {{ page }} / {{ pages }} 页</span
      ><label
        >每页<select
          v-model.number="pageSize"
          aria-label="每页调用数"
          :disabled="busy"
          @change="
            page = 1;
            load();
          "
        >
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select></label
      ><button
        class="secondary"
        :disabled="busy || page <= 1"
        @click="
          page--;
          load();
        "
      >
        上一页</button
      ><button
        class="secondary"
        :disabled="busy || page >= pages"
        @click="
          page++;
          load();
        "
      >
        下一页
      </button>
    </nav>
    <form class="request-lookup" @submit.prevent="inspect(lookup)">
      <label
        >按 Admin 请求 ID 定位<input
          v-model="lookup"
          maxlength="128"
          required
          placeholder="完整请求 ID" /></label
      ><button class="secondary">打开请求</button>
    </form>
    <DetailDialog v-model:open="detailOpen" title="模型请求详情" wide>
      <p v-if="detailBusy" role="status">正在读取请求详情…</p>
      <p v-if="detailError" role="alert" class="error">{{ detailError }}</p>
      <template v-if="detail"
        ><dl class="metadata-grid">
          <div>
            <dt>Admin 请求 ID</dt>
            <dd>
              <code>{{ detail.call.id }}</code>
            </dd>
          </div>
          <div>
            <dt>Core 请求 ID</dt>
            <dd>
              <code>{{ detail.call.client_request_id || "未上报" }}</code>
            </dd>
          </div>
          <div>
            <dt>所属用户 / 调用方</dt>
            <dd>
              <button
                v-if="detail.call.user_id"
                class="text-link"
                @click="goUser(detail.call)"
              >
                {{ owner(detail.call) }}</button
              ><span v-else>{{ owner(detail.call) }}</span>
            </dd>
          </div>
          <div>
            <dt>所属任务</dt>
            <dd>
              <button
                v-if="detail.call.task_id"
                class="text-link"
                @click="openTask(detail.call)"
              >
                {{ detail.call.task_id }}</button
              ><span v-else>未关联任务</span>
            </dd>
          </div>
          <div>
            <dt>轮次 / 阶段索引 / 步骤</dt>
            <dd>
              {{ detail.call.round_id || "未上报" }} /
              {{ detail.call.phase_index ?? "未知" }} /
              {{ detail.call.step_id || "未上报" }}
            </dd>
          </div>
          <div>
            <dt>调用类型 / 模型 / 供应商</dt>
            <dd>
              {{ operationLabels[detail.call.operation] || "未上报" }} ·
              {{ detail.call.model }} ·
              {{ detail.call.provider_name || detail.call.provider_id }}
            </dd>
          </div>
          <div>
            <dt>时间 / 耗时</dt>
            <dd>
              {{ time(detail.call.started_at) }} ·
              {{ number(detail.call.duration_ms) }} ms
            </dd>
          </div>
          <div>
            <dt>状态 / HTTP / 错误码</dt>
            <dd>
              {{ statusLabels[detail.call.status] }} ·
              {{ detail.call.http_status ?? "未知" }} ·
              {{ detail.call.error_code || "无" }}
            </dd>
          </div>
          <div>
            <dt>输入 / 输出 Token</dt>
            <dd>
              {{ number(detail.call.input_tokens) }} /
              {{ number(detail.call.output_tokens) }}
            </dd>
          </div>
        </dl>
        <template v-if="detail.detail"
          ><p class="table-note">
            脱敏快照保留至 {{ time(detail.expires_at) }}。结束原因：{{
              detail.detail.finish_reasons?.join(", ") || "未返回"
            }}。仅供排障，自动过滤不能保证移除所有业务敏感信息。
          </p>
          <p
            v-if="detail.detail.finish_reasons?.includes('length')"
            class="error"
          >
            输出达到长度上限，响应可能不完整。
          </p>
          <div class="subnav">
            <button
              :aria-pressed="contentTab === 'input'"
              @click="contentTab = 'input'"
            >
              请求输入</button
            ><button
              :aria-pressed="contentTab === 'output'"
              @click="contentTab = 'output'"
            >
              模型输出
            </button>
          </div>
          <p v-if="detail.detail[contentTab]?.truncated">快照已截短</p>
          <pre class="payload-view">{{ detail.detail[contentTab]?.text }}</pre>
        </template>
        <p v-else class="panel-empty">
          没有可用正文：请求可能尚未完成、发生于启用采集之前、采集开关已关闭、保留期已到或快照保存失败。历史未采集的正文无法补回。
        </p>
      </template>
    </DetailDialog>
  </section>
</template>
