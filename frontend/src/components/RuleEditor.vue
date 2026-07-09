<template>
  <el-dialog
    :model-value="modelValue"
    title="规则详情"
    top="4vh"
    class="rule-editor-dialog"
    append-to-body
    destroy-on-close
    @close="close"
  >
    <el-alert
      v-if="params.get('error')"
      :title="params.get('error')"
      type="error"
      show-icon
      :closable="false"
      class="editor-section"
    />
    <el-form class="rule-detail-form" label-position="left" label-width="96px" @submit.prevent>
      <div class="editor-section">
        <h3>邮箱映射</h3>
        <div class="mapping-grid">
          <el-form-item class="mapping-field" label="来源邮箱" label-position="left">
            <el-select
              v-model="form.source_key"
              filterable
              fit-input-width
              :disabled="identityLocked"
              popper-class="mailbox-option-popper"
              style="width: 100%"
              @change="handleSourceChange"
            >
              <el-option
                v-for="account in sourceOptions"
                :key="accountKey(account)"
                :label="accountLabel(account)"
                :value="accountKey(account)"
              >
                <div class="mailbox-option" :title="accountLabel(account)">
                  <span class="mailbox-option-primary">{{ account.email || "-" }}</span>
                  <span class="mailbox-option-secondary">{{ accountService(account) || "-" }}</span>
                </div>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item
            class="mapping-field"
            label="扫描间隔"
            label-position="left"
            :error="numericErrors.sync_interval"
          >
            <el-input
              v-model="form.sync_interval"
              :placeholder="`默认 ${ruleDefaults.sync_interval}`"
              inputmode="numeric"
              @input="clearNumericError('sync_interval')"
              @blur="validateNumericField('sync_interval', '完整扫描间隔')"
            />
          </el-form-item>

          <el-form-item class="mapping-field" label="移出范围" label-position="left">
            <el-select
              v-model="form.out_of_scope_policy"
              style="width: 100%"
              @change="handleOutOfScopePolicyChange"
            >
              <el-option label="保留现状" value="keep" />
              <el-option
                label="标记标签"
                value="label"
                :disabled="!targetHasLabels"
              />
              <el-option label="同步删除" value="delete" />
            </el-select>
          </el-form-item>

          <el-form-item class="mapping-field" label="目标邮箱" label-position="left">
            <el-select
              v-model="form.target_key"
              filterable
              fit-input-width
              :disabled="identityLocked"
              popper-class="mailbox-option-popper"
              style="width: 100%"
              @change="handleTargetChange"
            >
              <el-option
                v-for="account in targetOptions"
                :key="accountKey(account)"
                :label="accountLabel(account)"
                :value="accountKey(account)"
              >
                <div class="mailbox-option" :title="accountLabel(account)">
                  <span class="mailbox-option-primary">{{ account.email || "-" }}</span>
                  <span class="mailbox-option-secondary">{{ accountService(account) || "-" }}</span>
                </div>
              </el-option>
            </el-select>
          </el-form-item>
          <el-form-item
            class="mapping-field"
            label="单轮批量"
            label-position="left"
            :error="numericErrors.batch_size"
          >
            <el-input
              v-model="form.batch_size"
              :placeholder="`默认 ${ruleDefaults.batch_size}`"
              inputmode="numeric"
              @input="clearNumericError('batch_size')"
              @blur="validateNumericField('batch_size', '单轮批量')"
            />
          </el-form-item>

          <el-form-item
            v-if="form.out_of_scope_policy === 'label'"
            class="mapping-field"
            label="标记标签"
            label-position="left"
          >
            <el-select
              v-model="form.out_of_scope_labels"
              multiple
              filterable
              collapse-tags
              collapse-tags-tooltip
              :max-collapse-tags="2"
              :placeholder="targetHasLabels ? '选择标签' : '目标邮箱未定义标签'"
              :disabled="!targetHasLabels"
              style="width: 100%"
            >
              <el-option
                v-for="label in labelOptions"
                :key="label"
                :label="label"
                :value="label"
              />
            </el-select>
          </el-form-item>
        </div>
      </div>

      <div class="editor-section">
        <h3>文件夹映射</h3>
        <div class="folder-row folder-row-header">
          <span>排除</span>
          <span>源文件夹</span>
          <span>目标文件夹</span>
          <span>绑定标签</span>
        </div>
        <div
          v-for="row in folders"
          :key="row.key"
          :class="['folder-row', { 'is-excluded': row.excluded }]"
        >
          <el-checkbox v-model="row.excluded" aria-label="排除" />
          <div class="folder-source-cell" :title="folderFullLabel(row)">
            <div class="folder-source-primary">
              <span class="folder-source-name">{{ folderPrimaryLabel(row) }}</span>
              <el-tooltip
                v-if="folderTranslationInfo(row)"
                :content="folderTranslationInfo(row)"
                placement="top"
              >
                <el-icon class="folder-translation-icon" aria-label="本地翻译">
                  <InfoFilled />
                </el-icon>
              </el-tooltip>
            </div>
            <small v-if="folderSecondaryLabel(row)">{{ folderSecondaryLabel(row) }}</small>
          </div>
          <el-select
            v-if="foldersSynced"
            v-model="row.target_folder"
            class="folder-target-select"
            filterable
            fit-input-width
            :disabled="row.excluded"
            placeholder="目标文件夹"
            popper-class="target-folder-popper"
            style="width: 100%"
          >
            <el-option
              v-for="folder in targetFolderOptions"
              :key="folder.value"
              :label="folder.label"
              :value="folder.value"
            >
              <div class="folder-option" :title="folder.label">
                <span class="folder-option-primary">{{ folder.primary }}</span>
                <span class="folder-option-secondary">{{ folder.secondary || folder.value }}</span>
              </div>
            </el-option>
          </el-select>
          <div
            v-else
            class="folder-source-cell folder-target-cell"
            :title="folderFullLabel(targetFolderDisplay(row))"
          >
            <span class="folder-source-name">{{ folderPrimaryLabel(targetFolderDisplay(row)) }}</span>
            <small v-if="folderSecondaryLabel(targetFolderDisplay(row))">
              {{ folderSecondaryLabel(targetFolderDisplay(row)) }}
            </small>
          </div>
          <el-select
            v-model="row.folder_labels"
            class="folder-label-select"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="2"
            :disabled="row.excluded || !targetHasLabels"
            :placeholder="targetHasLabels ? '绑定标签' : '目标邮箱未定义标签'"
            style="width: 100%"
          >
            <el-option
              v-for="label in labelOptions"
              :key="label"
              :label="label"
              :value="label"
            />
          </el-select>
        </div>
      </div>
    </el-form>

    <template #footer>
      <div class="form-actions">
        <el-button @click="close">取消</el-button>
        <el-button type="warning" :loading="folderLoading" @click="syncFolders">目录</el-button>
        <el-button
          type="primary"
          :loading="saving"
          @click="saveRule"
        >
          保存
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { InfoFilled } from "@element-plus/icons-vue";

