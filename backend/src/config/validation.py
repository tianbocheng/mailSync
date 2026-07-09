from __future__ import annotations

from typing import Dict, Optional, Set

from common.errors import SyncError
from config.proxy import parse_imap_proxy


MICROSOFT_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"


def oauth_settings(profile: dict) -> Optional[dict]:
    auth = profile.get("auth")
    if auth is None:
        return None
    if not isinstance(auth, dict):
        raise SyncError(f"auth must be an object for {profile.get('email', '<unknown>')}")
    auth_type = auth.get("type", "password")
    if auth_type == "password":
        return None
    if auth_type != "oauth2" or auth.get("provider", "microsoft") != "microsoft":
        raise SyncError(
            f"Unsupported authentication configuration for "
            f"{profile.get('email', '<unknown>')}"
        )
    if not auth.get("client_id"):
        raise SyncError(
            f"Missing auth.client_id for {profile.get('email', '<unknown>')}"
        )
    if not isinstance(auth.get("token"), dict) and not auth.get("token_file"):
        raise SyncError(
            f"Missing OAuth token for {profile.get('email', '<unknown>')}"
        )
    return auth


def label_id_map(raw: dict) -> Dict[str, str]:
    if not isinstance(raw, dict):
        raise SyncError("synology_labels must be an object")
    inverse: Dict[str, str] = {}
    for internal_id, display_name in raw.items():
        if not isinstance(internal_id, str) or not isinstance(display_name, str):
            raise SyncError("synology_labels must map string IDs to string names")
        if display_name in inverse:
            raise SyncError(f"Duplicate label name in mapping: {display_name}")
        inverse[display_name] = internal_id
    return inverse


def validate_optional_label_name(label_name: object, context: str) -> None:
    if label_name is None or label_name == "":
        return
    if not isinstance(label_name, str):
        raise SyncError(f"{context} must be a label name")


def validate_optional_string_list(value: object, context: str) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise SyncError(f"{context} must be an array of strings")


def validate_target_profile(target: object, context: str) -> None:
    if not isinstance(target, dict):
        raise SyncError(f"Missing {context} mailbox configuration")
    for field in ("email", "imap_server", "folder"):
        if not isinstance(target.get(field), str) or not target.get(field):
            raise SyncError(f"Missing {context}.{field} in configuration")
    if oauth_settings(target) is None and not target.get("password"):
        raise SyncError(f"Missing password for {context} mailbox {target['email']}")
    parse_imap_proxy(target.get("imap_proxy"), f"{context} {target['email']}")


