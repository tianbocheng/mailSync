<template>
  <main class="login-shell">
    <el-card class="login-panel page-card" shadow="never">
      <h1 class="login-title">邮件同步后台</h1>
      <p class="login-copy">登录后管理邮箱同步规则、邮箱账号和运行配置。</p>
      <el-alert
        v-if="displayError"
        :title="displayError"
        type="error"
        show-icon
        :closable="false"
        class="result-box"
      />
      <el-form label-position="top" @submit.prevent="submitLogin">
        <el-form-item label="用户名">
          <el-input
            v-model="username"
            autocomplete="username"
            required
            autofocus
            @keyup.enter="submitLogin"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
            show-password
            @keyup.enter="submitLogin"
          />
        </el-form-item>
        <el-button
          native-type="button"
          type="primary"
          :loading="submitting"
          style="width: 100%"
          @click="submitLogin"
        >
          登录
        </el-button>
      </el-form>
    </el-card>
  </main>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { ElMessage } from "element-plus";

const props = defineProps({
  search: {
    type: String,
    default: "",
  },
});

const emit = defineEmits(["login"]);

const username = ref("");
const password = ref("");
const submitting = ref(false);
const serverError = ref("");

const errorMessage = computed(() => {
  const params = new URLSearchParams(props.search || "");
  if (params.get("error") === "invalid_credentials") {
    return "用户名或密码不正确。";
  }
  return "";
});

const displayError = computed(() => serverError.value || errorMessage.value);

watch([username, password], () => {
  serverError.value = "";
});

onMounted(() => {
  if (errorMessage.value) {
    ElMessage.error(errorMessage.value);
  }
});

async function submitLogin() {
  const nextUsername = username.value.trim();
  if (!nextUsername) {
    ElMessage.warning("请输入用户名。");
    return;
  }
  if (!password.value) {
    ElMessage.warning("请输入密码。");
    return;
  }

  submitting.value = true;
  serverError.value = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({
        username: nextUsername,
        password: password.value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || "用户名或密码不正确。");
    }
    ElMessage.success("登录成功。");
    emit("login", {
      username: data.username || nextUsername,
      redirect_url: data.redirect_url || "/#/rules",
    });
  } catch (error) {
    const message = error.message || "登录失败，请稍后重试。";
    serverError.value = message;
    ElMessage.error(message);
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.login-shell {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background:
    linear-gradient(135deg, rgba(64, 158, 255, 0.12), transparent 36%),
    #f5f7fb;
}

.login-panel {
  width: min(420px, 100%);
}

.login-panel.page-card {
  min-height: 0;
}

.login-title {
  margin: 0 0 8px;
  font-size: 28px;
  color: #111827;
}

.login-copy {
  margin: 0 0 20px;
  color: #6b7280;
}
</style>
