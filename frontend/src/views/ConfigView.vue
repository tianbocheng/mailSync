<template>
  <section class="page">
    <el-card class="page-card" shadow="never">
      <div class="page-heading">
        <h2>系统配置管理</h2>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </div>
      <el-alert
        v-if="data?.error"
        :title="data.error"
        type="error"
        show-icon
        :closable="false"
        class="result-box"
      />

      <el-form label-position="top">
        <el-card shadow="never" class="settings-card drawer-section">
          <template #header>
            <strong>Outlook 登录</strong>
          </template>
          <el-form-item label="Outlook 客户端 ID">
            <el-input v-model="form.client_id" placeholder="Microsoft Entra 应用 Client ID" />
          </el-form-item>
        </el-card>

        <el-card shadow="never" class="settings-card">
          <template #header>
            <div class="page-heading" style="margin-bottom: 0">
              <strong>代理配置</strong>
              <el-button :icon="Plus" @click="addProxy">新增代理</el-button>
            </div>
          </template>
          <el-tabs v-model="activeProxy" type="border-card" @tab-remove="handleProxyTabRemove">
            <el-tab-pane
              v-for="(proxy, index) in form.proxies"
              :key="proxy.key"
              :label="proxyTabLabel(proxy, index)"
              :name="proxy.key"
              :closable="canCloseProxy(proxy)"
            >
              <el-alert
                v-if="proxy.delete"
                title="保存后将删除此代理配置。"
                type="warning"
                show-icon
                :closable="false"
                class="drawer-section"
              />
              <el-row :gutter="12">
                <el-col :span="12">
                  <el-form-item label="代理名称">
                    <el-input v-model="proxy.name" placeholder="例如：本地代理" :disabled="proxy.delete" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="代理类型">
                    <el-select v-model="proxy.type" style="width: 100%" :disabled="proxy.delete">
                      <el-option label="HTTP 连接" value="http_connect" />
                      <el-option label="SOCKS5" value="socks5" />
                    </el-select>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="12">
                <el-col :span="12">
                  <el-form-item label="代理主机">
                    <el-input v-model="proxy.host" placeholder="127.0.0.1" :disabled="proxy.delete" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="代理端口">
                    <el-input v-model="proxy.port" inputmode="numeric" placeholder="7890" :disabled="proxy.delete" />
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="12">
                <el-col :span="12">
                  <el-form-item label="用户名">
                    <el-input v-model="proxy.username" placeholder="可选" :disabled="proxy.delete" />
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="密码">
                    <el-input
                      v-model="proxy.password"
                      type="password"
                      show-password
                      :placeholder="proxy.id && proxy.has_password ? '留空保留' : '可选'"
                      :disabled="proxy.delete"
                    />
                  </el-form-item>
                </el-col>
              </el-row>
              <div v-if="proxy.id" class="inline-actions">
                <el-tag effect="plain">关联邮箱 {{ proxy.account_count || 0 }}</el-tag>
              </div>
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </el-form>
    </el-card>
  </section>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { Plus } from "@element-plus/icons-vue";

import { configApi } from "@/api/config";
import { appendField } from "@/utils/forms";

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

const saving = ref(false);
const activeProxy = ref("");
const form = reactive({
  client_id: "",
  proxies: [],
});

const config = computed(() => props.data || {});

watch(
  () => props.data,
  () => reset(),
  { immediate: true },
);

function reset() {
  form.client_id = config.value.outlook_login?.client_id || "";
  form.proxies = (config.value.proxies || []).map((proxy, index) => proxyModel(proxy, index));
  addProxy();
  activeProxy.value = form.proxies[0]?.key || "";
}

function proxyModel(proxy, index) {
  return {
    key: `${proxy?.id || "new"}-${index}-${Date.now()}`,
    id: proxy?.id || "",
    name: proxy?.name || "",
    type: proxy?.type || "http_connect",
    host: proxy?.host || "",
    port: proxy?.port || "",
    username: proxy?.username || "",
    password: "",
    has_password: Boolean(proxy?.has_password),
    account_count: Number(proxy?.account_count || 0),
    delete: false,
  };
}

function addProxy() {
  form.proxies.push(proxyModel(null, form.proxies.length));
  activeProxy.value = form.proxies[form.proxies.length - 1]?.key || "";
}

function removeProxy(index) {
  const removedKey = form.proxies[index]?.key || "";
  form.proxies.splice(index, 1);
  if (activeProxy.value === removedKey || !form.proxies.some((proxy) => proxy.key === activeProxy.value)) {
    activeProxy.value = form.proxies[Math.max(0, index - 1)]?.key || form.proxies[0]?.key || "";
  }
}

function proxyTabLabel(proxy, index) {
  const label = proxy.name || (proxy.id ? `代理 ${index + 1}` : "新增代理");
  return proxy.delete ? `${label}（待删除）` : label;
}

function canCloseProxy(proxy) {
  return !proxy.id || Number(proxy.account_count || 0) <= 0;
}

function handleProxyTabRemove(proxyKey) {
  const index = form.proxies.findIndex((proxy) => proxy.key === proxyKey);
  if (index < 0) return;

  const proxy = form.proxies[index];
  if (proxy.id) {
    if (!canCloseProxy(proxy)) return;
    proxy.delete = !proxy.delete;
    activeProxy.value = proxy.key;
    return;
  }

  removeProxy(index);
}

function buildFields() {
  const fields = {};
  appendField(fields, "client_id", form.client_id);
  appendField(fields, "proxy_count", form.proxies.length);
  form.proxies.forEach((proxy, index) => {
    appendField(fields, `proxy_id_${index}`, proxy.id);
    appendField(fields, `proxy_name_${index}`, proxy.name);
    appendField(fields, `proxy_type_${index}`, proxy.type);
    appendField(fields, `proxy_host_${index}`, proxy.host);
    appendField(fields, `proxy_port_${index}`, proxy.port);
    appendField(fields, `proxy_username_${index}`, proxy.username);
    appendField(fields, `proxy_password_${index}`, proxy.password);
    if (proxy.delete) appendField(fields, `proxy_delete_${index}`, "1");
  });
  return fields;
}

async function save() {
  saving.value = true;
  try {
    const result = await configApi.save(buildFields());
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "保存失败。");
  } finally {
    saving.value = false;
  }
}
</script>

<style scoped>
.drawer-section {
  margin-bottom: 20px;
}

.drawer-section h3 {
  margin: 0 0 12px;
  font-size: 16px;
}

.settings-card {
  border: 1px solid var(--line);
  border-radius: 4px;
  box-shadow: none;
}

.settings-card :deep(.el-card__header) {
  min-height: 40px;
  padding: 10px 16px;
  color: var(--text-strong);
  background: #fbfcff;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}

.settings-card :deep(.el-card__body) {
  padding: 14px 16px 4px;
}
</style>
