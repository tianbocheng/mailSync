<template>
  <section class="page">
    <el-card class="page-card" shadow="never">
      <div class="page-heading">
        <h2>邮箱账号管理</h2>
      </div>
      <div class="toolbar">
        <div class="toolbar-actions">
          <el-button type="primary" @click="openDialog('new')">手动录入</el-button>
          <el-button :loading="outlookBusy" @click="startOutlookLogin">Outlook 登录</el-button>
        </div>
        <el-form class="filter-row" inline @submit.prevent>
          <el-form-item label="邮箱">
            <el-input v-model="query" clearable placeholder="请输入" style="width: 220px" @keyup.enter="applyFilter" />
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
      <el-table :data="pagedMailboxes" empty-text="暂无邮箱" row-key="service_address">
        <el-table-column label="邮箱地址" min-width="240">
          <template #default="{ row }">
            <div class="strong-cell">
              <strong>{{ row.email || "未配置" }}</strong>
              <small>{{ mailboxSubtitle(row) }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="服务地址" min-width="180">
          <template #default="{ row }">
            {{ isOutlookMailbox(row) ? "-" : mailboxServiceText(row) }}
          </template>
        </el-table-column>
        <el-table-column prop="protocol_summary" label="协议" min-width="140" />
        <el-table-column label="标签" min-width="100">
          <template #default="{ row }">{{ row.label_count || 0 }} 个</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button link type="primary" @click="openMailbox(row, isOutlookMailbox(row) ? 'outlook' : 'edit')">
                {{ isOutlookMailbox(row) ? "查看" : "编辑" }}
              </el-button>
              <el-button link type="primary" @click="openMailbox(row, 'labels')">标签</el-button>
              <el-button
                link
                type="danger"
                :disabled="row.is_rule_bound"
                @click="deleteMailbox(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div class="table-footer">
        <el-pagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="filteredMailboxes.length"
          layout="total, prev, pager, next, sizes, jumper"
        />
      </div>
    </el-card>

    <el-dialog
      :model-value="dialogVisible"
      :title="dialogTitle"
      width="860px"
      append-to-body
      destroy-on-close
      @close="closeDialog"
    >
      <LabelsPanel
        v-if="dialog === 'labels'"
        :detail="detail"
        @reload="emit('reload')"
      />
      <OutlookPanel
        v-else-if="dialog === 'outlook' || (dialog === 'edit' && isOutlookMailbox(mailbox))"
        :detail="detail"
        :mailbox="mailbox"
      />
      <MailboxForm
        v-else
        :detail="detail"
        :mailbox="mailbox"
        :is-new="dialog === 'new'"
      />
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, h, onUnmounted, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import LabelsPanel from "@/components/LabelsPanel.vue";
import MailboxForm from "@/components/MailboxForm.vue";
import OutlookPanel from "@/components/OutlookPanel.vue";
import { mailApi } from "@/api/mail";
import { paramsFromSearch } from "@/utils/route";
import { selectedMailbox } from "@/utils/forms";

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

const query = ref("");
const currentPage = ref(1);
const pageSize = ref(10);
const outlookLoading = ref(false);
const outlookCompleting = ref(false);
const outlookResult = ref(null);
let outlookPopup = null;
let outlookTimer = null;
let outlookMessageHandler = null;

const detail = computed(() => props.data?.detail || {});
const params = computed(() => paramsFromSearch(props.search));
const dialog = computed(() => params.value.get("dialog") || detail.value.dialog || "");
const dialogVisible = computed(() => Boolean(dialog.value));
const mailbox = computed(() => selectedMailbox(detail.value, params.value) || {});
const dialogTitle = computed(() => {
  if (dialog.value === "labels") return "标签维护";
  if (dialog.value === "outlook" || (dialog.value === "edit" && isOutlookMailbox(mailbox.value))) return "Outlook 邮箱";
  return dialog.value === "new" ? "手动录入邮箱" : "编辑邮箱";
});
const outlookBusy = computed(() => outlookLoading.value || outlookCompleting.value);

const filteredMailboxes = computed(() => {
  const text = query.value.trim().toLowerCase();
  const mailboxes = detail.value.mailboxes || [];
  if (!text) return mailboxes;
  return mailboxes.filter((item) => {
    const email = String(item.email || "").toLowerCase();
    const service = [
      item.imap_server,
      item.service_address,
      item.pop_server,
    ].join(" ").toLowerCase();
    return email.includes(text) || service.includes(text);
  });
});

const pagedMailboxes = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return filteredMailboxes.value.slice(start, start + pageSize.value);
});

