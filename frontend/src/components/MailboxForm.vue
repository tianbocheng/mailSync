<template>
  <el-form label-position="top" @submit.prevent>
    <el-row :gutter="12">
      <el-col :span="12">
        <el-form-item label="邮箱地址" required>
          <el-input
            v-model="form.email"
            placeholder="example@domain.com"
          />
        </el-form-item>
      </el-col>
      <el-col :span="12">
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            autocomplete="new-password"
            :placeholder="passwordHelp"
            show-password
          />
        </el-form-item>
      </el-col>
    </el-row>

    <el-tabs v-model="activeEditorTab" @tab-change="handleEditorTabChange">
      <el-tab-pane label="IMAP" name="imap">
        <ProtocolFields
          v-model="form.imap"
          protocol="imap"
          :proxies="proxies"
        />
      </el-tab-pane>
      <el-tab-pane label="POP" name="pop">
        <ProtocolFields
          v-model="form.pop"
          protocol="pop"
          :required="false"
          :proxies="proxies"
        />
      </el-tab-pane>
      <el-tab-pane label="邮箱特征" name="features">
          <div class="probe-head">
            <div>
              <div class="probe-subtitle">特征初步检测</div>
              <div v-if="probeTestedAt" class="probe-time">{{ probeTestedAt }}</div>
            </div>
            <el-tag v-if="testing" type="info">检测中</el-tag>
            <el-tag v-else-if="probeStatus" :type="probeStatus.type">
              {{ probeStatus.label }}
            </el-tag>
          </div>
          <div v-if="testing && !displayedProbeResult" class="probe-placeholder">
            正在检测邮箱服务特性...
          </div>
          <template v-else>
            <div class="feature-list feature-list-common">
              <div
                v-for="item in commonFeatureItems"
                :key="item.key"
                class="feature-item"
              >
                <div class="feature-item-head">
                  <span>{{ item.label }}</span>
                  <el-button
                    v-if="item.help"
                    class="feature-help"
                    :class="item.helpClass"
                    :icon="QuestionFilled"
                    circle
                    link
                    :aria-label="`${item.label}说明`"
                    @click="openFeatureTour(item.help)"
                  />
                </div>
                <el-tag
                  class="feature-value"
                  :type="featureStatusType(item.status)"
                  effect="plain"
                >
                  {{ item.value }}
                </el-tag>
                <div class="feature-message">{{ item.message }}</div>
              </div>
            </div>
            <el-tabs v-model="activeFeatureTab" class="feature-tabs">
              <el-tab-pane label="来源邮箱特征" name="source">
                <div class="feature-list feature-list-source">
                  <div
                    v-for="item in sourceFeatureItems"
                    :key="item.key"
                    class="feature-item"
                    :class="{ 'feature-item-with-setting': item.setting }"
                  >
                    <div class="feature-item-head">
                      <span>{{ item.label }}</span>
                      <el-button
                        v-if="item.help"
                        class="feature-help"
                        :class="item.helpClass"
                        :icon="QuestionFilled"
                        circle
                        link
                        :aria-label="`${item.label}说明`"
                        @click="openFeatureTour(item.help)"
                      />
                    </div>
                    <el-tag
                      class="feature-value"
                      :type="featureStatusType(item.status)"
                      effect="plain"
                    >
                      {{ item.value }}
                    </el-tag>
                    <div class="feature-message">{{ item.message }}</div>
                    <div
                      v-if="item.setting === 'realtime_poll_interval'"
                      class="feature-setting"
                    >
                      <span class="feature-setting-label">轮询间隔</span>
                      <el-input-number
                        v-model="form.realtime_poll_interval"
                        :min="1"
                        :max="3600"
                        :step="1"
                        size="small"
                        controls-position="right"
                        :disabled="testing"
                        aria-label="准实时轮询间隔秒数"
                      />
                      <span class="feature-setting-unit">秒</span>
                    </div>
                  </div>
                </div>
              </el-tab-pane>
              <el-tab-pane label="目标邮箱特征" name="target">
                <div class="feature-list">
                  <div
                    v-for="item in targetFeatureItems"
                    :key="item.key"
                    class="feature-item"
                  >
                    <div class="feature-item-head">
                      <span>{{ item.label }}</span>
                      <el-button
                        v-if="item.help"
                        class="feature-help"
                        :class="item.helpClass"
                        :icon="QuestionFilled"
                        circle
                        link
                        :aria-label="`${item.label}说明`"
                        @click="openFeatureTour(item.help)"
                      />
                    </div>
                    <el-tag
                      class="feature-value"
                      :type="featureStatusType(item.status)"
                      effect="plain"
                    >
                      {{ item.value }}
                    </el-tag>
                    <div class="feature-message">{{ item.message }}</div>
                  </div>
                </div>
              </el-tab-pane>
            </el-tabs>
          </template>
          <el-tour v-model="featureTourOpen">
            <el-tour-step
              v-for="step in activeFeatureTourSteps"
              :key="step.title"
              :target="step.target"
              :title="step.title"
              :description="step.description"
            />
          </el-tour>
      </el-tab-pane>
    </el-tabs>
    <div class="form-actions result-box">
      <el-button @click="close">取消</el-button>
      <el-button :loading="testing" @click="testConnection">测试</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </div>
  </el-form>