import { ruleApi } from "@/api/rule";
import { appendField, submitFields } from "@/utils/forms";

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
  detail: {
    type: Object,
    default: () => ({}),
  },
  params: {
    type: Object,
    default: () => new URLSearchParams(),
  },
});

const emit = defineEmits(["update:modelValue", "navigate", "reload"]);

const saving = ref(false);
const folderLoading = ref(false);
const foldersSynced = ref(false);
const targetFolderOptions = ref([]);
const numericErrors = reactive({
  sync_interval: "",
  batch_size: "",
});
const FALLBACK_RULE_DEFAULTS = Object.freeze({
  sync_interval: "900",
  batch_size: "5",
});
const form = reactive({
  original_email: "",
  original_source_service_address: "",
  account_label: "",
  source_key: "",
  source_email: "",
  source_service_address: "",
  target_key: "",
  target_email: "",
  target_service_address: "",
  deleted_label: "",
  enabled: true,
  skip_deleted_drafts: false,
  sync_mode: "realtime",
  date_policy: "auto",
  out_of_scope_policy: "keep",
  out_of_scope_labels: [],
  sync_interval: "",
  batch_size: "",
  pop_fallback_enabled: false,
});
const folders = ref([]);

const selected = computed(() => props.detail.selected || {});
const identityLocked = computed(() => Boolean(selected.value?.has_sync_logs));
const sourceOptions = computed(() => props.detail.source_accounts || []);
const targetOptions = computed(() => props.detail.target_accounts || []);
const selectedSource = computed(() => (
  sourceOptions.value.find((account) => accountKey(account) === form.source_key)
  || sourceOptions.value[0]
  || {}
));
const selectedTarget = computed(() => (
  targetOptions.value.find((account) => accountKey(account) === form.target_key)
  || targetOptions.value[0]
  || {}
));
const labelRows = computed(() => {
  const rows = props.detail.all_labels || props.detail.labels || [];
  const targetEmail = String(form.target_email || selectedTarget.value.email || "").trim();
  const targetService = String(
    form.target_service_address || accountService(selectedTarget.value) || "",
  ).trim();
  if (!targetEmail) return [];
  return rows.filter((label) => (
    String(label.target_email || "").trim() === targetEmail
    && String(label.target_service_address || "").trim() === targetService
  ));
});
const labelOptions = computed(() => {
  const seen = new Set();
  for (const label of labelRows.value) {
    if (label.label_name) seen.add(label.label_name);
  }
  return [...seen];
});
const targetHasLabels = computed(() => labelOptions.value.length > 0);
const ruleDefaults = computed(() => ({
  sync_interval: numericDefaultValue("sync_interval"),
  batch_size: numericDefaultValue("batch_size"),
}));
const duplicateRule = computed(() => findDuplicateRule());
const duplicateRuleMessage = computed(() => (
  duplicateRule.value
    ? "该来源邮箱和目标邮箱已经存在同步规则，不能重复添加。"
    : ""
));

