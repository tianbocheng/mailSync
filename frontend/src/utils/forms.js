export function appendField(fields, name, value) {
  const nextValue = value == null ? "" : String(value);
  if (Object.prototype.hasOwnProperty.call(fields, name)) {
    if (!Array.isArray(fields[name])) fields[name] = [fields[name]];
    fields[name].push(nextValue);
    return;
  }
  fields[name] = nextValue;
}

export function appendChecked(fields, name, checked) {
  if (checked) appendField(fields, name, "1");
}

export function submitFields(fields) {
  const params = {};
  for (const [name, value] of Object.entries(fields)) {
    if (Array.isArray(value)) {
      if (!value.length) {
        appendField(params, name, "");
      } else {
        value.forEach((item) => appendField(params, name, item));
      }
    } else if (typeof value === "boolean") {
      appendChecked(params, name, value);
    } else {
      appendField(params, name, value);
    }
  }
  return params;
}

export function selectedMailbox(detail, params) {
  const email = params.get("email") || params.get("target") || "";
  const service = params.get("service") || "";
  const selected = detail.mailbox || detail.target || null;
  if (
    selected
    && (!email || selected.email === email)
    && (!service || selected.service_address === service)
  ) {
    return selected;
  }
  const mailboxes = detail.mailboxes || [];
  return mailboxes.find((mailbox) => {
    return (!email || mailbox.email === email)
      && (!service || mailbox.service_address === service);
  }) || selected;
}

export function truthyValue(value) {
  return value ? "1" : "0";
}