</template>

<script setup>
import { computed, nextTick, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { QuestionFilled } from "@element-plus/icons-vue";

import { mailApi } from "@/api/mail";
import ProtocolFields from "@/components/ProtocolFields.vue";
import { appendField, submitFields, truthyValue } from "@/utils/forms";

const props = defineProps({
  detail: {
    type: Object,
    default: () => ({}),
  },
  mailbox: {
    type: Object,
    default: null,
  },
  isNew: {
    type: Boolean,
    default: false,
  },
});

const saving = ref(false);
const testing = ref(false);
const pendingProbeResult = ref(null);
const showStoredProbe = ref(true);
const resetting = ref(false);
const activeEditorTab = ref("imap");
const activeFeatureTab = ref("source");
const featureTourOpen = ref(false);
const activeFeatureTour = ref("");
const form = reactive(defaultForm());

const account = computed(() => (props.isNew ? {} : (props.mailbox || {})));
const proxies = computed(() => props.detail.proxies || []);
const passwordHelp = computed(() => account.value.has_password ? "留空则保留现有密码" : "未保存密码");
const displayedProbeResult = computed(() => (
  pendingProbeResult.value
  || (showStoredProbe.value ? account.value.probe_results : null)
  || null
));
const probeItems = computed(() => displayedProbeResult.value?.items || []);
const probeTestedAt = computed(() => formatProbeTime(displayedProbeResult.value?.tested_at));
const commonFeatureItems = computed(() => [
  allTimeRangeFeature(),
]);
const sourceFeatureItems = computed(() => [
  idleFeature(),
  sourceDateCompensationFeature(),
]);
const targetFeatureItems = computed(() => [
  displayTimeFeature(),
  specialContentFeature(),
]);
const activeFeatureTourSteps = computed(() => {
  const tours = {
    allTimeRange: [
      {
        target: ".common-full-range-help",
        title: "全时间段范围",
        description: "用于判断邮箱服务是否支持历史邮件完整同步。若服务侧只开放最近一段时间，旧邮件可能读取或导入异常，同步日志会在备注或失败原因中提示。",
      },
    ],
    displayTime: [
      {
        target: ".target-display-time-help",
        title: "目标补写到达记录",
        description: "部分目标邮箱会给导入邮件补写到达记录，网页列表可能显示导入时间；邮件实际发生时间仍会保留。",
      },
    ],
  };
  return tours[activeFeatureTour.value] || [];
});
const probeStatus = computed(() => {
  if (!displayedProbeResult.value) return null;
  if (probeItems.value.some((item) => item.status === "fail")) {
    return { type: "danger", label: "有失败项" };
  }
  if (probeItems.value.some((item) => item.status === "warn")) {
    return { type: "warning", label: "有兼容项" };
  }
  if (probeItems.value.some((item) => item.status === "skip")) {
    return { type: "info", label: "部分跳过" };
  }
  return { type: "success", label: "通过" };
});

watch(
  () => [props.mailbox, props.isNew],
  () => reset(),
  { immediate: true },
);

watch(
  () => [
    form.email,
    form.target_folder,
    form.imap.server,
    form.imap.port,
    form.imap.use_ssl,
    form.imap.proxy,
    form.pop.server,
    form.pop.port,
    form.pop.use_ssl,
    form.pop.proxy,
  ],
  () => {
    if (resetting.value) return;
    pendingProbeResult.value = null;
    showStoredProbe.value = false;
  },
);

function defaultForm() {
  return {
    email: "",
    password: "",
    target_folder: "INBOX",
    protocol: "imap",
    imap: protocolModel("imap", {}),
    pop: protocolModel("pop", {}),
    smtp_enabled: false,
    smtp_server: "",
    smtp_port: "",
    smtp_use_ssl: "1",
    smtp_timeout: "",
    realtime_poll_interval: 3,
  };
}

function protocolModel(protocol, item) {
  return {
    server: protocol === "imap"
      ? imapServerModel(item)
      : popServerModel(item),
    port: protocol === "imap" ? (item.imap_port || "993") : (item.pop_port || "995"),
    use_ssl: truthyValue(protocol === "imap" ? item.use_ssl !== false : item.pop_use_ssl !== false),
    proxy: item.proxy_profile_id ? String(item.proxy_profile_id) : "",
  };
}

function imapServerModel(item) {
  if (item.imap_enabled === false && item.pop_enabled) return "";
  return item.imap_server || item.service_address || "";
}

function popServerModel(item) {
  if (item.pop_server) return item.pop_server;
  if (item.imap_enabled === false && item.pop_enabled) {
    return item.service_address || "";
  }
  return "";
}

function reset() {
  resetting.value = true;
  const item = account.value;
  pendingProbeResult.value = null;
  showStoredProbe.value = true;
  form.email = item.email || "";
  form.password = "";
  form.target_folder = item.target_folder || "INBOX";
  form.protocol = item.imap_enabled === false && item.pop_enabled ? "pop" : "imap";
  activeEditorTab.value = form.protocol;
  form.imap = protocolModel("imap", item);
  form.pop = protocolModel("pop", item);
  form.smtp_enabled = Boolean(item.smtp_enabled);
  form.smtp_server = item.smtp_server || "";
  form.smtp_port = item.smtp_port || "";
  form.smtp_use_ssl = truthyValue(item.smtp_use_ssl !== false);
  form.smtp_timeout = item.smtp_timeout || "";
  form.realtime_poll_interval = normalizePollInterval(item.realtime_poll_interval);
  nextTick(() => {
    resetting.value = false;
  });
}

function buildFields() {
  const active = form[form.protocol];
  const popServer = firstValue(form.pop.server);
  const imapServer = firstValue(form.imap.server);
  const imapEnabled = Boolean(imapServer);
  const popEnabled = Boolean(popServer);
  const email = firstValue(
    form.email,
    account.value.email,
  );
  const serviceAddress = firstValue(
    imapServer,
    account.value.service_address,
    account.value.imap_server,
    popServer,
  );
  const fields = submitFields({
    original_email: props.isNew ? "" : account.value.email || "",
    original_service_address: props.isNew ? "" : account.value.service_address || account.value.imap_server || "",
    email,
    service_address: serviceAddress,
    auth_provider: "manual",
    password: form.password,
    oauth_client_id: account.value.oauth_client_id || props.detail.outlook_defaults?.client_id || "",
    imap_timeout: account.value.imap_timeout || "",
    proxy_profile_id: active.proxy,
    target_folder: form.target_folder || "INBOX",
    mailbox_protocol_tab: form.protocol,
    smtp_enabled: form.smtp_enabled,
    smtp_server: form.smtp_server,
    smtp_port: form.smtp_port,
    smtp_timeout: form.smtp_timeout,
    smtp_use_ssl: form.smtp_use_ssl === "1",
    realtime_poll_interval: normalizePollInterval(form.realtime_poll_interval),
  });

  appendField(fields, "imap_enabled", imapEnabled ? "1" : "");
  appendField(fields, "imap_server", imapEnabled ? imapServer : "");
  appendField(fields, "imap_port", imapEnabled ? firstValue(form.imap.port, account.value.imap_port, "993") : "");
  appendField(fields, "use_ssl", imapEnabled ? form.imap.use_ssl : "");
  appendField(fields, "pop_fallback_enabled", popEnabled ? "1" : "");
  appendField(fields, "pop_server", popServer);
  appendField(fields, "pop_port", popEnabled ? firstValue(form.pop.port, account.value.pop_port, "995") : "");
  appendField(fields, "pop_timeout", account.value.pop_timeout || "");
  appendField(fields, "pop_use_ssl", popEnabled ? form.pop.use_ssl : "");
  if (pendingProbeResult.value) {
    appendField(fields, "probe_results_json", JSON.stringify(pendingProbeResult.value));
  }
  return fields;
}

function firstValue(...values) {
  const value = values.find((item) => String(item || "").trim());
  return String(value || "").trim();
}

async function save() {
  saving.value = true;
  try {
    const result = await mailApi.saveAccount(buildFields());
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "保存失败。");
  } finally {
    saving.value = false;
  }
}

