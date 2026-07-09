import { postJson, requestJson } from "./session";

export const ruleApi = {
  detail(search = "") {
    return requestJson(`/api/rules${search}`);
  },
  folders(fields) {
    return postJson("/api/rules/folders", fields);
  },
  testSource(fields) {
    return postJson("/api/rules/test-source", fields);
  },
  save(fields) {
    return postJson("/api/rules/save", fields);
  },
  toggle(fields) {
    return postJson("/api/rules/toggle", fields);
  },
  logs(sourceEmail, options = {}) {
    const params = new URLSearchParams({ source: sourceEmail || "" });
    for (const [key, value] of Object.entries(options)) {
      if (value !== undefined && value !== null && value !== "") {
        params.set(key, String(value));
      }
    }
    return requestJson(`/api/rules/logs?${params}`);
  },
  delete(fields) {
    return postJson("/api/rules/delete", fields);
  },
  deleteLogs(fields) {
    return postJson("/api/rules/logs/delete", fields);
  },
};
