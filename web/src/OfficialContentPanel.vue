<script setup>
import { computed, onMounted, ref } from "vue";
import DetailDialog from "./DetailDialog.vue";
const view = ref("draft"),
  editorOpen = ref(false),
  publishOpen = ref(false),
  previewOpen = ref(false),
  query = ref("");
const filteredItems = computed(() =>
  (draft.value?.items || []).filter((item) =>
    `${item.id} ${item.name} ${item.description}`
      .toLowerCase()
      .includes(query.value.toLowerCase()),
  ),
);
function editItem(item) {
  selectedId.value = item.id;
  editorOpen.value = true;
}

const props = defineProps({
  api: { type: Function, required: true },
  kind: { type: String, required: true },
});
const draft = ref(null),
  history = ref([]),
  selectedId = ref(""),
  saved = ref("");
const schemaTexts = ref({}),
  savedSchemas = ref("");
const busy = ref(false),
  error = ref(""),
  notice = ref(""),
  preview = ref(null);
const notes = ref(""),
  minimum = ref("0.3.0");
const skills = computed(() => props.kind === "skills");
const selected = computed(() =>
  draft.value?.items.find((item) => item.id === selectedId.value),
);
const dirty = computed(
  () =>
    draft.value &&
    (JSON.stringify(draft.value.items) !== saved.value ||
      JSON.stringify(schemaTexts.value) !== savedSchemas.value),
);
const toolNames = computed(
  () => new Map((draft.value?.tool_catalog || []).map((t) => [t.id, t])),
);
const categories = {
  connectivity: "连接",
  "source-control": "源代码",
  environment: "环境",
  build: "构建",
  data: "数据",
  deployment: "部署",
  transfer: "传输",
  other: "其他",
};
const base = `official-content/${props.kind}`;

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
function accept(value) {
  draft.value = value;
  saved.value = JSON.stringify(value.items);
  syncSchemaTexts();
  savedSchemas.value = JSON.stringify(schemaTexts.value);
  if (!value.items.some((item) => item.id === selectedId.value))
    selectedId.value = value.items[0]?.id || "";
}
function syncSchemaTexts() {
  schemaTexts.value = skills.value
    ? {}
    : Object.fromEntries(
        draft.value.items.map((item) => [
          item.id,
          JSON.stringify(item.inputSchema, null, 2),
        ]),
      );
}
function resetParameters(item) {
  schemaTexts.value[item.id] = JSON.stringify(
    toolNames.value.get(item.id).inputSchema,
    null,
    2,
  );
}
async function refresh() {
  const [value, releases] = await Promise.all([
    props.api(base),
    props.api(`${base}/releases`),
  ]);
  accept(value);
  history.value = releases;
  minimum.value = releases[0]?.min_core_version || value.catalog_core_version;
}
async function reload() {
  if (dirty.value && !window.confirm("放弃尚未保存的修改并刷新？")) return;
  await refresh();
}
async function save() {
  if (skills.value) {
    for (const skill of draft.value.items) {
      for (const rule of skill.matchRules) {
        if (rule.startsWith("regex:")) {
          try {
            new RegExp(rule.slice(6), "i");
          } catch {
            throw new Error(`「${skill.name}」的匹配正则无效，请修正后保存。`);
          }
        }
      }
    }
  } else {
    const parsed = draft.value.items.map((item) => {
      try {
        return { ...item, inputSchema: JSON.parse(schemaTexts.value[item.id]) };
      } catch {
        throw new Error(
          `「${item.name}」的参数 JSON 格式不正确，请修正后保存。`,
        );
      }
    });
    draft.value.items = parsed;
  }
  accept(
    await props.api(base, "PUT", {
      revision: draft.value.revision,
      items: draft.value.items,
    }),
  );
  notice.value = "草稿已保存，发布后 Core 才会获取。";
}
async function publish() {
  if (
    !window.confirm(
      `发布 ${skills.value ? "官方 Skill" : "工具配置"} v${draft.value.next_version}？Core 下次检查时会自动获取兼容的更新。`,
    )
  )
    return;
  await save();
  await props.api(`${base}/releases`, "POST", {
    revision: draft.value.revision,
    version: draft.value.next_version,
    min_core_version: minimum.value,
    notes: notes.value,
  });
  await refresh();
  notes.value = "";
  publishOpen.value = false;
  notice.value =
    "已发布。Core 下次检查更新时自动同步；运行中任务保留 Skill 快照，工具配置对后续派发生效。";
}
function addSkill() {
  const id = `system-${Date.now()}`;
  draft.value.items.push({
    id,
    name: "新系统 Skill",
    category: "other",
    description: "",
    instructions: "",
    matchRules: [],
    enabled: true,
    version: 1,
    allowShell: false,
    allowedToolIds: [],
    forbiddenToolIds: [],
  });
  selectedId.value = id;
  editorOpen.value = true;
}
function removeSkill() {
  if (
    !window.confirm(
      `从草稿移除「${selected.value.name}」？已发布版本不受影响，发布新版本后生效。`,
    )
  )
    return;
  draft.value.items = draft.value.items.filter(
    (item) => item.id !== selectedId.value,
  );
  editorOpen.value = false;
  selectedId.value = draft.value.items[0]?.id || "";
}
async function inspect(item) {
  preview.value = await props.api(`${base}/releases/${item.id}`);
  previewOpen.value = true;
}
function restore() {
  if (
    !window.confirm(
      "将此历史内容复制到当前草稿？保存并发布更高版本后才会生效。",
    )
  )
    return;
  draft.value.items = JSON.parse(preview.value.content).items;
  if (!skills.value)
    draft.value.items = draft.value.items.map((item) => {
      const base = toolNames.value.get(item.id);
      return {
        ...Object.fromEntries(
          [
            "name",
            "description",
            "usageInstructions",
            "outputDescription",
            "inputSchema",
          ].map((key) => [key, base?.[key]]),
        ),
        ...item,
      };
    });
  syncSchemaTexts();
  selectedId.value = draft.value.items[0]?.id || "";
  preview.value = null;
  previewOpen.value = false;
  view.value = "draft";
  notice.value = "历史内容已载入草稿，请检查后保存或发布新版本。";
}
async function withdraw(item) {
  if (
    !window.confirm(
      `撤回 v${item.version}？这会停止后续分发，已更新的客户端保留本地版本。需要恢复旧内容时，请复制历史内容并发布更高版本。`,
    )
  )
    return;
  await props.api(`${base}/releases/${item.id}`, "DELETE");
  history.value = await props.api(`${base}/releases`);
  notice.value = "已撤回，停止后续分发。";
}
onMounted(() => run(refresh));
</script>