watch(
  () => [props.modelValue, props.detail.selected],
  () => resetForm(),
  { immediate: true },
);

watch(
  () => form.target_key,
  () => {
    foldersSynced.value = false;
    targetFolderOptions.value = mergeFolderOptions(
      [targetDefaultFolder()],
      folders.value.map((row) => row.target_folder),
      targetFolderOptions.value,
    );
    pruneFolderLabelsToTarget();
  },
);

function resetForm() {
  const item = selected.value || {};
  form.original_email = item.exists ? item.email || "" : "";
  form.original_source_service_address = item.exists
    ? item.source_service_address || item.account?.service_address || ""
    : "";
  form.account_label = "";
  form.source_key = accountKeyFor(
    item.email || sourceOptions.value[0]?.email || "",
    item.source_service_address || item.account?.service_address || "",
    sourceOptions.value,
  );
  applySourceKey();
  form.target_key = accountKeyFor(
    item.target_email || targetOptions.value[0]?.email || "",
    item.target_service_address || "",
    targetOptions.value,
  );
  applyTargetKey();
  form.deleted_label = "";
  form.enabled = item.exists && !item.enabled ? false : true;
  form.skip_deleted_drafts = Boolean(item.skip_deleted_drafts);
  form.sync_mode = item.sync_mode || "realtime";
  form.date_policy = item.date_policy || "auto";
  form.out_of_scope_policy = normalizedOutOfScopePolicy(item.out_of_scope_policy);
  form.out_of_scope_labels = normalizedFolderLabels(
    item.out_of_scope_labels ?? item.out_of_scope_label,
  );
  form.sync_interval = displayNumericValue(item.sync_interval, "sync_interval");
  form.batch_size = displayNumericValue(item.batch_size, "batch_size");
  form.pop_fallback_enabled = Boolean(item.pop_fallback_enabled);
  clearNumericErrors();
  const rows = item.folder_rules?.length ? item.folder_rules : [];
  folders.value = sortSourceFolders(rows.map((row, index) => folderModel(row, index)));
  pruneFolderLabelsToTarget();
  foldersSynced.value = false;
  resetFolderOptions();
}

function folderModel(row, index) {
  const labels = normalizedFolderLabels(row.folder_labels ?? row.folder_label);
  return {
    key: `${Date.now()}-${index}-${Math.random()}`,
    folder_name: row.folder_name || row.raw_name || "",
    display_name: row.display_name || "",
    attributes: row.attributes || [],
    target_folder: row.target_folder || targetDefaultFolder(),
    folder_labels: labels,
    excluded: Boolean(row.excluded),
    skip_uids: row.skip_uids || "",
    delete: Boolean(row.delete),
  };
}

function handleSourceChange() {
  applySourceKey();
  foldersSynced.value = false;
}

function handleTargetChange() {
  applyTargetKey();
  pruneOutOfScopeLabelToTarget();
}

function handleOutOfScopePolicyChange() {
  if (form.out_of_scope_policy !== "label") {
    form.out_of_scope_labels = [];
    return;
  }
  pruneOutOfScopeLabelToTarget();
}

function applySourceKey() {
  const account = selectedSource.value;
  form.source_email = account.email || "";
  form.source_service_address = accountService(account);
}

function applyTargetKey() {
  const account = selectedTarget.value;
  form.target_email = account.email || "";
  form.target_service_address = accountService(account);
}

function accountService(account) {
  return String(account?.service_address || account?.imap_server || "").trim();
}

function accountKey(account) {
  return accountKeyValue(account?.email, accountService(account));
}

function accountKeyValue(email, serviceAddress) {
  return `${String(email || "").trim()}\n${String(serviceAddress || "").trim()}`;
}

function accountKeyFor(email, serviceAddress, options) {
  const exact = options.find((account) => (
    String(account.email || "") === String(email || "")
    && accountService(account) === String(serviceAddress || "").trim()
  ));
  if (exact) return accountKey(exact);
  const byEmail = options.find((account) => String(account.email || "") === String(email || ""));
  if (byEmail) return accountKey(byEmail);
  return accountKeyValue(email, serviceAddress);
}

function accountLabel(account) {
  const email = String(account?.email || "").trim();
  const service = accountService(account);
  return service && service !== email ? `${email} / ${service}` : email;
}

