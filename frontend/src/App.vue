<template>
  <LoginView
    v-if="ready && !authenticated"
    :search="route.search || windowSearch"
    @login="handleLogin"
  />
  <el-container v-else-if="ready" class="app-layout">
    <el-aside width="224px" class="app-aside">
      <div class="brand">
        <div class="brand-mark">M</div>
        <div class="brand-text">
          <strong>邮件同步</strong>
          <span>后台</span>
        </div>
      </div>
      <el-menu :default-active="activePath">
        <el-menu-item index="/rules" @click="navigate('/rules')">
          <el-icon><Switch /></el-icon>
          <span>规则</span>
        </el-menu-item>
        <el-menu-item index="/mail" @click="navigate('/mail')">
          <el-icon><Message /></el-icon>
          <span>邮箱</span>
        </el-menu-item>
        <el-menu-item index="/config" @click="navigate('/config')">
          <el-icon><Setting /></el-icon>
          <span>配置</span>
        </el-menu-item>
      </el-menu>
      <div class="aside-user">
        <span class="aside-username" :title="username">{{ username }}</span>
        <el-button class="aside-logout" link type="primary" tag="a" href="/logout">
          退出登录
        </el-button>
      </div>
    </el-aside>
    <el-container>
      <el-main class="app-main" v-loading="loading">
        <el-alert
          v-if="loadError"
          :title="loadError"
          type="error"
          show-icon
          :closable="false"
          class="result-box"
        />
        <RulesView
          v-else-if="route.path === '/rules'"
          :data="routeData"
          :search="route.search"
          @navigate="navigateToRoute"
          @reload="loadRoute"
        />
        <MailView
          v-else-if="route.path === '/mail'"
          :data="routeData"
          :search="route.search"
          @navigate="navigateToRoute"
          @reload="loadRoute"
        />
        <ConfigView
          v-else-if="route.path === '/config'"
          :data="routeData"
          :search="route.search"
          @navigate="navigateToRoute"
          @reload="loadRoute"
        />
        <el-empty v-else description="页面不存在" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";

import LoginView from "@/views/LoginView.vue";
import RulesView from "@/views/RulesView.vue";
import MailView from "@/views/MailView.vue";
import ConfigView from "@/views/ConfigView.vue";
import { configApi } from "@/api/config";
import { mailApi } from "@/api/mail";
import { ruleApi } from "@/api/rule";
import { sessionApi } from "@/api/session";
import { parseRoute, routeFromRedirect, routeHref } from "@/utils/route";

const ready = ref(false);
const authenticated = ref(false);
const username = ref("");
const route = ref(parseRoute());
const routeData = ref(null);
const loading = ref(false);
const loadError = ref("");
const windowSearch = window.location.search;

const activePath = computed(() => route.value.path);

async function init() {
  const session = await sessionApi.current({ allow401: true });
  authenticated.value = Boolean(session?.authenticated);
  username.value = session?.username || "";
  ready.value = true;

  if (!authenticated.value) {
    return;
  }
  if (route.value.path === "/login") {
    navigate("/rules");
    return;
  }
  await loadRoute();
}

async function loadRoute() {
  route.value = parseRoute();
  if (!authenticated.value || route.value.path === "/login") {
    return;
  }
  loading.value = true;
  loadError.value = "";
  try {
    if (route.value.path === "/mail") {
      routeData.value = await mailApi.detail(route.value.search);
    } else if (route.value.path === "/config") {
      routeData.value = await configApi.detail(route.value.search);
    } else {
      routeData.value = await ruleApi.detail(route.value.search);
    }
    if (routeData.value?.notice) {
      ElMessage.success(routeData.value.notice);
    }
  } catch (error) {
    loadError.value = error.message || "页面加载失败。";
  } finally {
    loading.value = false;
  }
}

function navigate(path, params = "") {
  const href = routeHref(path, params);
  const current = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (href === current || href === `${window.location.pathname}${window.location.hash}`) {
    loadRoute();
    return;
  }
  window.history.pushState({}, "", href);
  loadRoute();
}

function navigateToRoute(payload) {
  if (typeof payload === "string") {
    navigate(payload);
    return;
  }
  navigate(payload.path, payload.search || payload.params || "");
}

function applyRedirect(result) {
  if (!result?.redirect_url) {
    loadRoute();
    return;
  }
  const next = routeFromRedirect(result.redirect_url);
  navigate(next.path, next.search);
}

function handleLogin(result) {
  authenticated.value = true;
  username.value = result?.username || "";
  const next = routeFromRedirect(result?.redirect_url || "/#/rules");
  navigate(next.path, next.search);
}

function onPopState() {
  loadRoute();
}

onMounted(() => {
  window.MailSyncNavigate = navigateToRoute;
  window.MailSyncApplyRedirect = applyRedirect;
  window.addEventListener("hashchange", onPopState);
  window.addEventListener("popstate", onPopState);
  init();
});

onBeforeUnmount(() => {
  window.removeEventListener("hashchange", onPopState);
  window.removeEventListener("popstate", onPopState);
});
</script>

<style scoped>
.app-layout {
  min-height: 100vh;
  background: var(--app-bg);
}

.app-aside {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--line-strong);
  background: var(--surface);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 62px;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 6px;
  color: #fff;
  font-weight: 700;
  background: #1f6feb;
}

.brand-text {
  display: grid;
  gap: 2px;
}

.brand-text strong {
  color: var(--text-strong);
  font-size: 15px;
}

.brand-text span {
  color: var(--muted);
  font-size: 12px;
}

.app-main {
  padding: 0;
}

.app-aside :deep(.el-menu) {
  flex: 1;
  min-height: 0;
  border-right: 0;
  overflow-y: auto;
}

.app-aside :deep(.el-menu-item) {
  height: 48px;
  color: #4b5565;
  font-size: 13px;
}

.app-aside :deep(.el-menu-item.is-active) {
  color: var(--primary);
  background: #f0f7ff;
}

.aside-user {
  display: grid;
  gap: 8px;
  padding: 14px 18px 16px;
  border-top: 1px solid var(--line);
}

.aside-username {
  min-width: 0;
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.aside-logout {
  justify-self: start;
}

@media (max-width: 900px) {
  .app-aside {
    width: 72px !important;
  }

  .brand-text {
    display: none;
  }

  .aside-user {
    padding: 12px 8px;
    justify-items: center;
  }

  .aside-username {
    display: none;
  }
}
</style>
