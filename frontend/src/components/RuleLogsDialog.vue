<template>
  <el-dialog
    v-model="dialogOpen"
    :title="dialogTitle"
    top="4vh"
    class="sync-log-modal"
    append-to-body
    destroy-on-close
  >
    <div class="sync-log-dialog" v-loading="loading">
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
      />
      <template v-else>
        <div class="sync-log-summary">
          <el-tag type="success" effect="plain"
            >同步成功 {{ pagination.success.total }}</el-tag
          >
          <el-tag
            :type="pagination.failed.total ? 'danger' : 'info'"
            effect="plain"
          >
            同步失败 {{ pagination.failed.total }}
          </el-tag>
          <!-- <el-tag class="muted log-email" type="warning">
            {{ sourceEmail }}
          </el-tag>
          <el-tag type="warning">
            {{ targetEmail }}
          </el-tag> -->
        </div>
        <div class="sync-log-toolbar">
          <span class="sync-log-filter-label">过滤条件</span>
          <el-input
            v-model="filterKeyword"
            class="sync-log-filter"
            placeholder="主题 / UID / 邮箱 / 标签"
            clearable
            @keyup.enter="applyFilter"
            @clear="applyFilter"
          />
          <el-button
            type="primary"
            :icon="Search"
            :disabled="loading"
            @click="applyFilter"
          >
            查询
          </el-button>
          <el-button :icon="Refresh" :disabled="loading" @click="refreshLogs">
            刷新
          </el-button>
          <el-button
            type="danger"
            plain
            :icon="Delete"
            :disabled="!activeSelectedCount || loading || deleting"
            :loading="deleting"
            @click="deleteSelectedLogs"
          >
            删除
          </el-button>
        </div>
        <el-tabs v-model="activeTab">
          <el-tab-pane label="同步成功" name="success">
            <el-table
              :data="logs.success"
              :row-key="logRowKey"
              empty-text="暂无成功记录"
              @selection-change="
                (rows) => handleSelectionChange('success', rows)
              "
            >
              <el-table-column
                type="selection"
                width="48"
                :selectable="isSelectableLog"
              />
              <el-table-column label="时间" min-width="150">
                <template #default="{ row }">
                  <div class="log-multiline-cell" :title="mailTimeText(row)">
                    <span>{{ datePart(mailTimeText(row)) }}</span>
                    <small v-if="timePart(mailTimeText(row))">{{
                      timePart(mailTimeText(row))
                    }}</small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="来源邮箱" min-width="200">
                <template #default="{ row }">
                  <div
                    class="log-mailbox-cell"
                    :title="sourceMailboxTitle(row)"
                  >
                    <span>{{ row.source_email || '-' }}</span>
                    <small>{{ folderFullLabel(row.source_folder) }}</small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="目标邮箱" min-width="200">
                <template #default="{ row }">
                  <div
                    class="log-mailbox-cell"
                    :title="targetMailboxTitle(row)"
                  >
                    <span>{{ row.target_email || '-' }}</span>
                    <small v-if="targetFolderLine(row)">
                      {{ targetFolderLine(row) }}
                    </small>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="UID" width="140">
                <template #default="{ row }">
                  <div class="log-multiline-cell">
                    <span>来源：{{ sourceUidText(row) }}</span>

                    <small v-if="targetFolderLine(row)">
                      目标：{{ targetUidText(row) }}
                    </small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column
                prop="sync_source"
                label="同步方式"
                width="100"
              />

              <el-table-column label="邮件主题" min-width="180">
                <template #default="{ row }">
                  <span class="log-subject-cell" :title="subjectText(row)">
                    {{ subjectText(row) }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column label="备注" min-width="190">
                <template #default="{ row }">
                  <span>{{ logStatusText(row, 'success') }}</span>

                  <div
                    class="log-multiline-cell log-remark-cell"
                    :title="remarkText(row)"
                  >
                    <span v-for="remark in remarkLines(row)" :key="remark">
                      {{ remark }}
                    </span>
                  </div>
                </template>
              </el-table-column>
            </el-table>
            <div class="sync-log-footer">
              <el-pagination
                v-model:current-page="pagination.success.page"
                v-model:page-size="pageSize"
                :page-sizes="pageSizeOptions"
                :total="pagination.success.total"
                layout="total, prev, pager, next, sizes, jumper"
                @current-change="loadCurrentLogs"
                @size-change="changePageSize"
              />
            </div>
          </el-tab-pane>
          <el-tab-pane label="同步失败" name="failed">
            <el-table
              :data="logs.failed"
              :row-key="logRowKey"
              empty-text="暂无失败记录"
              @selection-change="
                (rows) => handleSelectionChange('failed', rows)
              "
            >
              <el-table-column
                type="selection"
                width="48"
                :selectable="isSelectableLog"
              />
              <el-table-column label="时间" min-width="150">
                <template #default="{ row }">
                  <div class="log-multiline-cell" :title="mailTimeText(row)">
                    <span>{{ datePart(mailTimeText(row)) }}</span>
                    <small v-if="timePart(mailTimeText(row))">{{
                      timePart(mailTimeText(row))
                    }}</small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="来源邮箱" min-width="200">
                <template #default="{ row }">
                  <div
                    class="log-mailbox-cell"
                    :title="sourceMailboxTitle(row)"
                  >
                    <span>{{ row.source_email || '-' }}</span>
                    <small>{{ folderFullLabel(row.source_folder) }}</small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="目标邮箱" min-width="200">
                <template #default="{ row }">
                  <div
                    class="log-mailbox-cell"
                    :title="targetMailboxTitle(row)"
                  >
                    <span>{{ row.target_email || '-' }}</span>
                    <small v-if="targetFolderLine(row)">
                      {{ targetFolderLine(row) }}
                    </small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="UID" width="140">
                <template #default="{ row }">
                  <div class="log-multiline-cell">
                    <span>来源：{{ sourceUidText(row) }}</span>

                    <small v-if="targetFolderLine(row)">
                      目标：{{ targetUidText(row) }}
                    </small>
                  </div>
                </template>
              </el-table-column>

              <el-table-column
                prop="sync_source"
                label="同步方式"
                width="100"
              />

              <el-table-column label="邮件主题" min-width="240">
                <template #default="{ row }">
                  <span class="log-subject-cell" :title="subjectText(row)">
                    {{ subjectText(row) }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column label="备注" min-width="190">
                <template #default="{ row }">
                  <span>{{ logStatusText(row, 'failed') }}</span>
                  <div
                    class="log-multiline-cell log-remark-cell"
                    :title="remarkText(row)"
                  >
                    <span v-for="remark in remarkLines(row)" :key="remark">
                      {{ remark }}
                    </span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column label="失败原因" min-width="300">
                <template #default="{ row }">
                  <span class="log-error">{{
                    failureReasonText(row, 'failed')
                  }}</span>
                </template>
              </el-table-column>
            </el-table>
            <div class="sync-log-footer">
              <el-pagination
                v-model:current-page="pagination.failed.page"
                v-model:page-size="pageSize"
                :page-sizes="pageSizeOptions"
                :total="pagination.failed.total"
                layout="total, prev, pager, next, sizes, jumper"
                @current-change="loadCurrentLogs"
                @size-change="changePageSize"
              />
            </div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </div>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Delete, Refresh, Search } from '@element-plus/icons-vue';

import { ruleApi } from '@/api/rule';

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
  sourceEmail: {
    type: String,
    default: '',
  },
  targetEmail: {
    type: String,
    default: '',
  },
});