function findDuplicateRule() {
  if (!form.source_email || !form.target_email) return null;
  const sourceIdentity = mailboxIdentityKey(
    form.source_email,
    form.source_service_address,
  );
  const targetIdentity = mailboxIdentityKey(
    form.target_email,
    form.target_service_address,
  );
  return (props.detail.rule_details || []).find((rule) => (
    !isCurrentRule(rule)
    && mailboxIdentityKey(rule.email, ruleSourceService(rule)) === sourceIdentity
    && mailboxIdentityKey(
      rule.target_email,
      rule.target_service_address,
    ) === targetIdentity
  )) || null;
}

function isCurrentRule(rule) {
  const originalEmail = String(form.original_email || "").trim();
  if (!originalEmail) return false;
  if (!form.original_source_service_address) {
    return normalizeMailboxIdentity(rule.email) === normalizeMailboxIdentity(originalEmail);
  }
  return mailboxIdentityKey(
    rule.email,
    ruleSourceService(rule),
  ) === mailboxIdentityKey(originalEmail, form.original_source_service_address);
}

function ruleSourceService(rule) {
  return rule?.source_service_address || rule?.account?.service_address || rule?.account?.imap_server || "";
}

function mailboxIdentityKey(email, serviceAddress) {
  return `${normalizeMailboxIdentity(email)}\n${normalizeMailboxIdentity(serviceAddress)}`;
}

function normalizeMailboxIdentity(value) {
  return String(value || "").trim().toLowerCase();
}

function targetDefaultFolder() {
  return selectedTarget.value.target_folder || "INBOX";
}

function resetFolderOptions() {
  targetFolderOptions.value = mergeFolderOptions(
    [targetDefaultFolder()],
    folders.value.map((row) => row.target_folder),
  );
}

function mergeFolderOptions(...groups) {
  const options = new Map();
  for (const group of groups) {
    const items = Array.isArray(group) ? group : [group];
    for (const item of items) {
      const option = folderOption(item);
      if (option && !options.has(option.value)) {
        options.set(option.value, option);
      }
    }
  }
  return [...options.values()];
}

function selectableTargetFolders(items) {
  return (items || []).filter((item) => {
    const attrs = new Set((item?.attributes || []).map((attr) => String(attr).toLowerCase()));
    return !attrs.has("\\noselect");
  });
}

function folderOption(item) {
  const value = folderRawValue(item);
  if (!value) return null;
  const primary = String(item?.primary || folderPrimaryLabel(item)).trim() || value;
  const secondary = String(item?.secondary || folderSecondaryLabel(item)).trim();
  const label = primary !== value ? `${primary} / ${value}` : primary;
  return {
    value,
    label,
    primary,
    secondary,
    slashLabel: label,
  };
}

function targetFolderDisplay(row) {
  const target = row?.target_folder || targetDefaultFolder();
  return targetFolderOptions.value.find((folder) => folder.value === target) || {
    folder_name: target,
  };
}

function targetFolderExists(value) {
  const target = String(value || "").trim();
  return Boolean(target && targetFolderOptions.value.some((folder) => folder.value === target));
}

function normalizeTargetFolder(value, sourceFolder) {
  const target = String(value || "").trim();
  if (targetFolderExists(target)) return target;
  return preferredTargetFolder(sourceFolder);
}

function folderLookupKeys(item) {
  const candidates = [
    folderRawValue(item),
    item?.display_name,
    item?.folder_name,
    item?.raw_name,
    item?.value,
    folderPrimaryLabel(item),
  ];
  const keys = new Set();
  for (const candidate of candidates) {
    const key = String(candidate || "").trim().toLowerCase();
    if (key) keys.add(key);
  }
  return [...keys];
}

function normalizedFolderLabels(value) {
  const labels = Array.isArray(value) ? value : String(value || "").split(/\r?\n/);
  const seen = new Set();
  for (const label of labels) {
    const text = String(label || "").trim();
    if (text) seen.add(text);
  }
  return [...seen];
}

function pruneFolderLabelsToTarget() {
  const allowed = targetLabelNames();
  for (const row of folders.value) {
    row.folder_labels = normalizedFolderLabels(row.folder_labels)
      .filter((label) => allowed.has(label));
  }
  pruneOutOfScopeLabelToTarget();
}

function pruneOutOfScopeLabelToTarget() {
  const allowed = targetLabelNames();
  form.out_of_scope_labels = normalizedFolderLabels(form.out_of_scope_labels)
    .filter((label) => allowed.has(label));
  if (form.out_of_scope_policy === "label" && !targetHasLabels.value) {
    form.out_of_scope_policy = "keep";
    form.out_of_scope_labels = [];
  }
}

function targetLabelNames() {
  return new Set(labelOptions.value);
}

