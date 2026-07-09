<template>
  <section class="page">
    <el-card class="page-card" shadow="never">
      <div class="page-heading">
        <h2>同步规则管理</h2>
      </div>
      <div class="toolbar">
        <div class="toolbar-actions">
          <el-button type="primary" :disabled="!canCreate" @click="openNewRule">
            新增规则
          </el-button>
        </div>
        <el-form class="filter-row" inline @submit.prevent>
          <el-form-item label="源邮箱">
            <el-input v-model="sourceQuery" clearable placeholder="请输入源邮箱" style="width: 180px" @keyup.enter="applyFilter" />
          </el-form-item>
          <el-form-item label="目标邮箱">
            <el-input v-model="targetQuery" clearable placeholder="请输入目标邮箱" style="width: 180px" @keyup.enter="applyFilter" />
          </el-form-item>
          <el-button type="primary" @click="applyFilter">搜索</el-button>
          <el-button @click="resetFilter">重置</el-button>
        </el-form>
      </div>
      <el-alert
        v-if="data?.error"
        :title="data.error"
        type="error"
        show-icon
        :closable="false"
        class="result-box"
      />
      <el-table :data="pagedRules" empty-text="暂无同步规则" row-key="email">
        <el-table-column label="源邮箱" min-width="200">
          <template #default="{ row }">
            <div class="strong-cell">
              <strong>{{ row.email || "未配置" }}</strong>
              <small>{{ mailboxProtocol(row.account || {}) }} · {{ row.account?.imap_server || row.account?.service_address || "-" }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="目标邮箱" min-width="200">
          <template #default="{ row }">
            <div class="strong-cell">
              <strong>{{ row.target_email || "未配置" }}</strong>
              <small>{{ row.target_service_address || "-" }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="150">
          <template #default="{ row }">
            <el-tooltip v-if="ruleHasError(row)" placement="top">
              <template #content>
                <div class="rule-error-tooltip">
                  <div v-for="line in ruleErrorLines(row)" :key="line">
                    {{ line }}
                  </div>
                </div>
              </template>
              <span :class="['status-dot', 'is-error', { 'is-muted': !row.enabled }]">
                {{ ruleStatusText(row) }}
              </span>
            </el-tooltip>
            <span v-else :class="['status-dot', { 'is-muted': !row.enabled }]">
              {{ ruleStatusText(row) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <div class="inline-actions rule-actions">
              <el-button link type="primary" @click="openRule(row)">编辑</el-button>
              <el-button
                link
                :type="row.enabled ? 'warning' : 'success'"
                :loading="isRuleToggling(row)"
                :disabled="Boolean(togglingEmail) && !isRuleToggling(row)"
                @click="toggleRule(row)"
              >
                {{ row.enabled ? "暂停" : "启动" }}
              </el-button>
              <el-button link type="primary" @click="openLogs(row)">日志</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="table-footer">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="filteredRules.length"
          layout="total, prev, pager, next, sizes, jumper"
        />
      </div>
    </el-card>

    <RuleEditor
      v-model="editorOpen"
      :detail="detail"
      :params="params"
      @navigate="emit('navigate', $event)"
      @reload="emit('reload')"
    />

    <RuleLogsDialog
      v-model="logDialogOpen"
      :source-email="logSourceEmail"
      :target-email="logTargetEmail"
    />
  </section>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { ruleApi } from "@/api/rule";
import RuleEditor from "@/components/RuleEditor.vue";
import RuleLogsDialog from "@/components/RuleLogsDialog.vue";
import { paramsFromSearch } from "@/utils/route";

const props = defineProps({
  data: {
    type: Object,
    default: null,
  },
  search: {
    type: String,
    default: "",
  },
});

const emit = defineEmits(["navigate", "reload"]);

const sourceQuery = ref("");
const targetQuery = ref("");
const currentPage = ref(1);
const pageSize = ref(10);
const togglingEmail = ref("");
const logDialogOpen = ref(false);
const logSourceEmail = ref("");
const logTargetEmail = ref("");
const RULE_FALLBACK_DEFAULTS = Object.freeze({
  sync_interval: "900",
  batch_size: "5",
});

const detail = computed(() => props.data?.detail || {});
const params = computed(() => paramsFromSearch(props.search));
const editorOpen = computed({
  get() {
    return Boolean(detail.value.selected && (params.value.get("dialog") === "detail" || params.value.has("error")));
  },
  set(value) {
    if (!value) emit("navigate", { path: "/rules" });
  },
});
const canCreate = computed(() => Boolean((detail.value.available_sources || [])[0] && (detail.value.target_accounts || [])[0]));

const filteredRules = computed(() => {
  const source = sourceQuery.value.trim().toLowerCase();
  const target = targetQuery.value.trim().toLowerCase();
  return (detail.value.rule_details || []).filter((item) => {
    const sourceEmail = String(item.email || "").toLowerCase();
    const sourceService = String(item.source_service_address || item.account?.service_address || item.account?.imap_server || "").toLowerCase();
    const targetEmail = String(item.target_email || "").toLowerCase();
    const targetService = String(item.target_service_address || "").toLowerCase();
    return (
      !source || sourceEmail.includes(source) || sourceService.includes(source)
    ) && (
      !target || targetEmail.includes(target) || targetService.includes(target)
    );
  });
});

const pagedRules = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return filteredRules.value.slice(start, start + pageSize.value);
});

watch(
  () => props.search,
  () => {
    const next = paramsFromSearch(props.search);
    sourceQuery.value = next.get("source_query") || "";
    targetQuery.value = next.get("target_query") || "";
    currentPage.value = 1;
  },
  { immediate: true },
);

function applyFilter() {
  currentPage.value = 1;
  const next = new URLSearchParams();
  if (sourceQuery.value.trim()) next.set("source_query", sourceQuery.value.trim());
  if (targetQuery.value.trim()) next.set("target_query", targetQuery.value.trim());
  emit("navigate", { path: "/rules", search: next });
}

function resetFilter() {
  sourceQuery.value = "";
  targetQuery.value = "";
  currentPage.value = 1;
  emit("navigate", { path: "/rules" });
}

function openRule(row) {
  const next = new URLSearchParams({ dialog: "detail", source: row.email || "" });
  const sourceService = row.source_service_address || row.account?.service_address || "";
  if (sourceService) next.set("source_service", sourceService);
  if (row.target_email) next.set("target", row.target_email);
  if (row.target_service_address) next.set("target_service", row.target_service_address);
  emit("navigate", { path: "/rules", search: next });
}

function isRuleToggling(row) {
  return togglingEmail.value === (row.email || "");
}

async function toggleRule(row) {
  const sourceEmail = row.email || "";
  if (!sourceEmail || togglingEmail.value) return;
  togglingEmail.value = sourceEmail;
  try {
    const result = await ruleApi.toggle({
      source_email: sourceEmail,
      enabled: row.enabled ? "" : "1",
    });
    if (window.MailSyncApplyRedirect) {
      window.MailSyncApplyRedirect(result);
    } else {
      emit("reload");
    }
  } catch (error) {
    ElMessage.error(error.message || "操作失败。");
  } finally {
    togglingEmail.value = "";
  }
}

function openLogs(row) {
  const sourceEmail = row.email || "";
  if (!sourceEmail) return;
  logSourceEmail.value = sourceEmail;
  logTargetEmail.value = row.target_email || "";
  logDialogOpen.value = true;
}

function openNewRule() {
  const source = (detail.value.available_sources || [])[0];
  const target = (detail.value.target_accounts || [])[0];
  if (!source || !target) return;
  const next = new URLSearchParams({
    dialog: "detail",
    source: source.email || "",
    target: target.email || "",
  });
  if (source.service_address) next.set("source_service", source.service_address);
  if (target.service_address) next.set("target_service", target.service_address);
  emit("navigate", { path: "/rules", search: next });
}

function mailboxProtocol(account) {
  const server = String(account.imap_server || account.service_address || "").toLowerCase();
  return server.includes("pop.") ? "POP" : "IMAP";
}

function ruleStatusText(row) {
  const status = ruleHasError(row) ? "异常" : (row.enabled ? "正常" : "停用");
  return `${status}（${ruleIntervalText(row)}/${ruleBatchText(row)}）`;
}

function ruleHasError(row) {
  return String(row.sync_status || "").toLowerCase() === "error"
    || Boolean(row.sync_error_summary || row.sync_error_detail);
}

function ruleErrorLines(row) {
  const lines = [];
  const summary = String(row.sync_error_summary || "").trim();
  const detail = String(row.sync_error_detail || "").trim();
  lines.push(summary || "规则初始化失败");
  if (detail && detail !== summary) lines.push(detail);
  return lines;
}

function ruleIntervalText(row) {
  return `${ruleNumericValue(row.sync_interval, "sync_interval")}秒`;
}

function ruleBatchText(row) {
  return `${ruleNumericValue(row.batch_size, "batch_size")}封`;
}

function ruleNumericValue(value, name) {
  const text = String(value ?? "").trim();
  if (/^[1-9]\d*$/.test(text)) return text;
  return ruleDefaultValue(name);
}

function ruleDefaultValue(name) {
  const value = detail.value.defaults?.[name] ?? RULE_FALLBACK_DEFAULTS[name];
  const text = String(value || "").trim();
  if (/^[1-9]\d*$/.test(text)) return text;
  return RULE_FALLBACK_DEFAULTS[name];
}
</script>

<style scoped>
.status-dot {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #3f4755;
}

.status-dot::before {
  width: 6px;
  height: 6px;
  content: "";
  border-radius: 999px;
  background: var(--success);
}

.status-dot.is-muted::before {
  background: #c7cbd3;
}

.status-dot.is-error {
  color: var(--el-color-danger, #f56c6c);
  cursor: help;
}

.status-dot.is-error::before {
  background: var(--el-color-danger, #f56c6c);
}

.status-dot.is-error.is-muted::before {
  background: var(--el-color-danger, #f56c6c);
}

.rule-error-tooltip {
  display: grid;
  gap: 4px;
  max-width: 360px;
  line-height: 1.5;
}

.rule-actions {
  flex-wrap: nowrap;
}
</style>
