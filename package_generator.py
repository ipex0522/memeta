"""Salesforce metadata type の照会と、編集可能な package.xml の生成。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from xml.dom import minidom
from xml.etree import ElementTree as ET

ROOT_DIR = Path(__file__).resolve().parent
TEMP_DIR = ROOT_DIR / "temp"
NAMESPACE = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", NAMESPACE)

# 画面での複数形・小文字の入力を、Metadata API の正式名に補正する。
TYPE_ALIAS_MAP = {
    "reports": "Report", "report": "Report",
    "dashboards": "Dashboard", "dashboard": "Dashboard",
    "documents": "Document", "document": "Document",
    "emailtemplates": "EmailTemplate", "emailtemplate": "EmailTemplate",
    "apexclasses": "ApexClass", "apexclass": "ApexClass",
    "apextriggers": "ApexTrigger", "apextrigger": "ApexTrigger",
    "customobjects": "CustomObject", "customobject": "CustomObject",
    "customfields": "CustomField", "customfield": "CustomField",
    "standardvaluesets": "StandardValueSet", "standardvalueset": "StandardValueSet",
}
NON_WILDCARD_TYPES = {"Report", "Dashboard", "Document", "EmailTemplate", "StandardValueSet"}
FOLDER_TYPE_MAP = {
    "Report": ("ReportFolder",),
    "Dashboard": ("DashboardFolder",),
    "Document": ("DocumentFolder",),
    "EmailTemplate": ("EmailFolder", "EmailTemplateFolder"),
}
DEFAULT_WILDCARD_TYPES = [
    "ApexClass", "ApexComponent", "ApexPage", "ApexTrigger", "AuraDefinitionBundle",
    "CustomApplication", "CustomObject", "CustomTab", "Flow", "Layout",
    "LightningComponentBundle", "PermissionSet", "Profile", "StaticResource", "Workflow",
]
DEFAULT_INDIVIDUAL_TYPES = ["Report", "Dashboard", "Document", "EmailTemplate"]


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def create_default_project_files(api_version: str = "60.0") -> Path:
    """CLI 作業領域に最小 SFDX プロジェクトを作成する。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # sfdx-project.json の packageDirectories に指定するパスは、
    # retrieve 実行前から実在している必要がある。
    (TEMP_DIR / "force-app").mkdir(parents=True, exist_ok=True)
    project_path = TEMP_DIR / "sfdx-project.json"
    project = {
        "packageDirectories": [{"path": "force-app", "default": True}],
        "name": "MemetaProject",
        "namespace": "",
        "sourceApiVersion": api_version or "60.0",
    }
    with project_path.open("w", encoding="utf-8", errors="replace") as handle:
        json.dump(project, handle, ensure_ascii=False, indent=2)
    return project_path