function sortSourceFolders(rows) {
  return [...rows].sort((left, right) => {
    const primary = sourceFolderSortKey(left).localeCompare(
      sourceFolderSortKey(right),
      ["zh-Hans-CN", "en"],
      { numeric: true, sensitivity: "base" },
    );
    if (primary !== 0) return primary;
    return folderRawValue(left).localeCompare(
      folderRawValue(right),
      ["zh-Hans-CN", "en"],
      { numeric: true, sensitivity: "base" },
    );
  });
}

function sourceFolderSortKey(item) {
  return folderPrimaryLabel(item);
}

function folderRawValue(item) {
  if (item == null) return "";
  if (typeof item === "string" || typeof item === "number") {
    return String(item).trim();
  }
  return String(item.raw_name ?? item.folder_name ?? item.value ?? "").trim();
}

function folderSlashLabel(item) {
  const primary = folderPrimaryLabel(item);
  const value = folderRawValue(item);
  if (!value || primary === value) return primary;
  return `${primary} / ${value}`;
}

function folderFullLabel(item) {
  const value = folderRawValue(item);
  if (!value) return "-";
  const secondary = folderSecondaryLabel(item);
  return secondary ? `${folderPrimaryLabel(item)} ${secondary}` : folderPrimaryLabel(item);
}

function folderPrimaryLabel(item) {
  const existing = String(item?.primary || "").trim();
  if (existing) return existing;
  const value = folderRawValue(item);
  if (!value) return "-";
  const display = String(item?.display_name || "").trim();
  const localized = localizedFolderName(item);
  const decoded = decodeModifiedUtf7(value);
  if (display && display !== value) {
    return display;
  }
  if (localized && localized !== value) {
    return localized;
  }
  if (decoded && decoded !== value) {
    return decoded;
  }
  return value;
}

function folderSecondaryLabel(item) {
  const existing = String(item?.secondary || "").trim();
  if (existing) return existing;
  const value = folderRawValue(item);
  if (!value) return "";
  const display = String(item?.display_name || "").trim();
  const localized = localizedFolderName(item);
  const primary = folderPrimaryLabel(item);
  if (value !== primary) return value;
  if (display && display !== value && display !== primary) return display;
  if (localized && localized !== value && localized !== primary) return localized;
  return "";
}

function folderTranslationInfo(item) {
  const value = folderRawValue(item);
  if (!value) return "";
  const display = String(item?.display_name || "").trim();
  const localized = localizedFolderName(item);
  const decoded = decodeModifiedUtf7(value);
  if (
    localized
    && localized === folderPrimaryLabel(item)
    && localized !== value
    && (!display || display === value)
    && (!decoded || decoded === value)
  ) {
    return `“${localized}”是本地翻译；实际同步使用原始文件夹名“${value}”。`;
  }
  return "";
}

function localizedFolderName(item) {
  const value = folderRawValue(item).toLowerCase();
  const display = String(item?.display_name || "").trim().toLowerCase();
  const attrs = new Set((item?.attributes || []).map((attr) => String(attr).toLowerCase()));
  if (attrs.has("\\sent")) return "已发送";
  if (attrs.has("\\drafts")) return "草稿箱";
  if (attrs.has("\\trash")) return "已删除";
  if (attrs.has("\\junk")) return "垃圾邮件";
  if (attrs.has("\\archive")) return "归档";
  if (attrs.has("\\all")) return "所有邮件";
  if (attrs.has("\\flagged")) return "已加星标";

  const name = display || value;
  if (name === "inbox") return "收件箱";
  if (["sent", "sent mail", "sent messages", "sent items"].includes(name)) return "已发送";
  if (["draft", "drafts"].includes(name)) return "草稿箱";
  if (["trash", "deleted", "deleted items", "deleted messages"].includes(name)) return "已删除";
  if (["junk", "spam", "junk email", "junk e-mail", "junk mail"].includes(name)) return "垃圾邮件";
  if (["virus items", "virus mail"].includes(name)) return "病毒文件夹";
  if (name === "archive") return "归档";
  if (["all mail", "all"].includes(name)) return "所有邮件";
  if (["starred", "flagged"].includes(name)) return "已加星标";
  if (name === "important") return "重要邮件";
  return "";
}

function decodeModifiedUtf7(value) {
  if (!value.includes("&")) return "";
  try {
    let result = "";
    let index = 0;
    while (index < value.length) {
      const amp = value.indexOf("&", index);
      if (amp < 0) {
        result += value.slice(index);
        break;
      }
      result += value.slice(index, amp);
      const end = value.indexOf("-", amp);
      if (end < 0) return "";
      const token = value.slice(amp + 1, end);
      if (!token) {
        result += "&";
      } else {
        const base64 = token.replace(/,/g, "/");
        const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
        const bytes = atob(padded);
        if (bytes.length % 2 !== 0) return "";
        for (let i = 0; i < bytes.length; i += 2) {
          result += String.fromCharCode(
            (bytes.charCodeAt(i) << 8) + bytes.charCodeAt(i + 1),
          );
        }
      }
      index = end + 1;
    }
    return result;
  } catch {
    return "";
  }
}

