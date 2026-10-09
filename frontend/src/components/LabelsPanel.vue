<template>
  <div>
    <el-form inline @submit.prevent>
      <el-form-item label="邮件 Message-ID（推荐）">
        <el-input
          v-model="discoverMessageId"
          placeholder="例如 <abc@example.com>"
        />
      </el-form-item>
      <el-form-item label="测试邮件主题（备用）">
        <el-input
          v-model="discoverSubject"
          placeholder="Message-ID 不可用时输入主题"
        />
      </el-form-item>
      <el-button :loading="discovering" @click="discoverLabels">读取邮件标签</el-button>
    </el-form>

    <el-alert
      v-if="discoveredLabels.length"
      type="info"
      :closable="false"
      class="label-discovery"
      title="已读取测试邮件标签。IMAP 只能提供内部 ID；本地已有映射会自动带出名称，未知标签的名称请在下方映射表中维护。"
    />
    <el-table v-if="discoveredLabels.length" :data="discoveredLabels" class="result-box discovery-table">
      <el-table-column prop="label_id" label="发现的内部 ID" min-width="220" />
      <el-table-column prop="label_name" label="显示名称" min-width="220" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          {{ row.known ? "已存在" : "未映射" }}
        </template>
      </el-table-column>
    </el-table>

    <el-table :data="pagedLabels" empty-text="暂无标签" class="result-box">
      <el-table-column label="内部 ID" min-width="180">
        <template #default="{ row }">
          <el-input v-model="row.edit_label_id" />
        </template>
      </el-table-column>
      <el-table-column label="显示名称" min-width="220">
        <template #default="{ row }">
          <el-input v-model="row.edit_label_name" />
        </template>
      </el-table-column>
      <el-table-column label="使用数" width="90">
        <template #default="{ row }">{{ row.usage_count || 0 }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <div class="table-actions">
            <el-button link type="primary" @click="updateLabel(row)">保存</el-button>
            <el-button
              link
              type="danger"
              :disabled="Number(row.usage_count || 0) > 0"
              @click="deleteLabel(row)"
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
        :total="labels.length"
        layout="total, prev, pager, next, sizes, jumper"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { mailApi } from "@/api/mail";

const props = defineProps({
  detail: {
    type: Object,
    default: () => ({}),
  },
});

const emit = defineEmits(["reload"]);

const discoverSubject = ref("");
const discoverMessageId = ref("");
const discoveredLabels = ref([]);
const labels = ref([]);
const discovering = ref(false);
const currentPage = ref(1);
const pageSize = ref(10);

const pagedLabels = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return labels.value.slice(start, start + pageSize.value);
});

watch(
  () => props.detail.labels,
  () => {
    labels.value = (props.detail.labels || []).map((label) => ({
      ...label,
      edit_label_id: label.label_id || "",
      edit_label_name: label.label_name || "",
    }));
    currentPage.value = 1;
  },
  { immediate: true },
);

watch([() => labels.value.length, pageSize], () => {
  const maxPage = Math.max(1, Math.ceil(labels.value.length / pageSize.value));
  if (currentPage.value > maxPage) currentPage.value = maxPage;
});

function mailboxFields() {
  const mailbox = props.detail.mailbox || props.detail.target || {};
  return {
    target_email: mailbox.email || "",
    target_service_address: mailbox.service_address || mailbox.imap_server || "",
    dialog: "labels",
  };
}

async function discoverLabels() {
  discovering.value = true;
  try {
    const result = await mailApi.discoverLabels({
      ...mailboxFields(),
      message_id: discoverMessageId.value,
      subject: discoverSubject.value,
    });
    discoveredLabels.value = result.labels || [];
    if (result.unknown_ids?.length) {
      ElMessage.info("发现了新标签 ID；IMAP 不会返回标签名称，请在下方映射表中维护名称。");
    } else {
      ElMessage.success(`已读取 ${discoveredLabels.value.length} 个标签。`);
    }
  } catch (error) {
    ElMessage.error(error.message || "读取标签失败。");
  } finally {
    discovering.value = false;
  }
}

async function updateLabel(row) {
  try {
    const result = await mailApi.updateLabel({
      ...mailboxFields(),
      original_label_id: row.label_id,
      label_id: row.edit_label_id,
      label_name: row.edit_label_name,
    });
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "保存失败。");
  }
}

async function deleteLabel(row) {
  try {
    await ElMessageBox.confirm("确定删除这个标签吗？", "删除标签", {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    });
    const result = await mailApi.deleteLabel({
      ...mailboxFields(),
      label_id: row.label_id,
    });
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    if (error !== "cancel") ElMessage.error(error.message || "删除失败。");
  }
}
</script>
