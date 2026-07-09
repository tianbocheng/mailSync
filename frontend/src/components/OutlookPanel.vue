<template>
  <div>
    <el-descriptions :column="1" border>
      <el-descriptions-item label="邮箱">{{ account.email || "-" }}</el-descriptions-item>
      <el-descriptions-item label="认证方式">Microsoft OAuth</el-descriptions-item>
      <el-descriptions-item label="协议">{{ account.protocol_summary || "IMAP" }}</el-descriptions-item>
      <el-descriptions-item label="IMAP 服务">{{ account.imap_server || "-" }}</el-descriptions-item>
      <el-descriptions-item label="IMAP 端口">{{ account.imap_port || "-" }}</el-descriptions-item>
      <el-descriptions-item label="使用 SSL">{{ account.use_ssl === false ? "否" : "是" }}</el-descriptions-item>
      <el-descriptions-item label="OAuth Client ID">{{ account.oauth_client_id || "-" }}</el-descriptions-item>
      <el-descriptions-item label="OAuth 令牌">{{ account.oauth_token_saved ? "已保存到数据库" : "未保存" }}</el-descriptions-item>
    </el-descriptions>

    <el-alert
      v-if="testMessage"
      :title="testMessage"
      type="success"
      show-icon
      :closable="false"
      class="result-box"
    />

    <div class="form-actions result-box">
      <el-button @click="close">关闭</el-button>
      <el-button :loading="testing" @click="testConnection">测试验证</el-button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { mailApi } from "@/api/mail";
import { submitFields } from "@/utils/forms";

const props = defineProps({
  mailbox: {
    type: Object,
    default: null,
  },
});

const account = computed(() => props.mailbox || {});
const testing = ref(false);
const testMessage = ref("");

watch(
  () => props.mailbox,
  () => {
    testMessage.value = "";
  },
);

function buildFields() {
  return submitFields({
    original_email: account.value.email || "",
    original_service_address:
      account.value.service_address || account.value.imap_server || "",
    email: account.value.email || "",
    service_address:
      account.value.service_address || account.value.imap_server || "",
    auth_provider: "outlook",
    password: "",
    oauth_client_id: account.value.oauth_client_id || "",
    imap_timeout: account.value.imap_timeout || "",
    proxy_profile_id: account.value.proxy_profile_id || "",
    target_folder: account.value.target_folder || "INBOX",
    mailbox_protocol_tab: "imap",
    imap_enabled: true,
    imap_server:
      account.value.imap_server || account.value.service_address || "",
    imap_port: account.value.imap_port || "993",
    use_ssl: account.value.use_ssl !== false,
  });
}

async function testConnection() {
  testing.value = true;
  testMessage.value = "";
  try {
    const result = await mailApi.testConnection(buildFields());
    testMessage.value = result.message || "连接测试成功。";
  } catch (error) {
    ElMessage.error(error.message || "测试失败。");
  } finally {
    testing.value = false;
  }
}

function close() {
  window.MailSyncNavigate?.({ path: "/mail" });
}
</script>
