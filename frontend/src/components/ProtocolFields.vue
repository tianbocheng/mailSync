<template>
  <el-row :gutter="12">
    <el-col :span="24">
      <el-form-item :label="`${protocolName} 服务地址`" :required="required">
        <el-input
          :model-value="modelValue.server"
          :placeholder="protocol === 'imap' ? 'imap.example.com' : 'pop.example.com'"
          @update:model-value="patch('server', $event)"
        />
      </el-form-item>
    </el-col>
  </el-row>
  <el-row :gutter="12">
    <el-col :span="8">
      <el-form-item label="端口" :required="required">
        <el-input
          :model-value="modelValue.port"
          inputmode="numeric"
          @update:model-value="patch('port', $event)"
        />
      </el-form-item>
    </el-col>
    <el-col :span="8">
      <el-form-item label="使用 SSL">
        <el-select
          :model-value="modelValue.use_ssl"
          style="width: 100%"
          @update:model-value="patch('use_ssl', $event)"
        >
          <el-option label="是" value="1" />
          <el-option label="否" value="0" />
        </el-select>
      </el-form-item>
    </el-col>
    <el-col :span="8">
      <el-form-item label="代理">
        <el-select
          :model-value="modelValue.proxy"
          :empty-values="emptyProxyValues"
          style="width: 100%"
          @update:model-value="patch('proxy', $event)"
        >
          <el-option label="Direct" value="" />
          <el-option
            v-for="proxy in proxies"
            :key="proxy.id"
            :label="proxy.name || `Proxy ${proxy.id}`"
            :value="String(proxy.id || '')"
          />
        </el-select>
      </el-form-item>
    </el-col>
  </el-row>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  modelValue: {
    type: Object,
    required: true,
  },
  protocol: {
    type: String,
    required: true,
  },
  proxies: {
    type: Array,
    default: () => [],
  },
  required: {
    type: Boolean,
    default: true,
  },
});

const emit = defineEmits(["update:modelValue"]);

const protocolName = computed(() => props.protocol.toUpperCase());
const emptyProxyValues = [null, undefined];

function patch(key, value) {
  emit("update:modelValue", { ...props.modelValue, [key]: value });
}
</script>
