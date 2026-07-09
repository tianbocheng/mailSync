export async function requestJson(url, options = {}) {
  const { allow401, allowOkFalse, ...fetchOptions } = options;
  const response = await fetch(url, {
    ...fetchOptions,
    headers: {
      Accept: "application/json",
      ...(fetchOptions.headers || {}),
    },
  });

  if (response.status === 401 && allow401) {
    return { authenticated: false };
  }
  if (response.status === 401) {
    window.location.assign("/#/login");
    return null;
  }

  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok || (data.ok === false && !allowOkFalse)) {
    throw new Error(data.error || data.message || "请求失败。");
  }
  return data;
}

function jsonBody(fields) {
  if (fields instanceof URLSearchParams) {
    const body = {};
    for (const [key, value] of fields.entries()) {
      if (Object.prototype.hasOwnProperty.call(body, key)) {
        if (!Array.isArray(body[key])) body[key] = [body[key]];
        body[key].push(value);
      } else {
        body[key] = value;
      }
    }
    return body;
  }
  return fields || {};
}

export function postJson(path, fields, options = {}) {
  const { headers, ...rest } = options;
  return requestJson(path, {
    ...rest,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(headers || {}),
    },
    body: JSON.stringify(jsonBody(fields)),
  });
}

export const sessionApi = {
  current(options = {}) {
    return requestJson("/api/session", options);
  },
  submit(path, fields) {
    return postJson(path, fields);
  },
};