def _run_sf_json(arguments: list[str], *, api_version: str = "60.0") -> tuple[bool, Any, str]:
    sf_path = shutil.which("sf")
    if not sf_path:
        return False, None, "Salesforce CLI (sf) が PATH 上に見つかりません。`sf --version` を確認してください。"
    create_default_project_files(api_version)
    command = [os.path.abspath(sf_path), *arguments, "--api-version", api_version or "60.0", "--json"]
    try:
        result = subprocess.run(command, cwd=TEMP_DIR, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", check=False, creationflags=_creation_flags())
    except OSError as error:
        return False, None, f"Salesforce CLI を起動できませんでした: {error}"
    output = (result.stdout + result.stderr).strip()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, None, output or "Salesforce CLI の JSON 出力を読み取れませんでした。"
    if result.returncode != 0 or payload.get("status", 0) != 0:
        return False, None, str(payload.get("message") or output or "Salesforce CLI の実行に失敗しました。")
    return True, payload.get("result", payload), output


def normalize_metadata_type_name(raw_type: str, official_map: dict[str, str] | None = None) -> str:
    cleaned = raw_type.strip()
    if not cleaned:
        return ""
    lower = cleaned.lower()
    if official_map and lower in official_map:
        return official_map[lower]
    if lower in TYPE_ALIAS_MAP:
        return TYPE_ALIAS_MAP[lower]
    # API 名を推測して壊さない。未知の入力はユーザー入力をそのまま使う。
    return cleaned


def _fallback_metadata_type_data(message: str) -> dict[str, Any]:
    """互換性を確認できない場合にだけ使う、限定された既定タイプ。"""
    defaults = DEFAULT_WILDCARD_TYPES + DEFAULT_INDIVIDUAL_TYPES
    return {
        "wildcard_types": DEFAULT_WILDCARD_TYPES.copy(),
        "individual_types": DEFAULT_INDIVIDUAL_TYPES.copy(),
        "official_map": {name.casefold(): name for name in defaults},
        "all_type_map": {name.casefold(): name for name in defaults},
        "unsupported_types": [],
        "unsupported_type_map": {},
        "discovered_count": 0,
        "compatible_count": len(defaults),
        "message": message,
    }


def _extract_metadata_type_records(result: Any) -> list[tuple[str, list[str]]]:
    """``org list metadata-types`` の親型名と childXmlNames を抽出する。"""
    if isinstance(result, dict):
        records = result.get("metadataObjects", result.get("metadataTypes", result.get("types", [])))
    else:
        records = result
    if isinstance(records, dict):
        records = [records]
    extracted: dict[str, tuple[str, list[str]]] = {}
    for record in records if isinstance(records, list) else []:
        name = record.get("xmlName", record.get("name", "")) if isinstance(record, dict) else str(record)
        name = str(name).strip()
        if name:
            child_values = record.get("childXmlNames", []) if isinstance(record, dict) else []
            children: dict[str, str] = {}
            for child in child_values if isinstance(child_values, list) else []:
                child_name = str(child).strip()
                if child_name:
                    children.setdefault(child_name.casefold(), child_name)
            extracted.setdefault(name.casefold(), (name, sorted(children.values(), key=str.casefold)))
    return sorted(extracted.values(), key=lambda item: item[0].casefold())


def get_cli_compatible_metadata_types(target_org: str, api_version: str = "60.0") -> tuple[bool, dict[str, Any]]:
    """組織の型一覧を、実行中 Salesforce CLI のレジストリで照合する。

    ``--filter-known`` は名前とは逆に、CLI のレジストリに *未登録* の型だけを返す。
    組織の DescribeMetadata に存在しても、source 形式へ変換できない型を
    package.xml へ入れないために、通常結果との差分を利用する。
    """
    base_arguments = ["org", "list", "metadata-types", "--target-org", target_org]
    success, result, message = _run_sf_json(base_arguments, api_version=api_version)
    if not success:
        return False, _fallback_metadata_type_data(
            f"タイプ一覧を取得できませんでした。既定一覧を表示しています。\n{message}"
        )

    discovered_records = _extract_metadata_type_records(result)
    if not discovered_records:
        return False, _fallback_metadata_type_data(
            "組織から有効なメタデータタイプを取得できませんでした。既定一覧を表示しています。"
        )

    filter_success, filter_result, filter_message = _run_sf_json(
        [*base_arguments, "--filter-known"], api_version=api_version
    )
    if not filter_success:
        return False, _fallback_metadata_type_data(
            "Salesforce CLI のメタデータ互換性を確認できませんでした。"
            "安全のため、組織から取得した全型は Package.xml に使用しません。\n"
            f"詳細: {filter_message}"
        )

    discovered_parent_map = {name.casefold(): name for name, _children in discovered_records}
    unregistered_parent_keys = {
        name.casefold() for name, _children in _extract_metadata_type_records(filter_result)
    }
    unsupported = sorted(
        (name for key, name in discovered_parent_map.items() if key in unregistered_parent_keys), key=str.casefold
    )
    compatible_parents = {
        key: name for key, name in discovered_parent_map.items() if key not in unregistered_parent_keys
    }
    if not compatible_parents:
        return False, _fallback_metadata_type_data(
            "Salesforce CLI のレジストリで取得可能なメタデータタイプを確認できませんでした。"
            "安全のため、組織から取得した全型は Package.xml に使用しません。"
        )

    # childXmlNames (例: CustomField) は親型が CLI 登録済みなら manifest で
    # 有効になり得る。ただし、一覧へ自動追加すると wildcard 取得を誤って増やすため、
    # 画面に出すのは親型だけにし、手入力・既存manifestの検査にだけ利用する。
    all_type_map = dict(discovered_parent_map)
    official_map = dict(compatible_parents)
    unsupported_type_map = {key: name for key, name in discovered_parent_map.items() if key in unregistered_parent_keys}
    for parent_name, children in discovered_records:
        parent_key = parent_name.casefold()
        for child_name in children:
            child_key = child_name.casefold()
            all_type_map.setdefault(child_key, child_name)
            if parent_key in unregistered_parent_keys:
                unsupported_type_map.setdefault(child_key, child_name)
            else:
                official_map.setdefault(child_key, child_name)

    compatible = sorted(compatible_parents.values(), key=str.casefold)
    wildcard = [name for name in compatible if name not in NON_WILDCARD_TYPES]
    individual = [name for name in compatible if name in NON_WILDCARD_TYPES]
    detail = (
        f"組織検出: {len(discovered_parent_map)} 種 / CLI互換: {len(compatible)} 種 / "
        f"CLI未対応: {len(unsupported)} 種"
    )
    if message:
        detail += f"\n{message}"
    return True, {
        "wildcard_types": wildcard,
        "individual_types": individual,
        "official_map": official_map,
        "all_type_map": all_type_map,
        "unsupported_types": unsupported,
        "unsupported_type_map": unsupported_type_map,
        "discovered_count": len(discovered_parent_map),
        "compatible_count": len(compatible),
        "message": detail,
    }


def fetch_org_metadata_types(target_org: str, api_version: str = "60.0") -> tuple[bool, dict[str, Any]]:
    """CLI で source 形式に取得できる型だけを画面用に返す。"""
    return get_cli_compatible_metadata_types(target_org, api_version)


def _format_type_names(names: list[str]) -> str:
    return ", ".join(names) if names else "なし"


def validate_retrieve_metadata_types(target_org: str, metadata_types: list[str],
                                     api_version: str = "60.0") -> tuple[bool, list[str], list[str], list[str], str]:
    """指定型を、組織の有効型かつ CLI 登録済みの型に限定する。

    戻り値は ``(検査実行可否, 互換型, CLI未対応型, 組織で確認不能な型, メッセージ)``。
    検査自体が失敗した場合は安全側で ``False`` を返す。
    """
    success, data = get_cli_compatible_metadata_types(target_org, api_version)
    if not success:
        return False, [], [], [], str(data.get("message", "CLI互換性を確認できませんでした。"))

    all_type_map = data.get("all_type_map", {})
    compatible_map = data.get("official_map", {})
    unsupported_map = data.get("unsupported_type_map") or {
        name.casefold(): name for name in data.get("unsupported_types", [])
    }
    compatible: list[str] = []
    unsupported: list[str] = []
    unavailable: list[str] = []
    seen: set[str] = set()
    for raw_type in metadata_types:
        name = normalize_metadata_type_name(str(raw_type), all_type_map)
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        if key in compatible_map:
            compatible.append(compatible_map[key])
        elif key in unsupported_map:
            unsupported.append(unsupported_map[key])
        else:
            unavailable.append(name)

    message = (
        f"CLI互換性を確認しました。互換: {len(compatible)} 種 / "
        f"CLI未対応: {len(unsupported)} 種 / 組織で確認不能: {len(unavailable)} 種"
    )
    return True, compatible, unsupported, unavailable, message


def _manifest_type_names(manifest_path: str | Path) -> tuple[bool, list[str], str]:
    """namespace の有無に依存せず package.xml の ``types/name`` を読む。"""
    try:
        root = ET.parse(manifest_path).getroot()
    except (ET.ParseError, OSError) as error:
        return False, [], f"Package.xml を読み取れません: {error}"
    names: dict[str, str] = {}
    for types in root.iter():
        if types.tag.rsplit("}", 1)[-1] != "types":
            continue
        for child in types:
            if child.tag.rsplit("}", 1)[-1] != "name" or not child.text:
                continue
            name = child.text.strip()
            if name:
                names.setdefault(name.casefold(), name)
            break
    if not names:
        return False, [], "Package.xml に取得対象のメタデータタイプがありません。"
    return True, list(names.values()), ""


def validate_package_xml_compatibility(target_org: str, manifest_path: str | Path,
                                       api_version: str = "60.0") -> tuple[bool, str]:
    """既存または手編集済みの Package.xml を retrieve 前に検査する。"""
    parsed, types, message = _manifest_type_names(manifest_path)
    if not parsed:
        return False, message
    checked, compatible, unsupported, unavailable, check_message = validate_retrieve_metadata_types(
        target_org, types, api_version
    )
    if not checked:
        return False, (
            "Package.xml の CLI互換性を確認できないため、取得を開始しません。\n"
            f"{check_message}"
        )
    if unsupported or unavailable:
        details: list[str] = ["Package.xml に Salesforce CLI で source 形式へ取得できない型が含まれています。"]
        if unsupported:
            details.append(f"CLIレジストリ未対応: {_format_type_names(unsupported)}")
        if unavailable:
            details.append(f"組織で有効な型として確認できない: {_format_type_names(unavailable)}")
        details.append("Package.xml を再生成するか、該当型を確認してから再実行してください。")
        return False, "\n".join(details)
    return True, f"Package.xml の CLI互換性を確認しました。対象: {len(compatible)} 種"


def _list_metadata_items(target_org: str, metadata_type: str, api_version: str,
                         folder: str | None = None) -> tuple[bool, list[str], str]:
    """CLI の list metadata 結果から fullName のみを抽出する。"""
    arguments = ["org", "list", "metadata", "--metadata-type", metadata_type, "--target-org", target_org]
    if folder:
        arguments.extend(["--folder", folder])
    success, result, message = _run_sf_json(
        arguments, api_version=api_version
    )
    if not success:
        return False, [], message
    if isinstance(result, dict):
        records = result.get("metadata", result.get("records", result))
    else:
        records = result
    if isinstance(records, dict):
        records = [records]
    members: list[str] = []
    for record in records if isinstance(records, list) else []:
        name = record.get("fullName", record.get("name", "")) if isinstance(record, dict) else str(record)
        if str(name).strip():
            members.append(str(name).strip())
    return True, sorted(set(members)), message


def fetch_individual_metadata_items(target_org: str, metadata_type: str,
                                    api_version: str = "60.0") -> tuple[bool, list[str], str]:
    """Report 等について、package.xml に入れる fullName を照会する。

    Salesforce CLI はフォルダ型に ``--folder`` を要求するため、先に対応する
    Folder メタデータを列挙してから各フォルダ内の資材を取得する。
    """
    folder_type = FOLDER_TYPE_MAP.get(metadata_type)
    if not folder_type:
        return _list_metadata_items(target_org, metadata_type, api_version)
    folders: list[str] = []
    folder_failures: list[str] = []
    for folder_metadata_type in folder_type:
        success, values, message = _list_metadata_items(target_org, folder_metadata_type, api_version, folder="*")
        if success:
            folders.extend(values)
        else:
            folder_failures.append(f"{folder_metadata_type}: {message}")
    if not folders:
        return False, [], f"{metadata_type} のフォルダ一覧を取得できませんでした。{' '.join(folder_failures)}"
    members: list[str] = []
    failures: list[str] = []
    for folder in sorted(set(folders)):
        success, values, message = _list_metadata_items(target_org, metadata_type, api_version, folder)
        if not success:
            failures.append(f"{folder}: {message}")
            continue
        members.extend(values)
    if failures:
        return False, [], f"{metadata_type} の一部フォルダを列挙できませんでした。{' '.join(failures)}"
    detail = "\n".join(failures)
    return True, sorted(set(members)), detail


def _unique_normalized(values: list[str], official_map: dict[str, str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = normalize_metadata_type_name(raw, official_map)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def generate_custom_package_xml(target_org: str, wildcard_types: list[str], individual_types: list[str], *,
                                api_version: str = "60.0", official_map: dict[str, str] | None = None,
                                output_path: str | Path | None = None) -> tuple[bool, str]:
    """編集されたタイプ一覧から、組織別 package.xml を生成する。"""
    wildcard = _unique_normalized(wildcard_types, official_map)
    individual = _unique_normalized(individual_types, official_map)
    if not wildcard and not individual:
        return False, "作成対象のメタデータタイプを1つ以上指定してください。"

    checked, compatible, unsupported, unavailable, check_message = validate_retrieve_metadata_types(
        target_org, wildcard + individual, api_version
    )
    if not checked:
        return False, (
            "Package.xml 作成前の Salesforce CLI互換性検査に失敗しました。\n"
            f"{check_message}\n安全のため Package.xml は作成しませんでした。"
        )
    compatible_keys = {name.casefold() for name in compatible}
    wildcard = [name for name in wildcard if name.casefold() in compatible_keys]
    individual = [name for name in individual if name.casefold() in compatible_keys]

    package = ET.Element(f"{{{NAMESPACE}}}Package")
    warnings: list[str] = []
    if unsupported:
        warnings.append(
            "Salesforce CLI のレジストリ未対応のため除外しました: "
            f"{_format_type_names(unsupported)}"
        )
    if unavailable:
        warnings.append(
            "組織で有効なメタデータ型として確認できないため除外しました: "
            f"{_format_type_names(unavailable)}"
        )
    if not wildcard and not individual:
        return False, (
            "CLI互換性検査の結果、取得可能なメタデータタイプがありません。\n"
            + "\n".join(warnings)
            + "\nPackage.xml は作成しませんでした。"
        )
    for metadata_type in wildcard:
        types = ET.SubElement(package, f"{{{NAMESPACE}}}types")
        ET.SubElement(types, f"{{{NAMESPACE}}}members").text = "*"
        ET.SubElement(types, f"{{{NAMESPACE}}}name").text = metadata_type
    added_individual = 0
    for metadata_type in individual:
        success, members, message = fetch_individual_metadata_items(target_org, metadata_type, api_version)
        if not success:
            warnings.append(f"{metadata_type}: 個別名を取得できなかったため、この型を省略しました。{message}")
            continue
        if not members:
            warnings.append(f"{metadata_type}: 取得対象の資材がありません。この型を省略しました。")
            continue
        types = ET.SubElement(package, f"{{{NAMESPACE}}}types")
        for member in members:
            ET.SubElement(types, f"{{{NAMESPACE}}}members").text = member
        ET.SubElement(types, f"{{{NAMESPACE}}}name").text = metadata_type
        added_individual += 1
    if not wildcard and not added_individual:
        return False, "個別名が必要なタイプを列挙できませんでした。Package.xml は作成しませんでした。\n" + "\n".join(warnings)
    ET.SubElement(package, f"{{{NAMESPACE}}}version").text = api_version or "60.0"

    destination = Path(output_path) if output_path else ROOT_DIR / "data" / target_org / "package.xml"
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    pretty = minidom.parseString(ET.tostring(package, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    temporary = destination.with_suffix(".xml.tmp")
    temporary.write_bytes(pretty)
    os.replace(temporary, destination)
    description = f"package.xml を生成しました。\n保存先: {destination}\n対象タイプ: {len(wildcard) + added_individual}"
    if warnings:
        description += "\n\n注意:\n" + "\n".join(warnings)
    return True, description


# 既存利用者向けの API 名。
def generate_hybrid_package_xml(target_org: str, wildcard_types: list[str], named_types: list[str],
                                api_version: str) -> tuple[Path, list[str]]:
    success, message = generate_custom_package_xml(target_org, wildcard_types, named_types, api_version=api_version)
    if not success:
        raise RuntimeError(message)
    return (ROOT_DIR / "data" / target_org / "package.xml").resolve(), [message]