const emit = defineEmits(['update:modelValue']);

const loading = ref(false);
const deleting = ref(false);
const error = ref('');
const activeTab = ref('success');
const filterKeyword = ref('');
const appliedKeyword = ref('');
const pageSizeOptions = [10, 20, 50, 100];
const pageSize = ref(10);
const logs = ref({
  success: [],
  failed: [],
});
const selectedLogs = ref({
  success: [],
  failed: [],
});
const pagination = ref({
  success: {
    page: 1,
    total: 0,
  },
  failed: {
    page: 1,
    total: 0,
  },
});
let requestId = 0;

const dialogOpen = computed({
  get() {
    return props.modelValue;
  },
  set(value) {
    emit('update:modelValue', value);
  },
});
const dialogTitle = computed(() => (props.sourceEmail = '近期同步日志'));
const activeSelectedCount = computed(
  () => selectedContentHashes(activeTab.value).length,
);

watch(
  () => [props.modelValue, props.sourceEmail],
  ([open, sourceEmail], [wasOpen, previousSourceEmail] = []) => {
    if (!open || !sourceEmail) return;
    const reset = !wasOpen || sourceEmail !== previousSourceEmail;
    if (reset) {
      resetPagination();
      resetFilter();
      clearSelectedLogs();
    }
    loadLogs(sourceEmail, { selectDefaultTab: reset });
  },
  { immediate: true },
);