async function testConnection() {
  testing.value = true;
  pendingProbeResult.value = null;
  showStoredProbe.value = false;
  try {
    const result = await mailApi.testConnection(buildFields());
    pendingProbeResult.value = result.probe_results || {
      tested_at: "",
      protocol: form.protocol,
      items: result.items || [],
      features: result.features || {},
    };
    activeEditorTab.value = "features";
  } catch (error) {
    pendingProbeResult.value = {
      tested_at: "",
      protocol: form.protocol,
      items: [
        {
          key: "request_failed",
          label: "连接测试",
          status: "fail",
          message: error.message || "测试失败。",
        },
      ],
      features: {},
    };
    activeEditorTab.value = "features";
  } finally {
    testing.value = false;
  }
}

function close() {
  window.MailSyncNavigate?.({ path: "/mail" });
}

function featureStatusType(status) {
  if (status === "ok") return "success";
  if (status === "warn") return "warning";
  if (status === "fail") return "danger";
  return "info";
}

function handleEditorTabChange(name) {
  if (name === "imap" || name === "pop") {
    form.protocol = name;
  }
}

function openFeatureTour(name) {
  activeFeatureTour.value = name;
  featureTourOpen.value = true;
}

function allTimeRangeFeature() {
  const value = firstDefined(
    firstFeatureValue("common", "all_time_range_supported"),
    firstFeatureValue(
      "source",
      "source_all_time_range_supported",
      "target_all_time_range_supported",
      "append_internal_date_supported",
    ),
    firstFeatureValue(
      "target",
      "target_all_time_range_supported",
      "append_internal_date_supported",
    ),
  );
  const allowed = value === true;
  const denied = value === false;
  return {
    key: "all_time_range",
    label: "全时间段范围",
    value: allowed ? "允许" : (denied ? "不允许" : "未检测"),
    status: allowed ? "ok" : (denied ? "warn" : "skip"),
    help: "allTimeRange",
    helpClass: "common-full-range-help",
    message: allowed
      ? "适合历史邮件全量同步。"
      : (
        denied
          ? "服务侧可能限制历史邮件范围，同步遇到异常会写入日志。"
          : "尚未形成结论，保存后可重新测试。"
      ),
  };
}

