"""Salesforce metadata type の取得と package.xml の生成。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

ROOT_DIR = Path(__file__).resolve().parent
NAMESPACE = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", NAMESPACE)
FOLDERED_TYPES = {"Report", "Dashboard", "Document", "EmailTemplate"}


def _run_json(command: list[str]) -> tuple[object | None, str]:
    if not shutil.which("sf"):
        return None, "Salesforce CLI (sf) が PATH 上に見つかりません。"
    result = subprocess.run(command + ["--json"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    message = (result.stdout + result.stderr).strip()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, message
    if result.returncode != 0 or payload.get("status", 0) != 0:
        return None, payload.get("message", message)
    return payload.get("result", payload), message


def get_org_metadata_types(target_org: str) -> tuple[list[str], str]:
    result, message = _run_json(["sf", "org", "list", "metadata-types", "--target-org", target_org])
    if result is None:
        return [], message
    values = result.get("metadataTypes", result) if isinstance(result, dict) else result
    types = []
    for item in values if isinstance(values, list) else []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name:
            types.append(name)
    return sorted(set(types)), message


def list_metadata_members(target_org: str, metadata_type: str) -> tuple[list[str], str]:
    result, message = _run_json(["sf", "org", "list", "metadata", "--metadata", metadata_type, "--target-org", target_org])
    if result is None:
        return [], message
    values = result.get("metadata", result) if isinstance(result, dict) else result
    members: list[str] = []
    for item in values if isinstance(values, list) else []:
        name = item.get("fullName") if isinstance(item, dict) else str(item)
        if name:
            members.append(name)
    return sorted(set(members)), message


def generate_hybrid_package_xml(target_org: str, wildcard_types: list[str], named_types: list[str],
                                api_version: str) -> tuple[Path, list[str]]:
    """個別タイプを org から列挙し、整形済み manifest を保存する。"""
    warnings: list[str] = []
    package = ET.Element(f"{{{NAMESPACE}}}Package")
    for metadata_type in sorted(set(filter(None, wildcard_types))):
        types = ET.SubElement(package, f"{{{NAMESPACE}}}types")
        ET.SubElement(types, f"{{{NAMESPACE}}}members").text = "*"
        ET.SubElement(types, f"{{{NAMESPACE}}}name").text = metadata_type
    for metadata_type in sorted(set(filter(None, named_types))):
        members, message = list_metadata_members(target_org, metadata_type)
        if not members:
            warnings.append(f"{metadata_type}: 個別資材が取得できなかったため省略しました。{message}")
            continue
        types = ET.SubElement(package, f"{{{NAMESPACE}}}types")
        for member in members:
            ET.SubElement(types, f"{{{NAMESPACE}}}members").text = member
        ET.SubElement(types, f"{{{NAMESPACE}}}name").text = metadata_type
    ET.SubElement(package, f"{{{NAMESPACE}}}version").text = api_version or "60.0"
    xml = minidom.parseString(ET.tostring(package, encoding="utf-8")).toprettyxml(indent="  ", encoding="utf-8")
    path = ROOT_DIR / "data" / target_org / "package.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml)
    return path, warnings