<template>
  <section class="content-panel management-panel">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="!draft && busy">正在读取官方目录…</p>
    <template v-if="draft">
      <div class="subnav">
        <button :aria-pressed="view === 'draft'" @click="view = 'draft'">
          目录与草稿</button
        ><button :aria-pressed="view === 'history'" @click="view = 'history'">
          发布历史
        </button>
      </div>
      <div class="panel-toolbar">
        <div>
          <h2>{{ skills ? "系统 Skill" : "工具配置" }}</h2>
          <p>
            Core 目录 {{ draft.catalog_core_version }} · 修订
            {{ draft.revision }} · {{ dirty ? "有未保存修改" : "已保存草稿" }}
          </p>
        </div>
        <div class="toolbar-actions">
          <button class="secondary" :disabled="busy" @click="run(reload)">
            刷新草稿</button
          ><button
            v-if="skills && view === 'draft'"
            class="secondary"
            :disabled="busy"
            @click="addSkill"
          >
            新建系统 Skill</button
          ><button
            v-if="view === 'draft'"
            class="secondary"
            :disabled="busy"
            @click="run(save)"
          >
            保存草稿</button
          ><button
            :disabled="busy || !draft.items.length"
            @click="publishOpen = true"
          >
            发布新版本
          </button>
        </div>
      </div>
      <p
        v-for="warning in draft.catalog_warnings || []"
        :key="warning"
        role="status"
      >
        {{ warning }}
      </p>
      <template v-if="view === 'draft'">
        <p class="table-note">
          编辑详情并保存草稿；只有发布后 Core 才会获取更新。{{
            skills
              ? "用户自己的 Skill 不受影响。"
              : "工具执行代码随安装包交付，此处维护说明、启停与参数约束。"
          }}
        </p>
        <form class="compact-filter" @submit.prevent>
          <label
            >搜索目录<input
              v-model="query"
              type="search"
              placeholder="名称、ID 或说明"
          /></label>
        </form>
        <div class="data-grid">
          <table>
            <thead>
              <tr>
                <th>名称 / 标识</th>
                <th>说明</th>
                <th>版本</th>
                <th>草稿状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in filteredItems" :key="item.id">
                <td>
                  <strong>{{ item.name }}</strong
                  ><small>{{ item.id }}</small>
                </td>
                <td class="description-cell">{{ item.description }}</td>
                <td>
                  {{ skills ? item.version : item.min_implementation_version }}
                </td>
                <td>{{ item.enabled ? "启用" : "停用" }}</td>
                <td>
                  <button
                    class="secondary"
                    :disabled="busy"
                    @click="editItem(item)"
                  >
                    编辑详情
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="!filteredItems.length" class="panel-empty">
          没有匹配的目录项。
        </p>
      </template>
      <template v-else>
        <section class="release-history">
          <h2>发布记录</h2>
          <p v-if="!history.length">尚未发布；Core 继续使用安装包内置内容。</p>
          <article v-for="item in history" :key="item.id" class="history-row">
            <div>
              <strong
                >v{{ item.version }} ·
                {{ item.enabled ? "已发布" : "已撤回" }}</strong
              >
              <p>
                Core ≥ {{ item.min_core_version }} ·
                {{ new Date(item.created_at * 1000).toLocaleString() }}
              </p>
              <p>{{ item.notes }}</p>
            </div>
            <button
              class="secondary"
              :disabled="busy"
              @click="run(() => inspect(item))"
            >
              查看内容</button
            ><button
              class="secondary"
              :disabled="busy || !item.enabled"
              @click="run(() => withdraw(item))"
            >
              撤回
            </button>
          </article>
        </section>
      </template>
      <DetailDialog
        v-model:open="editorOpen"
        :title="skills ? '系统 Skill 详情' : '工具配置详情'"
        :busy="busy"
        wide
      >
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <fieldset :disabled="busy" class="editor-fieldset">
          <template v-if="skills"
            ><div v-if="selected" class="skill-editor">
              <div class="field-grid">
                <label>Skill ID<input v-model="selected.id" readonly /></label>
                <label
                  >名称<input v-model="selected.name" maxlength="80" required
                /></label>
                <label
                  >分类<select v-model="selected.category">
                    <option
                      v-for="(label, id) in categories"
                      :key="id"
                      :value="id"
                    >
                      {{ label }}
                    </option>
                  </select></label
                >
                <label class="check"
                  ><input v-model="selected.enabled" type="checkbox" />启用此
                  Skill</label
                >
              </div>
              <label
                >说明<textarea
                  v-model="selected.description"
                  rows="2"
                  maxlength="1000"
                />
              </label>
              <label
                >工作流指令<textarea
                  v-model="selected.instructions"
                  rows="15"
                  maxlength="8000"
                />
              </label>
              <label
                >匹配提示（每行一条）<textarea
                  :value="selected.matchRules.join('\n')"
                  rows="3"
                  @input="
                    selected.matchRules = $event.target.value
                      .split('\n')
                      .filter((line) => line.trim())
                  "
                />
              </label>
              <label class="check"
                ><input v-model="selected.allowShell" type="checkbox" />允许请求
                Shell（仍需本机授权）</label
              >
              <details>
                <summary>工作流工具范围</summary>
                <div
                  class="tool-choices"
                  v-for="tool in draft.tool_catalog"
                  :key="tool.id"
                >
                  <span>{{ tool.name }}</span
                  ><label class="check"
                    ><input
                      v-model="selected.allowedToolIds"
                      type="checkbox"
                      :value="tool.id"
                    />允许请求</label
                  ><label class="check"
                    ><input
                      v-model="selected.forbiddenToolIds"
                      type="checkbox"
                      :value="tool.id"
                    />明确禁止</label
                  >
                </div>
              </details>
              <p>
                内容变更发布时自动递增 Skill
                版本。修改内置指令后使用完整指令规划，本地旧阶段合同不再套用。
              </p>
              <button
                class="secondary"
                :disabled="draft.items.length <= 1"
                @click="removeSkill"
              >
                从草稿移除
              </button>
            </div></template
          ><template v-else-if="selected">
            <div class="tool-editor">
              <label class="check"
                ><input
                  v-model="selected.enabled"
                  type="checkbox"
                />启用此工具</label
              >
              <label
                >工具名称<input v-model="selected.name" maxlength="80"
              /></label>
              <label
                >工具说明<textarea
                  v-model="selected.description"
                  rows="2"
                  maxlength="1000"
                />
              </label>
              <label
                >使用说明<textarea
                  v-model="selected.usageInstructions"
                  rows="3"
                  maxlength="2000"
                />
              </label>
              <label
                >输出说明<textarea
                  v-model="selected.outputDescription"
                  rows="2"
                  maxlength="1000"
                />
              </label>
              <label
                >参数协议（JSON）<textarea
                  v-model="schemaTexts[selected.id]"
                  class="schema-input"
                  rows="16"
                  spellcheck="false"
                />
              </label>
              <p>
                可调整已有参数的
                description、default、required、enum、数值范围及长度/数量限制。参数名称和类型须与
                Core
                实现一致，取值范围不能超出内置限制。默认值只在调用未传入该参数时补充。
              </p>
              <button
                type="button"
                class="secondary"
                @click="resetParameters(selected)"
              >
                恢复内置参数
              </button>
            </div></template
          >
        </fieldset>
        <p class="table-note">
          关闭保留本页未保存的编辑；保存目录草稿后仍需发布才会下发。
        </p>
        <button :disabled="busy" @click="run(save)">保存目录草稿</button>
      </DetailDialog>
      <DetailDialog v-model:open="publishOpen" title="发布官方内容" :busy="busy"
        ><p v-if="error" class="error" role="alert">{{ error }}</p>
        <form class="publish-card" @submit.prevent="run(publish)">
          <h2>
            发布{{ skills ? "官方 Skill" : "工具配置" }} v{{
              draft.next_version
            }}
          </h2>
          <fieldset :disabled="busy" class="editor-fieldset">
            <label
              >最低 Core 版本<input
                v-model="minimum"
                pattern="[0-9]+\.[0-9]+\.[0-9]+"
                required
            /></label>
            <label
              >发布说明<textarea
                v-model="notes"
                rows="3"
                maxlength="8000"
                required
              />
            </label>
            <button>保存并发布</button>
          </fieldset>
        </form>
      </DetailDialog>
      <DetailDialog v-model:open="previewOpen" title="发布记录详情" wide>
        <section v-if="preview" class="publish-card">
          <h3>发布内容 v{{ preview.version }}</h3>
          <p class="checksum">SHA-256：{{ preview.sha256 }}</p>
          <pre>{{ JSON.stringify(JSON.parse(preview.content), null, 2) }}</pre>
          <button :disabled="busy" @click="restore">复制到草稿</button
          ><button class="secondary" @click="previewOpen = false">
            关闭预览
          </button>
        </section>
      </DetailDialog>
    </template>
  </section>