function idleFeature() {
  const value = firstFeatureValue("source", "idle_supported");
  return {
    key: "idle_supported",
    label: "准实时同步",
    value: value === true ? "支持" : (value === false ? "轮询" : "未检测"),
    status: value === true ? "ok" : (value === false ? "warn" : "skip"),
    setting: "realtime_poll_interval",
    message: value === true
      ? "可以监听新邮件变化；异常时按右侧间隔检查。"
      : (
        value === false
          ? "不支持准实时监听，会按右侧间隔检查变化。"
          : "尚未检测准实时能力；保存后仍按该间隔兜底检查。"
      ),
  };
}

function sourceDateCompensationFeature() {
  const value = firstFeatureValue("source", "synthesize_missing_date_header");
  return {
    key: "source_date_compensation",
    label: "缺失时间补偿",
    value: value === true ? "自动补偿" : (value === false ? "不启用" : "未检测"),
    status: value === true ? "ok" : "skip",
    message: value === true
      ? "来源邮件缺少标准时间时，会用邮箱内部时间补齐。"
      : "来源邮件按原始内容处理。"
  };
}

function displayTimeFeature() {
  const arrivalRecord = firstFeatureValue("target", "leading_received_header_added");
  const displayValue = firstFeatureValue(
    "target",
    "web_display_time_uses_import_time",
    "display_time_uses_append_time_without_internal_date",
    "effective_display_time_uses_append_time",
  );
  const added = arrivalRecord === true || (arrivalRecord === undefined && displayValue === true);
  const notAdded = arrivalRecord === false || (arrivalRecord === undefined && displayValue === false);
  return {
    key: "target_display_time",
    label: "目标补写到达记录",
    value: added ? "会补写" : (notAdded ? "未补写" : "未检测"),
    status: added ? "warn" : (notAdded ? "ok" : "skip"),
    help: "displayTime",
    helpClass: "target-display-time-help",
    message: added
      ? "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
      : (
        notAdded
          ? "未发现目标补写到达记录。"
          : "尚未检测目标到达记录。"
      ),
  };
}

