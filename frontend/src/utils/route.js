export function parseRoute() {
  const raw = window.location.hash.startsWith("#/")
    ? window.location.hash.slice(1)
    : normalizePath(window.location.pathname || "/rules") + window.location.search;
  const [pathPart, queryPart = ""] = raw.split("?", 2);
  return {
    path: normalizePath(pathPart || "/rules"),
    search: queryPart ? `?${queryPart}` : "",
  };
}

export function normalizePath(path) {
  if (path === "/" || path === "") return "/rules";
  if (path === "/accounts" || path === "/labels" || path === "/target") return "/mail";
  if (path === "/proxies") return "/config";
  return path;
}

export function routeHref(path, params = "") {
  const normalized = normalizePath(path || "/rules");
  const text = params instanceof URLSearchParams ? params.toString() : String(params || "");
  const search = text ? (text.startsWith("?") ? text : `?${text}`) : "";
  return `/#${normalized}${search}`;
}

export function paramsFromSearch(search) {
  return new URLSearchParams(search || "");
}

export function routeFromRedirect(url) {
  const next = new URL(url, window.location.href);
  if (next.hash.startsWith("#/")) {
    const [pathPart, queryPart = ""] = next.hash.slice(1).split("?", 2);
    return {
      path: normalizePath(pathPart || "/rules"),
      search: queryPart ? `?${queryPart}` : "",
    };
  }
  return {
    path: normalizePath(next.pathname || "/rules"),
    search: next.search,
  };
}