function ruleParams() {
  const fields = submitFields({
    original_email: form.original_email,
    account_label: form.account_label,
    default_folder_label: "",
    deleted_label: form.deleted_label,
    enabled: form.enabled,
    skip_deleted_drafts: form.skip_deleted_drafts,
    sync_mode: form.sync_mode,
    date_policy: form.date_policy,
    out_of_scope_policy: form.out_of_scope_policy,
    source_email: form.source_email,
    source_service_address: form.source_service_address,
    target_email: form.target_email,
    target_service_address: form.target_service_address,
    sync_interval: submitNumericValue("sync_interval"),
    batch_size: submitNumericValue("batch_size"),
    pop_fallback_enabled: form.pop_fallback_enabled,
    folder_count: folders.value.length,
  });
  const outOfScopeLabels = form.out_of_scope_policy === "label"
    ? normalizedFolderLabels(form.out_of_scope_labels)
    : [""];
  outOfScopeLabels.forEach((label) => appendField(fields, "out_of_scope_label", label));
  folders.value.forEach((row, index) => {
    appendField(fields, `folder_name_${index}`, row.folder_name);
    appendField(fields, `folder_display_name_${index}`, row.display_name || "");
    const attributes = row.attributes?.length ? row.attributes : [""];
    attributes.forEach((attribute) => appendField(fields, `folder_attribute_${index}`, attribute));
    appendField(fields, `target_folder_${index}`, row.target_folder);
    const labels = row.folder_labels?.length ? row.folder_labels : [""];
    labels.forEach((label) => appendField(fields, `folder_label_${index}`, label));
    appendField(fields, `folder_skip_uids_${index}`, row.skip_uids || "");
    if (row.excluded) appendField(fields, `folder_excluded_${index}`, "1");
    if (row.delete) appendField(fields, `folder_delete_${index}`, "1");
  });
  return fields;
}

async function saveRule() {
  if (!validateDuplicateRule()) return;
  if (!validateOutOfScopePolicy()) return;
  if (!validateNumericFields()) return;
  saving.value = true;
  try {
    const result = await ruleApi.save(ruleParams());
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "保存失败。");
  } finally {
    saving.value = false;
  }
}

function validateOutOfScopePolicy() {
  if (form.out_of_scope_policy !== "label") return true;
  if (!targetHasLabels.value) {
    ElMessage.warning("目标邮箱未定义标签，不能选择标记标签。");
    return false;
  }
  if (!normalizedFolderLabels(form.out_of_scope_labels).length) {
    ElMessage.warning("请选择移出范围后的标记标签。");
    return false;
  }
  return true;
}

function normalizedOutOfScopePolicy(value) {
  return ["keep", "label", "delete"].includes(value) ? value : "keep";
}

function validateDuplicateRule() {
  if (!duplicateRule.value) return true;
  ElMessage.warning(duplicateRuleMessage.value);
  return false;
}

function clearNumericError(name) {
  numericErrors[name] = "";
}

function clearNumericErrors() {
  numericErrors.sync_interval = "";
  numericErrors.batch_size = "";
}

function numericDefaultValue(name) {
  const value = props.detail.defaults?.[name] ?? FALLBACK_RULE_DEFAULTS[name];
  const text = String(value || "").trim();
  if (/^[1-9]\d*$/.test(text)) return text;
  return FALLBACK_RULE_DEFAULTS[name];
}

function displayNumericValue(value, name) {
  const text = String(value ?? "").trim();
  return text || numericDefaultValue(name);
}

function submitNumericValue(name) {
  const value = String(form[name] || "").trim();
  return value === numericDefaultValue(name) ? "" : value;
}

function validateNumericField(name, label) {
  const value = String(form[name] || "").trim();
  form[name] = value;
  if (!value) {
    numericErrors[name] = "";
    return true;
  }
  if (!/^[1-9]\d*$/.test(value)) {
    numericErrors[name] = `${label}必须是大于 0 的整数`;
    return false;
  }
  numericErrors[name] = "";
  return true;
}

function validateNumericFields() {
  const fields = [
    ["sync_interval", "完整扫描间隔"],
    ["batch_size", "单轮批量"],
  ];
  const results = fields.map(([name, label]) => validateNumericField(name, label));
  if (results.every(Boolean)) return true;
  ElMessage.warning(Object.values(numericErrors).find(Boolean) || "请检查数字配置。");
  return false;
}