async function loadLogs(sourceEmail, options = {}) {
  const currentRequest = ++requestId;
  loading.value = true;
  error.value = '';
  try {
    const result = await ruleApi.logs(sourceEmail, {
      success_page: pagination.value.success.page,
      failed_page: pagination.value.failed.page,
      page_size: pageSize.value,
      keyword: appliedKeyword.value,
    });
    if (currentRequest !== requestId) return;
    const success = Array.isArray(result?.success) ? result.success : [];
    const failed = Array.isArray(result?.failed) ? result.failed : [];
    logs.value = { success, failed };
    clearSelectedLogs();
    syncPagination(result?.pagination);
    if (options.selectDefaultTab) {
      activeTab.value = pagination.value.failed.total ? 'failed' : 'success';
    }
  } catch (loadError) {
    if (currentRequest === requestId) {
      error.value = loadError.message || '日志加载失败。';
    }
  } finally {
    if (currentRequest === requestId) {
      loading.value = false;
    }
  }
}

function loadCurrentLogs() {
  if (props.sourceEmail) loadLogs(props.sourceEmail);
}

function refreshLogs() {
  loadCurrentLogs();
}

function applyFilter() {
  appliedKeyword.value = filterKeyword.value.trim();
  resetPagination();
  loadCurrentLogs();
}

function changePageSize(value) {
  pageSize.value = value;
  resetPagination();
  loadCurrentLogs();
}

function resetPagination() {
  pagination.value = {
    success: {
      page: 1,
      total: 0,
    },
    failed: {
      page: 1,
      total: 0,
    },
  };
}

function resetFilter() {
  filterKeyword.value = '';
  appliedKeyword.value = '';
}

function syncPagination(nextPagination) {
  pagination.value = {
    success: paginationInfo('success', nextPagination, logs.value.success),
    failed: paginationInfo('failed', nextPagination, logs.value.failed),
  };
}

function paginationInfo(key, nextPagination, rows) {
  const value = nextPagination?.[key] || {};
  return {
    page: Number(value.page || pagination.value[key].page || 1),
    total: Number(value.total ?? rows.length),
  };
}

function handleSelectionChange(tab, rows) {
  selectedLogs.value = {
    ...selectedLogs.value,
    [tab]: rows,
  };
}

function clearSelectedLogs(tab) {
  if (tab) {
    selectedLogs.value = {
      ...selectedLogs.value,
      [tab]: [],
    };
    return;
  }
  selectedLogs.value = {
    success: [],
    failed: [],
  };
}

function selectedContentHashes(tab) {
  return (selectedLogs.value[tab] || [])
    .map((row) => String(row.content_hash || '').trim())
    .filter(Boolean);
}

function logRowKey(row) {
  return String(
    row.content_hash ||
      `${row.source_email || ''}:${row.source_uid || ''}:${row.updated_at || ''}`,
  );
}

function isSelectableLog(row) {
  return Boolean(String(row.content_hash || '').trim());
}

async function deleteSelectedLogs() {
  const tab = activeTab.value;
  const hashes = selectedContentHashes(tab);
  if (!hashes.length) {
    ElMessage.warning('请先勾选日志。');
    return;
  }
  try {
    await ElMessageBox.confirm(
      `确定删除已勾选的 ${hashes.length} 条日志吗？`,
      '删除日志',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      },
    );
  } catch (confirmError) {
    if (!['cancel', 'close'].includes(confirmError)) {
      ElMessage.error(confirmError.message || '删除取消。');
    }
    return;
  }

  deleting.value = true;
  try {
    await ruleApi.deleteLogs({
      source_email: props.sourceEmail,
      success_hashes: tab === 'success' ? hashes : [],
      failed_hashes: tab === 'failed' ? hashes : [],
    });
    if (
      hashes.length >= logs.value[tab].length &&
      pagination.value[tab].page > 1
    ) {
      pagination.value[tab].page -= 1;
    }
    clearSelectedLogs(tab);
    ElMessage.success('已删除选中日志。');
    loadCurrentLogs();
  } catch (deleteError) {
    ElMessage.error(deleteError.message || '删除失败。');
  } finally {
    deleting.value = false;
  }
}