watch(
  () => props.search,
  () => {
    query.value = paramsFromSearch(props.search).get("mail_query") || "";
    currentPage.value = 1;
  },
  { immediate: true },
);

function applyFilter() {
  currentPage.value = 1;
  const next = new URLSearchParams();
  if (query.value.trim()) next.set("mail_query", query.value.trim());
  emit("navigate", { path: "/mail", search: next });
}

function resetFilter() {
  query.value = "";
  currentPage.value = 1;
  emit("navigate", { path: "/mail" });
}

function openDialog(nextDialog) {
  const next = new URLSearchParams({ dialog: nextDialog });
  emit("navigate", { path: "/mail", search: next });
}

function openMailbox(row, nextDialog) {
  const next = new URLSearchParams({
    dialog: nextDialog,
    email: row.email || "",
    service: row.service_address || row.imap_server || "",
  });
  emit("navigate", { path: "/mail", search: next });
}

function closeDialog() {
  emit("navigate", { path: "/mail" });
}

async function deleteMailbox(row) {
  try {
    await ElMessageBox.confirm("确定删除这个邮箱账号吗？", "删除邮箱", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    const result = await mailApi.deleteAccount({
      email: row.email || "",
      service_address: row.service_address || row.imap_server || "",
    });
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    if (error !== "cancel") ElMessage.error(error.message || "删除失败。");
  }
}

async function startOutlookLogin() {
  if (outlookBusy.value) return;
  outlookLoading.value = true;
  outlookResult.value = null;
  clearOutlookTimer();
  clearOutlookMessageHandler();
  try {
    const result = await mailApi.outlookStart({
      oauth_client_id: detail.value.outlook_defaults?.client_id || "",
    });
    const loginUrl = result.verification_uri_complete || result.verification_uri;
    if (!loginUrl) {
      throw new Error("Microsoft 未返回登录地址。");
    }
    outlookResult.value = result;
    const opened = await showOutlookDeviceMessage(result, loginUrl);
    if (!opened) {
      outlookResult.value = null;
      return;
    }
    outlookCompleting.value = true;
    scheduleOutlookCompletion(Number(result.interval || 5));
  } catch (error) {
    clearOutlookMessageHandler();
    closeOutlookPopup();
    ElMessage.error(error.message || "Outlook 登录启动失败。");
  } finally {
    outlookLoading.value = false;
  }
}

function openOutlookLoginPage(loginUrl) {
  outlookPopup = window.open(loginUrl, "mail_sync_outlook_login", "popup,width=560,height=720");
  if (!outlookPopup) {
    ElMessage.error("浏览器拦截了 Microsoft 登录窗口，请允许弹窗后重试。");
    return false;
  }
  outlookPopup.focus();
  return true;
}

async function showOutlookDeviceMessage(result, loginUrl) {
  const completeUrl = result.verification_uri_complete || "";
  const verificationUrl = result.verification_uri || "";
  const message = completeUrl && completeUrl !== verificationUrl
    ? h("div", { style: "line-height: 1.7;" }, [
        h("p", null, "点击“去登录”打开 Microsoft 登录页面。"),
        h("p", null, "登录完成后会自动保存邮箱账号。"),
      ])
    : h("div", { style: "line-height: 1.7;" }, [
        h("p", null, "点击“去登录”打开 Microsoft 登录页面，并输入下面的验证码。"),
        h(
          "div",
          {
            style: [
              "margin: 14px auto",
              "padding: 12px 18px",
              "width: fit-content",
              "min-width: 180px",
              "border: 1px solid #c3ddff",
              "border-radius: 4px",
              "background: #ecf5ff",
              "color: #172033",
              "font-family: ui-monospace, SFMono-Regular, Menlo, monospace",
              "font-size: 22px",
              "font-weight: 600",
              "text-align: center",
              "letter-spacing: 1px",
            ].join("; "),
          },
          result.user_code || "-",
        ),
        h("p", null, "登录完成后会自动保存邮箱账号。"),
      ]);
  const confirmed = await ElMessageBox.alert(message, "Outlook 登录", {
    type: "info",
    center: true,
    showClose: false,
    closeOnClickModal: false,
    closeOnPressEscape: false,
    confirmButtonText: "去登录",
  }).then(
    () => true,
    () => false,
  );
  if (!confirmed) return false;
  return openOutlookLoginPage(loginUrl);
}

function scheduleOutlookCompletion(delaySeconds) {
  clearOutlookTimer();
  outlookTimer = window.setTimeout(completeOutlookLogin, Math.max(0, delaySeconds) * 1000);
}

function clearOutlookTimer() {
  if (!outlookTimer) return;
  window.clearTimeout(outlookTimer);
  outlookTimer = null;
}

function bindOutlookMessageHandler() {
  outlookMessageHandler = (event) => {
    const payload = event.data || {};
    if (outlookPopup && event.source !== outlookPopup) return;
    if (payload.type !== "mail-sync-outlook-login") return;
    clearOutlookMessageHandler();
    outlookLoading.value = false;
    outlookCompleting.value = false;
    outlookResult.value = null;
    closeOutlookPopup();
    if (!payload.ok) {
      ElMessage.error(payload.message || "Outlook 登录失败。");
      return;
    }
    ElMessage.success(payload.message || "Outlook 登录完成。");
    if (payload.redirect_url) {
      window.MailSyncApplyRedirect?.(payload);
      return;
    }
    emit("reload");
  };
  window.addEventListener("message", outlookMessageHandler);
}

function clearOutlookMessageHandler() {
  if (!outlookMessageHandler) return;
  window.removeEventListener("message", outlookMessageHandler);
  outlookMessageHandler = null;
}

function closeOutlookPopup() {
  try {
    outlookPopup?.close();
  } catch (error) {
    void error;
  }
  outlookPopup = null;
}

async function completeOutlookLogin() {
  if (!outlookResult.value?.device_code) {
    outlookCompleting.value = false;
    return;
  }
  try {
    const result = await mailApi.outlookComplete({
      oauth_client_id: outlookResult.value?.client_id || detail.value.outlook_defaults?.client_id || "",
      device_code: outlookResult.value?.device_code || "",
    });
    if (result.pending) {
      const interval = result.slow_down
        ? Number(outlookResult.value?.interval || 5) + 5
        : Number(outlookResult.value?.interval || 5);
      scheduleOutlookCompletion(interval);
      return;
    }
    if (result.ok === false) {
      throw new Error(result.message || "Outlook 登录完成失败。");
    }
    ElMessage.success(result.message || "Outlook 登录完成。");
    outlookResult.value = null;
    outlookCompleting.value = false;
    closeOutlookPopup();
    if (result.redirect_url) {
      window.MailSyncApplyRedirect?.(result);
      return;
    }
    emit("reload");
  } catch (error) {
    outlookResult.value = null;
    outlookCompleting.value = false;
    closeOutlookPopup();
    ElMessage.error(error.message || "Outlook 登录完成失败。");
  }
}

onUnmounted(() => {
  clearOutlookTimer();
  clearOutlookMessageHandler();
});

function mailboxSubtitle(row) {
  const auth = isOutlookMailbox(row) ? "Outlook 邮箱" : "手动密码";
  return auth;
}

function mailboxServiceText(row) {
  return row.imap_server || row.service_address || row.pop_server || "-";
}

function isOutlookMailbox(row) {
  return Boolean(row && row.auth_provider === "outlook");
}
</script>

<style scoped>
.code-line {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 18px;
  color: var(--text-strong);
}
</style>
