<script setup>
import { computed, onMounted, ref } from "vue";
import DetailDialog from "./DetailDialog.vue";
const emit = defineEmits(["user", "monitor"]);
const policyOpen = ref(false),
  releaseOpen = ref(false),
  feedbackOpen = ref(false),
  releaseDetailOpen = ref(false),
  releaseDetail = ref(null),
  feedbackTab = ref("overview");
const query = ref(""),
  ticketStatus = ref("all");
const filteredTickets = computed(() =>
  tickets.value.filter(
    (item) =>
      (ticketStatus.value === "all" || item.status === ticketStatus.value) &&
      `${item.id} ${item.user_id || ""}`.includes(query.value.trim()),
  ),
);
const props = defineProps({
  api: { type: Function, required: true },
  mode: { type: String, default: "cloud" },
});
const policy = ref({
  support_email: "zgkj@zgspace.cn",
  support_wechat: "zgkjkj",
  developer_name: "智明",
  support_url: "",
  min_cloud_version: "0.0.0",
  revision: 0,
});
const releases = ref([]),
  tickets = ref([]),
  selected = ref(null),
  reply = ref(""),
  status = ref("open");
const release = ref({
  platform: "macos",
  arch: "aarch64",
  version: "",
  notes: "",
  download_url: "",
  sha256: "",
});
const busy = ref(false),
  error = ref(""),
  notice = ref("");
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
  if (props.mode === "releases")
    releases.value = await props.api("client-releases");
  else
    [policy.value, tickets.value] = await Promise.all(
      ["client-policy", "feedback"].map((path) => props.api(path)),
    );
}
async function savePolicy() {
  policy.value = await props.api("client-policy", "PUT", policy.value);
  notice.value =
    "联系与兼容策略已保存。Core 下次启动或检查版本时会同步联系方式。";
}
async function publish() {
  await props.api("client-releases", "POST", release.value);
  await refresh();
  releaseOpen.value = false;
  notice.value = "已发布更新引导。客户端会检查平台/架构，用户手动下载安装。";
}
async function withdraw(item) {
  if (
    !window.confirm(
      `撤回 ${item.version} 的更新引导？不会删除安装包或覆盖用户的已安装版本。`,
    )
  )
    return;
  await props.api(`client-releases/${item.id}`, "DELETE");
  await refresh();
}
async function open(ticket) {
  selected.value = await props.api(`feedback/${ticket.id}`);
  status.value = selected.value.status;
  reply.value = "";
  feedbackTab.value = "overview";
  feedbackOpen.value = true;
}
function downloadLogs() {
  const url = URL.createObjectURL(
    new Blob([selected.value.content.task_logs], {
      type: "text/plain;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `feedback-${selected.value.id}-logs.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function respond() {
  selected.value = await props.api(`feedback/${selected.value.id}`, "PUT", {
    revision: selected.value.revision,
    status: status.value,
    message: reply.value,
  });
  reply.value = "";
  tickets.value = await props.api("feedback");
  notice.value = "回复与状态已保存。";
}
onMounted(() => run(refresh));
</script>
<template>
  <section class="cloud-panel management-panel">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <div class="panel-toolbar">
      <div>
        <h2>{{ mode === "cloud" ? "用户反馈工单" : "系统发布记录" }}</h2>
        <p>
          {{
            mode === "cloud"
              ? "列表查找工单，详情查看诊断和回复。"
              : "管理各平台发布记录，查看下载地址和校验信息。"
          }}
        </p>
      </div>
      <div class="toolbar-actions">
        <button class="secondary" :disabled="busy" @click="run(refresh)">
          刷新列表</button
        ><button
          v-if="mode === 'cloud'"
          :disabled="busy"
          @click="policyOpen = true"
        >
          联系方式与兼容策略</button
        ><button v-else :disabled="busy" @click="releaseOpen = true">
          发布系统版本
        </button>
      </div>
    </div>
    <template v-if="mode === 'cloud'">
      <form class="compact-filter" @submit.prevent>
        <fieldset>
          <label class="grow"
            >筛选工单<input
              v-model="query"
              placeholder="已加载工单 ID / 用户 ID" /></label
          ><label
            >处理状态<select v-model="ticketStatus" aria-label="处理状态">
              <option value="all">全部</option>
              <option value="open">待处理</option>
              <option value="in_progress">处理中</option>
              <option value="resolved">已解决</option>
            </select></label
          >
        </fieldset>
      </form>
      <p class="table-note">
        正文、图片与任务日志加密保存 30
        天；读取、回复留审计。匿名反馈需通过用户留下的联系方式跟进。
      </p>
      <div class="data-grid">
        <table>
          <thead>
            <tr>
              <th>工单 ID</th>
              <th>用户</th>
              <th>类型</th>
              <th>状态</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ticket in filteredTickets" :key="ticket.id">
              <td>
                <code>{{ ticket.id.slice(0, 12) }}</code>
              </td>
              <td>
                <button
                  v-if="ticket.user_id"
                  class="text-link"
                  @click="emit('user', ticket.user_id)"
                >
                  {{ ticket.user_id.slice(0, 12) }}</button
                ><span v-else>匿名反馈</span>
              </td>
              <td>{{ ticket.category }}</td>
              <td>
                {{
                  { open: "待处理", in_progress: "处理中", resolved: "已解决" }[
                    ticket.status
                  ]
                }}
              </td>
              <td>{{ new Date(ticket.created_at * 1000).toLocaleString() }}</td>
              <td>
                <button
                  class="secondary"
                  :disabled="busy"
                  @click="run(() => open(ticket))"
                >
                  查看工单
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!filteredTickets.length && !busy" class="panel-empty">
        没有匹配的反馈工单。
      </p>
    </template>
    <template v-else
      ><div class="data-grid">
        <table>
          <thead>
            <tr>
              <th>版本</th>
              <th>平台 / 架构</th>
              <th>发布状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in releases" :key="item.id">
              <td>{{ item.version }}</td>
              <td>{{ item.platform }} / {{ item.arch }}</td>
              <td>{{ item.enabled ? "已发布" : "已撤回" }}</td>
              <td>
                <button
                  class="secondary"
                  @click="
                    releaseDetail = item;
                    releaseDetailOpen = true;
                  "
                >
                  版本详情
                </button>
                <button
                  class="secondary"
                  :disabled="busy || !item.enabled"
                  @click="run(() => withdraw(item))"
                >
                  撤回引导
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!releases.length && !busy" class="panel-empty">
        尚未发布系统版本。
      </p></template
    >
    <p v-if="busy" role="status">正在处理…</p>
    <DetailDialog v-model:open="policyOpen" title="联系与兼容设置" :busy="busy"
      ><p v-if="error" class="error" role="alert">{{ error }}</p>
      <form @submit.prevent="run(savePolicy)">
        <h2>联系与客户端兼容策略</h2>
        <fieldset :disabled="busy">
          <label
            >微信<input
              v-model="policy.support_wechat"
              maxlength="100"
              placeholder="zgkjkj" /></label
          ><label
            >开发者<input
              v-model="policy.developer_name"
              maxlength="100"
              placeholder="智明" /></label
          ><label
            >联系邮箱<input
              v-model="policy.support_email"
              type="email"
              maxlength="254" /></label
          ><label
            >官方联系页面<input
              v-model="policy.support_url"
              type="url"
              placeholder="https://…"
              maxlength="1000" /></label
          ><label
            >最低云功能版本<input
              v-model="policy.min_cloud_version"
              pattern="[0-9]+\.[0-9]+\.[0-9]+"
              required /></label
          ><button>保存联系与兼容策略</button
          ><button type="button" class="secondary" @click="run(refresh)">
            刷新
          </button>
        </fieldset>
        <p>
          默认微信 zgkjkj，邮箱
          zgkj@zgspace.cn，开发者智明。修改后随版本检查同步到
          Core，未修改时保留原值。
        </p>
        <p>
          GitHub OAuth Client ID、Client Secret、固定 HTTPS
          回调地址在服务端环境配置，不能下发到 Core。系统 Skill
          和工具启停配置请在对应管理页发布；工具执行代码随 Core 安装包交付。
        </p>
      </form></DetailDialog
    >
    <DetailDialog v-model:open="releaseOpen" title="发布系统版本" :busy="busy"
      ><p v-if="error" class="error" role="alert">{{ error }}</p>
      <form @submit.prevent="run(publish)">
        <h2>发布系统版本</h2>
        <p>
          下载域名必须位于服务端
          RELEASE_ALLOWED_HOSTS。安装包由发布流水线生成并签名；此页面不生成签名、不上传安装包、不自动重启客户端。
        </p>
        <fieldset :disabled="busy">
          <label
            >平台<select v-model="release.platform">
              <option>macos</option>
              <option>windows</option>
              <option>linux</option>
            </select></label
          ><label
            >架构<select v-model="release.arch">
              <option>aarch64</option>
              <option>x86_64</option>
            </select></label
          ><label
            >版本<input
              v-model="release.version"
              placeholder="0.3.1"
              pattern="[0-9]+\.[0-9]+\.[0-9]+"
              required /></label
          ><label
            >官网下载地址（HTTPS）<input
              v-model="release.download_url"
              type="url"
              maxlength="2000"
              required /></label
          ><label
            >安装包 SHA-256<input
              v-model="release.sha256"
              pattern="[a-fA-F0-9]{64}"
              required /></label
          ><label class="wide"
            >更新说明<textarea
              v-model="release.notes"
              rows="4"
              maxlength="8000"
              required
            /></label
          ><button>发布版本引导</button>
        </fieldset>
      </form></DetailDialog
    >
    <DetailDialog v-model:open="releaseDetailOpen" title="系统版本详情"
      ><dl v-if="releaseDetail" class="metadata-grid">
        <div>
          <dt>版本 / 平台</dt>
          <dd>
            {{ releaseDetail.version }} · {{ releaseDetail.platform }} /
            {{ releaseDetail.arch }}
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{{ releaseDetail.enabled ? "已发布" : "已撤回" }}</dd>
        </div>
        <div>
          <dt>官网下载地址</dt>
          <dd>{{ releaseDetail.download_url }}</dd>
        </div>
        <div>
          <dt>SHA-256</dt>
          <dd>{{ releaseDetail.sha256 }}</dd>
        </div>
      </dl>
      <pre class="payload-view">{{ releaseDetail?.notes }}</pre>
    </DetailDialog>
    <DetailDialog
      v-model:open="feedbackOpen"
      title="反馈工单详情"
      :busy="busy"
      wide
    >
      <template v-if="selected"
        ><p v-if="error" role="alert" class="error">{{ error }}</p>
        <p v-if="notice" role="status">{{ notice }}</p>
        <div class="detail-breadcrumb">
          <code>{{ selected.id }}</code
          ><button
            v-if="selected.user_id"
            class="text-link"
            @click="
              feedbackOpen = false;
              emit('user', selected.user_id);
            "
          >
            查看用户</button
          ><button
            v-if="selected.user_id && selected.content?.diagnostic?.taskId"
            class="text-link"
            @click="
              feedbackOpen = false;
              emit('monitor', {
                user_id: selected.user_id,
                q: selected.content.diagnostic.taskId,
              });
            "
          >
            查看关联任务调用
          </button>
        </div>
        <template v-if="selected.content"
          ><h3>{{ selected.content.title }}</h3>
          <div class="subnav">
            <button
              :aria-pressed="feedbackTab === 'overview'"
              @click="feedbackTab = 'overview'"
            >
              反馈内容</button
            ><button
              :aria-pressed="feedbackTab === 'logs'"
              @click="feedbackTab = 'logs'"
            >
              任务诊断与日志</button
            ><button
              :aria-pressed="feedbackTab === 'reply'"
              @click="feedbackTab = 'reply'"
            >
              处理与回复
            </button>
          </div>
          <section v-if="feedbackTab === 'overview'">
            <pre>{{ selected.content.message }}</pre>
            <p>联系方式：{{ selected.content.contact || "未留下联系方式" }}</p>
            <div class="feedback-images">
              <a
                v-for="(image, index) in selected.content.images || []"
                :key="index"
                :href="`data:${image.mime_type};base64,${image.data}`"
                :download="image.name"
                ><img
                  :src="`data:${image.mime_type};base64,${image.data}`"
                  :alt="image.name"
                /><span>{{ image.name }}</span></a
              >
            </div>
          </section>
          <section v-else-if="feedbackTab === 'logs'">
            <h4>用户确认上传的任务诊断</h4>
            <pre>{{
              JSON.stringify(selected.content.diagnostic, null, 2)
            }}</pre>
            <div v-if="selected.content.task_logs">
              <button class="secondary" @click="downloadLogs">
                下载完整任务日志
              </button>
              <pre>{{ selected.content.task_logs }}</pre>
            </div>
            <pre v-else-if="selected.content.log_excerpt">{{
              selected.content.log_excerpt
            }}</pre>
            <p v-else>未附任务日志。</p>
          </section>
          <form v-else @submit.prevent="run(respond)">
            <pre
              v-for="(item, index) in selected.content.replies"
              :key="index"
              >{{ item.message }}</pre>
            <fieldset :disabled="busy">
              <label
                >工单状态<select v-model="status" aria-label="工单状态">
                  <option value="open">待处理</option>
                  <option value="in_progress">处理中</option>
                  <option value="resolved">已解决</option>
                </select></label
              ><label class="wide"
                >工单回复（匿名反馈请另通过所留联系方式跟进）<textarea
                  v-model="reply"
                  rows="4"
                  maxlength="2000"
                /></label
              ><button>保存回复与状态</button>
            </fieldset>
          </form>
        </template>
        <p v-else>正文已删除或超过保留期。</p></template
      >
    </DetailDialog>
  </section>
</template>
<style scoped>
.cloud-panel {
  max-width: 1100px;
}
.cloud-panel form {
  margin: 20px 0;
  padding: 20px;
  border: 1px solid var(--line, #d4dae3);
  border-radius: 10px;
}
.cloud-panel fieldset {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 16px;
  border: 0;
  padding: 0;
}
.cloud-panel label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 200px;
}
.cloud-panel .wide {
  flex-basis: 100%;
}
.cloud-panel p {
  font-size: 13px;
  line-height: 1.8;
}
.cloud-panel pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 350px;
  overflow: auto;
  background: var(--bg, #f1f4f7);
  padding: 14px;
  font-size: 12px;
}
.cloud-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 0;
  border-bottom: 1px solid var(--line, #d4dae3);
}
.cloud-row > span {
  flex: 1;
}
.feedback-images {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 16px 0;
}
.feedback-images a {
  display: grid;
  gap: 6px;
  font-size: 12px;
  max-width: 220px;
  overflow-wrap: anywhere;
}
.feedback-images img {
  width: 180px;
  height: 140px;
  object-fit: contain;
  background: var(--bg, #f1f4f7);
  border-radius: 6px;
}
.cloud-panel summary {
  cursor: pointer;
  margin-top: 12px;
  font-size: 13px;
}
</style>

<style scoped>
.cloud-panel {
  max-width: 100%;
}
.cloud-panel form {
  margin: 0;
  padding: 0;
  border: 0;
}
.cloud-panel .compact-filter {
  margin-bottom: 16px;
}
.cloud-panel .compact-filter label {
  margin: 0;
  min-width: 150px;
}
.cloud-panel .toolbar-actions {
  display: flex;
  gap: 8px;
}
.cloud-panel .data-grid p {
  margin: 0;
}
.cloud-panel .dialog-body label {
  font-size: 13px;
}
.cloud-panel .wide {
  width: 100%;
}
</style>
