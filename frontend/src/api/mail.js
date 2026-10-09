import { postJson, requestJson } from "./session";

export const mailApi = {
  detail(search = "") {
    return requestJson(`/api/mail${search}`);
  },
  saveAccount(fields) {
    return postJson("/api/mail/account/save", fields);
  },
  deleteAccount(fields) {
    return postJson("/api/mail/account/delete", fields);
  },
  testConnection(fields) {
    return postJson("/api/mail/test-connection", fields, { allowOkFalse: true });
  },
  outlookStart(fields) {
    return postJson("/api/mail/outlook/start", fields);
  },
  outlookAuthorize(fields) {
    return postJson("/api/mail/outlook/authorize", fields);
  },
  outlookComplete(fields) {
    return postJson("/api/mail/outlook/complete", fields, { allowOkFalse: true });
  },
  discoverLabels(fields) {
    return postJson("/api/mail/labels/discover", fields);
  },
  updateLabel(fields) {
    return postJson("/api/mail/labels/update", fields);
  },
  deleteLabel(fields) {
    return postJson("/api/mail/labels/delete", fields);
  },
};