async function toggleRule() {
  try {
    const result = await ruleApi.toggle({
      source_email: selected.value.email || form.source_email,
      enabled: selected.value.enabled ? "" : "1",
    });
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "操作失败。");
  }
}

async function deleteRule() {
  try {
    await ElMessageBox.confirm("确定删除这条同步规则吗？", "删除规则", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    const result = await ruleApi.delete({ source_email: selected.value.email || form.source_email });
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    if (error !== "cancel") ElMessage.error(error.message || "删除失败。");
  }
}

async function syncFolders() {
  if (!form.source_email || !form.target_email) {
    ElMessage.warning("请先选择源邮箱和目标邮箱。");
    return;
  }
  folderLoading.value = true;
  try {
    const [sourceResult, targetResult] = await Promise.all([
      ruleApi.folders({
        role: "source",
        email: form.source_email,
        service_address: form.source_service_address,
      }),
      ruleApi.folders({
        role: "target",
        email: form.target_email,
        service_address: form.target_service_address,
      }),
    ]);
    const refreshedTargetOptions = mergeFolderOptions(
      selectableTargetFolders(targetResult.folders || []),
    );
    targetFolderOptions.value = refreshedTargetOptions;
    const existingRows = new Map();
    for (const row of folders.value) {
      for (const key of folderLookupKeys(row)) {
        if (!existingRows.has(key)) existingRows.set(key, row);
      }
    }
    folders.value = sortSourceFolders((sourceResult.folders || [])
      .map((folder, index) => {
        const rawName = folder.raw_name || folder.display_name || "";
        if (!rawName) return null;
        const existing = folderLookupKeys(folder)
          .map((key) => existingRows.get(key))
          .find(Boolean);
        return folderModel({
          ...existing,
          folder_labels: existing?.folder_labels || existing?.folder_label,
          excluded: existing?.excluded || false,
          skip_uids: existing?.skip_uids || "",
          target_folder: normalizeTargetFolder(existing?.target_folder, rawName),
          display_name: folder.display_name || existing?.display_name || "",
          attributes: folder.attributes || existing?.attributes || [],
          folder_name: rawName,
        }, index);
      })
      .filter(Boolean));
    targetFolderOptions.value = refreshedTargetOptions;
    foldersSynced.value = true;
    ElMessage.success(`文件夹同步完成，已按源邮箱生成 ${folders.value.length} 行。`);
  } catch (error) {
    ElMessage.error(error.message || "同步失败。");
  } finally {
    folderLoading.value = false;
  }
}

function preferredTargetFolder(sourceFolder) {
  if (targetFolderExists(sourceFolder)) {
    return sourceFolder;
  }
  if (targetFolderExists(targetDefaultFolder())) {
    return targetDefaultFolder();
  }
  return targetFolderOptions.value[0]?.value || targetDefaultFolder();
}

function close() {
  emit("update:modelValue", false);
}
</script>

<style scoped>
.editor-section {
  margin-bottom: 4px;
}

.editor-section h3 {
  margin: 0 0 12px;
  font-size: 16px;
}

:global(.rule-editor-dialog) {
  width: min(1280px, 98vw);
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

:global(.rule-editor-dialog .el-dialog__header),
:global(.rule-editor-dialog .el-dialog__footer) {
  flex: 0 0 auto;
}

:global(.rule-editor-dialog .el-dialog__body) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding-top: 8px;
}

:global(.rule-editor-dialog .el-dialog__footer) {
  border-top: 1px solid var(--line);
  background: var(--surface);
  padding: 12px 24px;
}

.rule-detail-form :deep(.el-form-item) {
  margin-bottom: 4px;
}

.rule-detail-form {
  --rule-control-height: 32px;
}

.rule-detail-form :deep(.el-input) {
  --el-input-height: var(--rule-control-height);
}

.rule-detail-form :deep(.el-input__wrapper),
.rule-detail-form :deep(.el-select__wrapper) {
  height: var(--rule-control-height);
  min-height: var(--rule-control-height);
}

.rule-detail-form :deep(.el-form-item__label) {
  min-height: var(--rule-control-height);
  line-height: var(--rule-control-height);
  padding-right: 10px;
  justify-content: flex-end;
}

.rule-detail-form :deep(.el-form-item__content) {
  min-width: 0;
}

.mapping-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(132px, 168px) minmax(0, 1fr);
  column-gap: 12px;
  row-gap: 8px;
  align-items: start;
  padding: 0 8px;
}

.mapping-field {
  min-width: 0;
}

.mapping-field :deep(.el-form-item__label) {
  width: 64px !important;
  justify-content: flex-start;
  align-items: center;
  min-height: var(--rule-control-height);
  line-height: var(--rule-control-height);
  padding: 0 6px 0 0;
  color: #5f6877;
  font-size: 12px;
  font-weight: 600;
}

