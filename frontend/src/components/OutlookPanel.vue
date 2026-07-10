<template>
  <el-form label-position="top" @submit.prevent>
    <el-row :gutter="12">
      <el-col :span="12">
        <el-form-item label="邮箱地址" required>
          <el-input
            :model-value="account.email || ''"
            disabled
            placeholder="example@domain.com"
          />
        </el-form-item>
      </el-col>
      <el-col :span="12">
        <el-form-item label="认证方式">
          <el-input model-value="Microsoft OAuth" disabled />
        </el-form-item>
      </el-col>
    </el-row>

    <el-tabs v-model="activeEditorTab">
      <el-tab-pane label="IMAP" name="imap">
        <ProtocolFields
          v-model="form.imap"
          protocol="imap"
          :proxies="proxies"
        />
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="OAuth Client ID">
              <el-input
                :model-value="account.oauth_client_id || '-'"
                disabled
              />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="OAuth 令牌">
              <el-input
                :model-value="account.oauth_token_saved ? '已保存到数据库' : '未保存'"
                disabled
              />
            </el-form-item>
          </el-col>
        </el-row>
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
              <div class="feature-item-head">{{ item.label }}</div>
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
                  <div class="feature-item-head">{{ item.label }}</div>
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
                      v-model="realtimePollInterval"
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
                  <div class="feature-item-head">{{ item.label }}</div>
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
      </el-tab-pane>
    </el-tabs>

    <el-alert
      v-if="testMessage"
      :title="testMessage"
      type="success"
      show-icon
      :closable="false"
      class="result-box"
    />

    <div class="form-actions result-box">
      <el-button @click="close">取消</el-button>
      <el-button :loading="testing" @click="testConnection">测试</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </div>
  </el-form>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { mailApi } from "@/api/mail";
import ProtocolFields from "@/components/ProtocolFields.vue";
import { submitFields, truthyValue } from "@/utils/forms";

const props = defineProps({
  detail: {
    type: Object,
    default: () => ({}),
  },
  mailbox: {
    type: Object,
    default: null,
  },
});

const account = computed(() => props.mailbox || {});
const saving = ref(false);
const testing = ref(false);
const testMessage = ref("");
const realtimePollInterval = ref(3);
const pendingProbeResult = ref(null);
const showStoredProbe = ref(true);
const activeEditorTab = ref("imap");
const activeFeatureTab = ref("source");
const form = reactive({
  imap: protocolModel({}),
});

const proxies = computed(() => props.detail.proxies || []);
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
  () => props.mailbox,
  () => {
    testMessage.value = "";
    pendingProbeResult.value = null;
    showStoredProbe.value = true;
    realtimePollInterval.value = normalizePollInterval(
      props.mailbox?.realtime_poll_interval,
    );
    form.imap = protocolModel(props.mailbox || {});
  },
  { immediate: true },
);

function protocolModel(item) {
  return {
    server: item.imap_server || item.service_address || "outlook.office365.com",
    port: item.imap_port || "993",
    use_ssl: truthyValue(item.use_ssl !== false),
    proxy: item.proxy_profile_id ? String(item.proxy_profile_id) : "",
  };
}

function buildFields() {
  const fields = submitFields({
    original_email: account.value.email || "",
    original_service_address:
      account.value.service_address || account.value.imap_server || "",
    email: account.value.email || "",
    service_address: form.imap.server || account.value.service_address || account.value.imap_server || "",
    auth_provider: "outlook",
    password: "",
    oauth_client_id: account.value.oauth_client_id || "",
    imap_timeout: account.value.imap_timeout || "",
    proxy_profile_id: form.imap.proxy,
    target_folder: account.value.target_folder || "INBOX",
    realtime_poll_interval: normalizePollInterval(realtimePollInterval.value),
    mailbox_protocol_tab: "imap",
    imap_enabled: true,
    imap_server: form.imap.server,
    imap_port: form.imap.port || "993",
    use_ssl: form.imap.use_ssl === "1",
  });
  if (pendingProbeResult.value) {
    fields.probe_results_json = JSON.stringify(pendingProbeResult.value);
  }
  return fields;
}

function normalizePollInterval(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 1) return 3;
  return Math.min(3600, Math.round(parsed));
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
  testMessage.value = "";
  pendingProbeResult.value = null;
  showStoredProbe.value = false;
  try {
    const result = await mailApi.testConnection(buildFields());
    pendingProbeResult.value = result.probe_results || {
      tested_at: "",
      protocol: "imap",
      items: result.items || [],
      features: result.features || {},
    };
    testMessage.value = result.message || "连接测试成功。";
  } catch (error) {
    pendingProbeResult.value = {
      tested_at: "",
      protocol: "imap",
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
    ElMessage.error(error.message || "测试失败。");
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
    message: allowed
      ? "适合历史邮件全量同步。"
      : (denied ? "服务侧可能限制历史邮件范围，同步遇到异常会写入日志。" : "尚未形成结论，保存后可重新测试。"),
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
      : (value === false ? "不支持准实时监听，会按右侧间隔检查变化。" : "尚未检测准实时能力；保存后仍按该间隔兜底检查。"),
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
      : "来源邮件按原始内容处理。",
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
    message: added
      ? "网页列表可能显示导入时间；邮件实际发生时间仍会保留。"
      : (notAdded ? "未发现目标补写到达记录。" : "尚未检测目标到达记录。"),
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
      : (clean ? "特殊内容写入后保持可校验。" : "尚未检测特殊内容处理。"),
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
  gap: 12px;
  justify-content: space-between;
  margin-bottom: 10px;
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

.feature-item-with-setting,
.feature-list-source .feature-item {
  grid-template-columns: minmax(118px, 0.9fr) 86px minmax(0, 1.35fr) minmax(178px, 0.85fr);
}

.feature-item + .feature-item {
  border-top: 1px solid var(--el-border-color-lighter);
}

.feature-item-head {
  color: var(--el-text-color-primary);
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  display: flex;
  align-items: center;
  color: var(--el-text-color-secondary);
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
  .feature-item,
  .feature-item-with-setting,
  .feature-list-source .feature-item {
    align-items: flex-start;
    grid-template-columns: minmax(0, 1fr);
  }

  .feature-value,
  .feature-setting {
    justify-self: start;
  }
}
</style>