function specialContentFeature() {
  const value = firstFeatureValue("target", "special_content_roundtrip");
  const normalized = firstFeatureValue("target", "nul_rewrite_normalized");
  const rewritten = normalized === true || value === false;
  const clean = normalized === false && value === true;
  return {
    key: "special_content",
    label: "目标特殊内容改写",
    value: rewritten ? "需注意" : (clean ? "未发现" : "未检测"),
    status: rewritten ? "warn" : (clean ? "ok" : "skip"),
    message: rewritten
      ? "特殊内容可能被服务侧改写。"
      : (
        clean
          ? "特殊内容写入后保持可校验。"
          : "尚未检测特殊内容处理。"
      ),
  };
}

function firstFeatureValue(scope, ...keys) {
  const result = displayedProbeResult.value || {};
  const groups = result.feature_groups || {};
  const group = groups[scope] || {};
  const features = result.features || {};
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(group, key)) return group[key];
    if (Object.prototype.hasOwnProperty.call(features, key)) return features[key];
  }
  return undefined;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined);
}

function formatProbeTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function normalizePollInterval(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 1) return 3;
  return Math.min(3600, Math.round(parsed));
}
</script>

<style scoped>
.probe-panel {
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  margin-top: 14px;
  padding: 12px;
}

.probe-head {
  align-items: flex-start;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.probe-title {
  color: var(--el-text-color-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.probe-subtitle {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 18px;
}

.probe-time {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 18px;
}

.probe-placeholder {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 20px;
}

.feature-section-title {
  color: var(--el-text-color-primary);
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
  margin-bottom: 6px;
}

.feature-tabs {
  margin-top: 10px;
}

.feature-list {
  border: 1px solid var(--el-border-color-lighter);
  display: grid;
}

.feature-list-common {
  margin-bottom: 2px;
}

.feature-item {
  align-items: center;
  display: grid;
  gap: 8px;
  grid-template-columns: minmax(118px, 0.9fr) 86px minmax(0, 1.8fr);
  min-height: 44px;
  padding: 8px 10px;
}

.feature-item-with-setting {
  grid-template-columns: minmax(118px, 0.9fr) 86px minmax(0, 1.35fr) minmax(178px, 0.85fr);
}

.feature-list-source .feature-item {
  grid-template-columns: minmax(118px, 0.9fr) 86px minmax(0, 1.35fr) minmax(178px, 0.85fr);
}

.feature-item + .feature-item {
  border-top: 1px solid var(--el-border-color-lighter);
}

.feature-item-head {
  align-items: center;
  color: var(--el-text-color-primary);
  display: flex;
  font-size: 13px;
  font-weight: 600;
  gap: 4px;
  min-width: 0;
}

.feature-item-head span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.feature-help {
  color: var(--el-text-color-secondary);
  flex: 0 0 auto;
  height: 20px;
  min-width: 20px;
  padding: 0;
  width: 20px;
}

.feature-value {
  justify-self: start;
  min-width: 58px;
  justify-content: center;
}

.feature-message {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 18px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.feature-setting {
  align-items: center;
  color: var(--el-text-color-secondary);
  display: flex;
  font-size: 12px;
  gap: 6px;
  justify-self: end;
  line-height: 18px;
  min-width: 0;
  white-space: nowrap;
}

.feature-setting :deep(.el-input-number) {
  width: 112px;
}

.feature-setting-label,
.feature-setting-unit {
  flex: 0 0 auto;
}

@media (max-width: 760px) {
  .feature-item {
    align-items: flex-start;
    grid-template-columns: minmax(0, 1fr);
  }

  .feature-value {
    justify-self: start;
  }

  .feature-setting {
    justify-self: start;
  }
}
</style>
