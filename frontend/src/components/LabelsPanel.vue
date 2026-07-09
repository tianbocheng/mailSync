<template>
  <div>
    <el-form :model="createForm" inline @submit.prevent>
      <el-form-item label="内部 ID">
        <el-input v-model="createForm.label_id" placeholder="$label" />
      </el-form-item>
      <el-form-item label="显示名称">
        <el-input v-model="createForm.label_name" />
      </el-form-item>
      <el-button type="primary" :loading="creating" @click="createLabel">新增标签</el-button>
    </el-form>

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
import { computed, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { mailApi } from "@/api/mail";

const props = defineProps({
  detail: {
    type: Object,
    default: () => ({}),
  },
});

const emit = defineEmits(["reload"]);

const createForm = reactive({
  label_id: "",
  label_name: "",
});
const labels = ref([]);
const creating = ref(false);
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

async function createLabel() {
  creating.value = true;
  try {
    const result = await mailApi.createLabel({
      ...mailboxFields(),
      label_id: createForm.label_id,
      label_name: createForm.label_name,
    });
    createForm.label_id = "";
    createForm.label_name = "";
    window.MailSyncApplyRedirect?.(result);
  } catch (error) {
    ElMessage.error(error.message || "新增失败。");
  } finally {
    creating.value = false;
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