</template>

<style scoped>
.content-panel {
  max-width: 1200px;
}
.content-panel p {
  font-size: 13px;
  line-height: 1.7;
  color: var(--muted, #627080);
}
.content-toolbar,
.history-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.content-toolbar > div,
.history-row > div {
  flex: 1;
}
.content-toolbar p {
  margin: 6px 0;
}
.editor-fieldset {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.skill-layout {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr);
  border: 1px solid var(--line, #d4dae3);
  border-radius: 10px;
  overflow: hidden;
  margin: 20px 0;
}
.skill-list {
  padding: 12px;
  border-right: 1px solid var(--line, #d4dae3);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.skill-list button {
  text-align: left;
  padding: 12px;
  white-space: normal;
}
.skill-list small {
  display: block;
  margin-top: 7px;
  overflow-wrap: anywhere;
  font-size: 11px;
}
.skill-list .selected {
  border-color: var(--accent, #467cf0);
  background: var(--bg, #f1f4f7);
}
.skill-editor {
  padding: 20px;
  min-width: 0;
}
.content-panel label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}
.field-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
.content-panel .check {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}
.check input {
  width: auto;
}
.content-panel textarea {
  width: 100%;
  resize: vertical;
}
.tool-row,
.publish-card {
  padding: 20px;
  border: 1px solid var(--line, #d4dae3);
  border-radius: 10px;
  margin: 16px 0;
}
.publish-card h2 {
  margin-top: 0;
}
.tool-choices {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 14px 0;
  font-size: 12px;
}
.tool-choices > span {
  flex: 1;
}
.tool-choices label {
  margin: 0;
}
.history-row {
  padding: 16px 0;
  border-bottom: 1px solid var(--line, #d4dae3);
}
.content-panel pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 400px;
  overflow: auto;
  background: var(--bg, #f1f4f7);
  padding: 14px;
  font-size: 12px;
}
.tool-editor > summary {
  margin-bottom: 18px;
}
.schema-input {
  font-family: ui-monospace, monospace;
  font-size: 12px;
  line-height: 1.6;
}
.checksum {
  overflow-wrap: anywhere;
}
.content-panel summary {
  cursor: pointer;
  font-size: 13px;
}
.publish-card button {
  margin-right: 10px;
}
@media (max-width: 900px) {
  .skill-layout {
    grid-template-columns: 1fr;
  }
  .skill-list {
    border-right: 0;
    border-bottom: 1px solid var(--line, #d4dae3);
  }
  .content-toolbar,
  .history-row {
    flex-wrap: wrap;
  }
  .field-grid {
    grid-template-columns: 1fr;
  }
}
</style>

<style scoped>
.content-panel {
  max-width: 100%;
}
.toolbar-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.description-cell {
  max-width: 480px;
  white-space: normal;
}
.content-panel .publish-card {
  margin: 0;
  padding: 0;
  border: 0;
}
.content-panel .skill-editor {
  padding: 0;
}
.content-panel .release-history h2 {
  font-size: 15px;
}
.content-panel .history-row p {
  margin: 5px 0;
}
.content-panel .history-row {
  padding: 12px 0;
}
.dialog-body label {
  font-size: 13px;
}
.content-panel .toolbar-actions button {
  font-size: 12px;
  padding: 8px 12px;
}
</style>
