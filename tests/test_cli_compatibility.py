"""CLI レジストリと package.xml の互換性検査に対する回帰テスト。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metadata_fetcher as metadata_fetcher
import package_generator as package_generator


def metadata_type_result(*names: str) -> dict[str, object]:
    return {"metadataObjects": [{"xmlName": name} for name in names]}


def compatibility_data(*, compatible: list[str], unsupported: list[str], discovered: list[str] | None = None) -> dict[str, object]:
    discovered = discovered or [*compatible, *unsupported]
    all_type_map = {name.casefold(): name for name in discovered}
    return {
        "official_map": {name.casefold(): name for name in compatible},
        "all_type_map": all_type_map,
        "unsupported_types": unsupported,
        "wildcard_types": compatible,
        "individual_types": [],
        "discovered_count": len(discovered),
        "compatible_count": len(compatible),
        "message": "test",
    }


class CliCompatibilityTests(unittest.TestCase):
    def test_type_discovery_excludes_cli_unregistered_type(self) -> None:
        calls: list[list[str]] = []

        def fake_run(arguments: list[str], *, api_version: str = "60.0") -> tuple[bool, object, str]:
            calls.append(arguments)
            if "--filter-known" in arguments:
                return True, metadata_type_result("activationplatformactvattr"), ""
            return True, metadata_type_result("ApexClass", "Report", "ActivationPlatformActvAttr"), ""

        with patch.object(package_generator, "_run_sf_json", side_effect=fake_run):
            success, data = package_generator.get_cli_compatible_metadata_types("mydev", "60.0")

        self.assertTrue(success)
        self.assertEqual(data["wildcard_types"], ["ApexClass"])
        self.assertEqual(data["individual_types"], ["Report"])
        self.assertEqual(data["unsupported_types"], ["ActivationPlatformActvAttr"])
        self.assertEqual(data["discovered_count"], 3)
        self.assertIn("--filter-known", calls[1])

    def test_filter_command_failure_is_fail_closed(self) -> None:
        responses = iter([
            (True, metadata_type_result("ApexClass", "ActivationPlatformActvAttr"), ""),
            (False, None, "unknown flag: --filter-known"),
        ])
        with patch.object(package_generator, "_run_sf_json", side_effect=lambda *args, **kwargs: next(responses)):
            success, data = package_generator.get_cli_compatible_metadata_types("mydev", "60.0")

        self.assertFalse(success)
        self.assertIn("安全のため", data["message"])
        self.assertNotIn("ActivationPlatformActvAttr", data["wildcard_types"])

    def test_child_type_of_a_registered_parent_is_valid_but_not_auto_listed(self) -> None:
        normal = {
            "metadataObjects": [
                {"xmlName": "CustomObject", "childXmlNames": ["CustomField"]},
                {"xmlName": "UnsupportedParent", "childXmlNames": ["UnsupportedChild"]},
            ]
        }
        filtered = {
            "metadataObjects": [
                {"xmlName": "UnsupportedParent", "childXmlNames": ["UnsupportedChild"]},
            ]
        }
        responses = iter([(True, normal, ""), (True, filtered, "")])
        with patch.object(package_generator, "_run_sf_json", side_effect=lambda *args, **kwargs: next(responses)):
            success, data = package_generator.get_cli_compatible_metadata_types("mydev", "60.0")

        self.assertTrue(success)
        self.assertEqual(data["wildcard_types"], ["CustomObject"])
        self.assertNotIn("CustomField", data["wildcard_types"])
        with patch.object(package_generator, "get_cli_compatible_metadata_types", return_value=(True, data)):
            checked, compatible, unsupported, unavailable, _message = package_generator.validate_retrieve_metadata_types(
                "mydev", ["CustomField", "UnsupportedChild"], "60.0"
            )
        self.assertTrue(checked)
        self.assertEqual(compatible, ["CustomField"])
        self.assertEqual(unsupported, ["UnsupportedChild"])
        self.assertEqual(unavailable, [])

    def test_generation_excludes_manual_unregistered_type_without_substitution(self) -> None:
        data = compatibility_data(
            compatible=["ApexClass"],
            unsupported=["ActivationPlatformActvAttr"],
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package.xml"
            with patch.object(package_generator, "get_cli_compatible_metadata_types", return_value=(True, data)):
                success, message = package_generator.generate_custom_package_xml(
                    "mydev", ["ApexClass", "ActivationPlatformActvAttr"], [], output_path=output
                )

            content = output.read_text(encoding="utf-8")
        self.assertTrue(success)
        self.assertIn("ApexClass", content)
        self.assertNotIn("ActivationPlatformActvAttr", content)
        self.assertNotIn("<name>ActivationPlatform</name>", content)
        self.assertIn("ActivationPlatformActvAttr", message)

    def test_all_rejected_types_do_not_overwrite_existing_manifest(self) -> None:
        data = compatibility_data(compatible=[], unsupported=["ActivationPlatformActvAttr"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "package.xml"
            output.write_text("previous manifest", encoding="utf-8")
            with patch.object(package_generator, "get_cli_compatible_metadata_types", return_value=(True, data)):
                success, message = package_generator.generate_custom_package_xml(
                    "mydev", ["ActivationPlatformActvAttr"], [], output_path=output
                )

            self.assertFalse(success)
            self.assertIn("Package.xml は作成しませんでした", message)
            self.assertEqual(output.read_text(encoding="utf-8"), "previous manifest")
            self.assertFalse((Path(directory) / "package.xml.tmp").exists())

    def test_manifest_guard_rejects_unregistered_and_unknown_types(self) -> None:
        data = compatibility_data(
            compatible=["ApexClass"],
            unsupported=["ActivationPlatformActvAttr"],
        )
        manifest = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Package xmlns=\"http://soap.sforce.com/2006/04/metadata\">
  <types><members>*</members><name>ApexClass</name></types>
  <types><members>*</members><name>ActivationPlatformActvAttr</name></types>
  <types><members>*</members><name>NotARealType</name></types>
  <version>60.0</version>
</Package>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.xml"
            path.write_text(manifest, encoding="utf-8")
            with patch.object(package_generator, "get_cli_compatible_metadata_types", return_value=(True, data)):
                success, message = package_generator.validate_package_xml_compatibility("mydev", path)

        self.assertFalse(success)
        self.assertIn("ActivationPlatformActvAttr", message)
        self.assertIn("NotARealType", message)

    def test_retrieve_stops_before_popen_when_manifest_is_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "package.xml"
            manifest.write_text("<Package><types><name>BadType</name></types></Package>", encoding="utf-8")
            with (
                patch.object(metadata_fetcher, "ROOT_DIR", Path(directory)),
                patch.object(metadata_fetcher.shutil, "which", return_value="C:/sf/bin/sf.exe"),
                patch.object(metadata_fetcher.package_generator, "validate_package_xml_compatibility", return_value=(False, "BadType is unsupported")),
                patch.object(metadata_fetcher.config_manager, "clean_temp_dir", return_value=True) as clean_temp,
                patch.object(metadata_fetcher.subprocess, "Popen") as popen,
            ):
                events = list(metadata_fetcher.retrieve_metadata_stream("mydev", manifest))

        self.assertEqual(events[-1], ("error", "BadType is unsupported"))
        clean_temp.assert_called_once()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