function pendingStatusText(status) {
  if (status === 'quarantined') return '同步失败';
  if (status === 'pending') return '待恢复';
  return status || '失败';
}

function logStatusText(row, tab) {
  if (tab === 'success') return '同步成功';
  return pendingStatusText(row.status);
}

function failureReasonText(row, tab) {
  if (tab === 'success') return '-';
  if (row.failure_reason) return row.failure_reason;
  return logErrorText(row);
}

function targetUidText(row) {
  return String(row.target_uid || row.new_target_uid || '').trim() || '-';
}

function sourceUidText(row) {
  return String(row.source_uid || '').trim() || '-';
}

function mailTimeText(row) {
  return String(row.internal_date || row.updated_at || '').trim() || '-';
}

function logErrorText(row) {
  if (row.last_error) return row.last_error;
  return row.status === 'pending' ? '等待恢复同步' : '同步失败';
}

function subjectText(row) {
  return String(row.subject || '').trim() || '-';
}

function sourceMailboxTitle(row) {
  return (
    [row.source_email, folderFullLabel(row.source_folder)]
      .filter(Boolean)
      .join(' / ') || '-'
  );
}

function targetMailboxTitle(row) {
  return (
    [row.target_email, targetFolderLine(row)].filter(Boolean).join(' / ') || '-'
  );
}

function targetFolderLine(row) {
  return folderFullLabel(row.target_folder);
}

function labelText(row) {
  return labelLines(row).join(' / ');
}

function labelLines(row) {
  const labels = [];
  if (row.account_label) labels.push(row.account_label);
  for (const label of folderLabels(row)) {
    if (label && !labels.includes(label)) labels.push(label);
  }
  return labels.length ? labels : ['-'];
}

function folderLabels(row) {
  if (Array.isArray(row.folder_labels)) return row.folder_labels;
  if (row.folder_labels_json) {
    try {
      const labels = JSON.parse(row.folder_labels_json);
      if (Array.isArray(labels)) return labels;
    } catch {
      return row.folder_label ? [row.folder_label] : [];
    }
  }
  return row.folder_label ? [row.folder_label] : [];
}

function remarkText(row) {
  return remarkLines(row).join(' / ');
}

function remarkLines(row) {
  const remarks = normalizeTextList(row.remarks);
  if (!remarks.length && row.remarks_json) {
    try {
      remarks.push(...normalizeTextList(JSON.parse(row.remarks_json)));
    } catch {
      // Ignore malformed remark payloads and fall back to the plain field.
    }
  }
  if (!remarks.length) remarks.push(...normalizeTextList(row.remark));
  return remarks.length ? remarks : ['-'];
}

function normalizeTextList(value) {
  const values = Array.isArray(value)
    ? value
    : String(value || '').split(/\r?\n|\/|,/);
  const result = [];
  for (const item of values) {
    const text = String(item || '').trim();
    if (text && !result.includes(text)) result.push(text);
  }
  return result;
}

function datePart(value) {
  const text = String(value || '').trim();
  if (!text) return '-';
  return text.split(/[T ]/)[0] || text;
}

function timePart(value) {
  const text = String(value || '').trim();
  const match = text.match(/[T ](\d{2}:\d{2}:\d{2})/);
  return match?.[1] || '';
}

function folderFullLabel(item) {
  const value = folderRawValue(item);
  if (!value) return '-';
  const secondary = folderSecondaryLabel(item);
  return secondary
    ? `${folderPrimaryLabel(item)} ${secondary}`
    : folderPrimaryLabel(item);
}

function folderPrimaryLabel(item) {
  const existing = String(item?.primary || '').trim();
  if (existing) return existing;
  const value = folderRawValue(item);
  if (!value) return '-';
  const display = String(item?.display_name || '').trim();
  const localized = localizedFolderName(item);
  const decoded = decodeModifiedUtf7(value);
  if (display && display !== value) return display;
  if (localized && localized !== value) return localized;
  if (decoded && decoded !== value) return decoded;
  return value;
}

