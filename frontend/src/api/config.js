import { postJson, requestJson } from "./session";

export const configApi = {
  detail(search = "") {
    return requestJson(`/api/config${search}`);
  },
  save(fields) {
    return postJson("/api/config/save", fields);
  },
};