def validate_config(
    config: dict,
    labels_by_name: Dict[str, str],
    *,
    allow_empty_sources: bool = False,
) -> None:
    """Fail fast on configuration errors that would otherwise surface mid-sync."""
    if not isinstance(config, dict):
        raise SyncError("Configuration file must contain an object")
    if not config.get("state_db"):
        raise SyncError("Missing state_db in configuration")

    target = config.get("target")
    if target is not None:
        validate_target_profile(target, "target")

    sources = config.get("sources")
    if not isinstance(sources, list):
        raise SyncError("sources must be a non-empty array")
    if not sources:
        if allow_empty_sources:
            return
        raise SyncError("sources must be a non-empty array")
    seen_sources: Set[str] = set()
    for index, rule in enumerate(sources, start=1):
        if not isinstance(rule, dict):
            raise SyncError(f"sources[{index}] must be an object")
        email = rule.get("email")
        if not isinstance(email, str) or not email:
            raise SyncError(f"sources[{index}].email is required")
        if email in seen_sources:
            raise SyncError(f"Duplicate source mailbox configuration for {email}")
        seen_sources.add(email)
        if not isinstance(rule.get("imap_server"), str) or not rule.get(
            "imap_server"
        ):
            raise SyncError(f"Missing imap_server for source {email}")
        if oauth_settings(rule) is None and not rule.get("password"):
            raise SyncError(f"Missing password for source mailbox {email}")
        parse_imap_proxy(rule.get("imap_proxy"), email)

        rule_target = rule.get("target") or target
        validate_target_profile(rule_target, f"{email}.target")
        if "synology_labels" in rule:
            label_id_map(rule.get("synology_labels"))

        validate_optional_label_name(
            rule.get("account_label"),
            f"{email}.account_label",
        )
        default_folder_label = rule.get("default_folder_label")
        if default_folder_label:
            validate_optional_label_name(
                default_folder_label,
                f"{email}.default_folder_label",
            )
        validate_optional_label_name(
            rule.get("deleted_label"),
            f"{email}.deleted_label",
        )
        out_of_scope_policy = str(
            rule.get("out_of_scope_policy") or "keep"
        ).strip()
        if out_of_scope_policy not in {"keep", "label", "delete"}:
            raise SyncError(f"{email}.out_of_scope_policy is invalid")
        if out_of_scope_policy == "label":
            out_of_scope_labels = rule.get(
                "out_of_scope_labels",
                rule.get("out_of_scope_label"),
            )
            if isinstance(out_of_scope_labels, list):
                if not [label for label in out_of_scope_labels if label]:
                    raise SyncError(f"{email}.out_of_scope_label is required")
                for label_index, label_name in enumerate(out_of_scope_labels):
                    if not label_name:
                        continue
                    validate_optional_label_name(
                        label_name,
                        f"{email}.out_of_scope_labels[{label_index}]",
                    )
            else:
                if not out_of_scope_labels:
                    raise SyncError(f"{email}.out_of_scope_label is required")
                validate_optional_label_name(
                    out_of_scope_labels,
                    f"{email}.out_of_scope_label",
                )

        folder_labels = rule.get("folder_labels", {})
        if not isinstance(folder_labels, dict):
            raise SyncError(f"folder_labels for {email} must be an object")
        for folder_name, label_name in folder_labels.items():
            if not isinstance(folder_name, str) or not folder_name:
                raise SyncError(
                    f"folder_labels for {email} must use string folder names"
                )
            if isinstance(label_name, list):
                for index, item in enumerate(label_name):
                    if not item:
                        continue
                    validate_optional_label_name(
                        item,
                        f"{email}.folder_labels[{folder_name}][{index}]",
                    )
            else:
                if not label_name:
                    continue
                validate_optional_label_name(
                    label_name,
                    f"{email}.folder_labels[{folder_name}]",
                )

        target_folders = rule.get("target_folders", {})
        if not isinstance(target_folders, dict):
            raise SyncError(f"target_folders for {email} must be an object")
        for folder_name, target_folder in target_folders.items():
            if not isinstance(folder_name, str) or not folder_name:
                raise SyncError(
                    f"target_folders for {email} must use string folder names"
                )
            if not isinstance(target_folder, str) or not target_folder.strip():
                raise SyncError(
                    f"target_folders for {email} folder {folder_name} "
                    "must be a non-empty string"
                )

        validate_optional_string_list(
            rule.get("exclude_folders"),
            f"{email}.exclude_folders",
        )
        skipped = rule.get("skip_uids", {})
        if not isinstance(skipped, dict):
            raise SyncError(f"skip_uids for {email} must be an object")
        for folder_name, values in skipped.items():
            if not isinstance(folder_name, str) or not isinstance(values, list):
                raise SyncError(f"skip_uids for {email} must map folders to arrays")
            try:
                [int(value) for value in values]
            except (TypeError, ValueError) as exc:
                raise SyncError(
                    f"skip_uids for {email} folder {folder_name} must contain integers"
                ) from exc

        for field in ("sync_interval", "batch_size"):
            if field not in rule:
                continue
            try:
                value = int(rule[field])
            except (TypeError, ValueError) as exc:
                raise SyncError(f"{field} for {email} must be an integer") from exc
            if value < 1:
                raise SyncError(f"{field} for {email} must be at least 1")