.mapping-field :deep(.el-form-item__content) {
  width: 100%;
}

.folder-row {
  display: grid;
  grid-template-columns: 52px 150px minmax(100px, 1.1fr) minmax(300px, 2fr);
  gap: 10px;
  align-items: center;
  margin-bottom: 4px;
}

.folder-row:not(.folder-row-header) {
  min-height: 38px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: 2px;
}

.folder-row-header {
  margin-bottom: 4px;
  padding: 0 8px;
  color: #5f6877;
  font-size: 12px;
  font-weight: 600;
}

.folder-row.is-excluded {
  color: #8b94a3;
  background: #f0f2f5;
  border-color: #e2e6ec;
}

.folder-source-cell {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.folder-target-cell {
  min-height: var(--rule-control-height);
  align-content: center;
}

.folder-source-primary {
  display: flex;
  align-items: flex-start;
  min-width: 0;
}

.folder-source-name,
.folder-source-cell small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-source-name {
  color: #3f4755;
  line-height: 16px;
  min-width: 0;
}

.folder-translation-icon {
  flex: 0 0 auto;
  margin-left: 3px;
  margin-top: -2px;
  color: var(--muted);
  cursor: help;
  font-size: 11px;
  opacity: 0.82;
}

.folder-source-cell small {
  color: var(--muted);
  font-size: 11px;
  line-height: 14px;
}

.folder-row.is-excluded .folder-source-name,
.folder-row.is-excluded .folder-source-cell small {
  color: #8b94a3;
}

.folder-label-select :deep(.el-select__wrapper) {
  overflow: hidden;
}

.folder-label-select :deep(.el-select__selection) {
  min-width: 0;
  flex-wrap: nowrap;
}

.folder-target-select :deep(.el-select__wrapper) {
  min-height: var(--rule-control-height);
}

.folder-row :deep(.el-select__selected-item),
.folder-row :deep(.el-select__placeholder) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:global(.target-folder-popper) {
  max-width: calc(100vw - 48px);
}

:global(.mailbox-option-popper) {
  max-width: calc(100vw - 48px);
}

:global(.mailbox-option-popper .el-select-dropdown) {
  max-width: 100%;
}

:global(.mailbox-option-popper .el-select-dropdown__item) {
  height: 34px;
  padding: 0 16px;
  overflow: hidden;
  line-height: 34px;
}

:global(.mailbox-option-popper .mailbox-option) {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
  column-gap: 28px;
  align-items: center;
  width: 100%;
  height: 34px;
  min-width: 0;
  font-size: 13px;
  line-height: 34px;
}

:global(.mailbox-option-popper .mailbox-option-primary),
:global(.mailbox-option-popper .mailbox-option-secondary) {
  overflow: hidden;
  line-height: 34px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:global(.mailbox-option-popper .mailbox-option-primary) {
  color: #4f5867;
}

:global(.mailbox-option-popper .mailbox-option-secondary) {
  color: #8b94a3;
  text-align: right;
}

:global(.mailbox-option-popper .el-select-dropdown__item.is-selected .mailbox-option-primary) {
  color: var(--primary);
  font-weight: 700;
}

:global(.mailbox-option-popper .el-select-dropdown__item.is-selected .mailbox-option-secondary) {
  color: #8b94a3;
  font-weight: 600;
}

:global(.target-folder-popper .el-select-dropdown) {
  max-width: 100%;
}

:global(.target-folder-popper .el-select-dropdown__item) {
  height: 34px;
  padding: 0 16px;
  overflow: hidden;
  line-height: 34px;
}

:global(.target-folder-popper .folder-option) {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  column-gap: 28px;
  align-items: center;
  width: 100%;
  height: 34px;
  min-width: 0;
  font-size: 13px;
  line-height: 34px;
}

:global(.target-folder-popper .folder-option-primary),
:global(.target-folder-popper .folder-option-secondary) {
  overflow: hidden;
  line-height: 34px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

:global(.target-folder-popper .folder-option-primary) {
  color: #4f5867;
}

:global(.target-folder-popper .folder-option-secondary) {
  color: #8b94a3;
  text-align: right;
}

:global(.target-folder-popper .el-select-dropdown__item.is-selected .folder-option-primary) {
  color: var(--primary);
  font-weight: 700;
}

:global(.target-folder-popper .el-select-dropdown__item.is-selected .folder-option-secondary) {
  color: #8b94a3;
  font-weight: 600;
}

@media (max-width: 900px) {
  .mapping-grid {
    grid-template-columns: 1fr;
  }

  .folder-row {
    grid-template-columns: 1fr;
  }

  .folder-row-header {
    display: none;
  }
}
</style>