function folderSecondaryLabel(item) {
  const existing = String(item?.secondary || '').trim();
  if (existing) return existing;
  const value = folderRawValue(item);
  if (!value) return '';
  const display = String(item?.display_name || '').trim();
  const localized = localizedFolderName(item);
  const primary = folderPrimaryLabel(item);
  if (value !== primary) return value;
  if (display && display !== value && display !== primary) return display;
  if (localized && localized !== value && localized !== primary)
    return localized;
  return '';
}

function folderRawValue(item) {
  if (item == null) return '';
  if (typeof item === 'string' || typeof item === 'number') {
    return String(item).trim();
  }
  return String(item.raw_name ?? item.folder_name ?? item.value ?? '').trim();
}

function localizedFolderName(item) {
  const value = folderRawValue(item).toLowerCase();
  const display = String(item?.display_name || '')
    .trim()
    .toLowerCase();
  const attrs = new Set(
    (item?.attributes || []).map((attr) => String(attr).toLowerCase()),
  );
  if (attrs.has('\\sent')) return '已发送';
  if (attrs.has('\\drafts')) return '草稿箱';
  if (attrs.has('\\trash')) return '已删除';
  if (attrs.has('\\junk')) return '垃圾邮件';
  if (attrs.has('\\archive')) return '归档';
  if (attrs.has('\\all')) return '所有邮件';
  if (attrs.has('\\flagged')) return '已加星标';

  const name = display || value;
  if (name === 'inbox') return '收件箱';
  if (['sent', 'sent mail', 'sent messages'].includes(name)) return '已发送';
  if (['draft', 'drafts'].includes(name)) return '草稿箱';
  if (['trash', 'deleted', 'deleted items', 'deleted messages'].includes(name))
    return '已删除';
  if (['junk', 'spam', 'junk email'].includes(name)) return '垃圾邮件';
  if (name === 'archive') return '归档';
  if (['all mail', 'all'].includes(name)) return '所有邮件';
  if (['starred', 'flagged'].includes(name)) return '已加星标';
  if (name === 'important') return '重要邮件';
  return '';
}

function decodeModifiedUtf7(value) {
  if (!value.includes('&')) return '';
  try {
    let result = '';
    let index = 0;
    while (index < value.length) {
      const amp = value.indexOf('&', index);
      if (amp < 0) {
        result += value.slice(index);
        break;
      }
      result += value.slice(index, amp);
      const end = value.indexOf('-', amp);
      if (end < 0) return '';
      const token = value.slice(amp + 1, end);
      if (!token) {
        result += '&';
      } else {
        const base64 = token.replace(/,/g, '/');
        const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
        const bytes = atob(padded);
        if (bytes.length % 2 !== 0) return '';
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
    return '';
  }
}
</script>

<style scoped>
:global(.sync-log-modal) {
  width: min(1960px, 98vw);
}

:global(.sync-log-modal .el-dialog__body) {
  max-height: calc(92vh - 72px);
  max-width: calc(100vw - 32px);
  overflow-y: auto;
}

.sync-log-dialog {
  min-height: 240px;
}

.sync-log-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.sync-log-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.sync-log-filter-label {
  color: #3f4755;
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
}

.sync-log-filter {
  width: min(360px, 100%);
}

.sync-log-footer {
  display: flex;
  justify-content: flex-end;
  min-height: 42px;
  padding-top: 10px;
}

.log-mailbox-cell,
.log-multiline-cell {
  display: grid;
  gap: 1px;
  min-width: 0;
}

.log-mailbox-cell span,
.log-subject-cell,
.log-mailbox-cell small,
.log-multiline-cell span,
.log-multiline-cell small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.log-mailbox-cell span,
.log-subject-cell,
.log-multiline-cell span {
  color: #3f4755;
  line-height: 16px;
}

.log-remark-cell span {
  color: #7a4d00;
}

.log-mailbox-cell small,
.log-multiline-cell small {
  color: var(--muted);
  font-size: 11px;
  line-height: 14px;
}

.log-email {
  margin-left: 15px;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.log-error {
  color: #b42318;
}

@media (max-width: 760px) {
  .sync-log-toolbar {
    align-items: stretch;
  }

  .sync-log-filter {
    width: 100%;
  }

  .log-email {
    width: 100%;
    margin-left: 0;
  }
}
</style>
