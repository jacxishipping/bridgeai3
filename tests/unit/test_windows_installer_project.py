"""Contract & structural validation for the native C# WPF Windows GUI Installer."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = REPO_ROOT / "apps" / "windows-installer"
CLIENT_DIR = REPO_ROOT / "apps" / "windows-client"


def test_installer_xml_and_xaml_files_are_well_formed():
    xml_files = [
        INSTALLER_DIR / "AIBridgeInstaller.csproj",
        INSTALLER_DIR / "Properties" / "PublishProfiles" / "win-x64-singlefile.pubxml",
        INSTALLER_DIR / "App.xaml",
        INSTALLER_DIR / "MainWindow.xaml",
    ]
    for path in xml_files:
        assert path.is_file(), f"Missing installer file: {path}"
        ET.parse(path)


def test_installer_csproj_targets_wpf_net9_and_optional_embedded_payload():
    csproj = (INSTALLER_DIR / "AIBridgeInstaller.csproj").read_text(encoding="utf-8")
    assert "<TargetFramework>net9.0-windows</TargetFramework>" in csproj
    assert "<UseWPF>true</UseWPF>" in csproj
    assert "<AssemblyName>AIBridgeSetup</AssemblyName>" in csproj
    assert "AIBridgeInstaller.Payload.zip" in csproj


def test_installer_dpapi_and_registry_contracts_match_client():
    sealer = (INSTALLER_DIR / "Services" / "DpapiSealer.cs").read_text(encoding="utf-8")
    client_dpapi = (CLIENT_DIR / "Services" / "DpapiTokenStore.cs").read_text(encoding="utf-8")
    assert 'DpapiMagic = "aib-dpapi-v1:"' in sealer
    assert 'DpapiMagic = "aib-dpapi-v1:"' in client_dpapi

    reg_svc = (INSTALLER_DIR / "Services" / "ShortcutAndRegistryService.cs").read_text(encoding="utf-8")
    client_cfg = (CLIENT_DIR / "Services" / "ConfigService.cs").read_text(encoding="utf-8")
    assert 'RegRunValueName = "AIBridgeWindowsClient"' in reg_svc
    assert 'RegAppName = "AIBridgeWindowsClient"' in client_cfg
    assert "Uninstall\\AIBridgeWindowsClient" in reg_svc


def test_installer_xaml_event_handlers_exist_in_code_behind():
    xaml = (INSTALLER_DIR / "MainWindow.xaml").read_text(encoding="utf-8")
    code = (INSTALLER_DIR / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    handlers = set(re.findall(r'(?:Click|TextChanged)="([A-Za-z0-9_]+)"', xaml))
    assert handlers, "Expected event handlers in MainWindow.xaml"
    for handler in sorted(handlers):
        assert f"void {handler}(" in code, f"Handler {handler} referenced in MainWindow.xaml missing in MainWindow.xaml.cs"

    # Every x:Name in XAML should be unique
    names = re.findall(r'x:Name="([A-Za-z0-9_]+)"', xaml)
    assert len(names) == len(set(names)), "Duplicate x:Name found in MainWindow.xaml"

    # Every DynamicResource/StaticResource/FindResource key must exist in App.xaml
    app_xaml = (INSTALLER_DIR / "App.xaml").read_text(encoding="utf-8")
    defined_keys = set(re.findall(r'x:Key="([A-Za-z0-9_]+)"', app_xaml))
    used_keys = set(re.findall(r'FindResource\("([A-Za-z0-9_]+)"\)', code)) | set(
        re.findall(r'(?:Dynamic|Static)Resource\s+([A-Za-z0-9_]+)', xaml)
    )
    missing = sorted(used_keys - defined_keys)
    assert not missing, f"Undefined XAML resource keys referenced: {missing}"


def test_build_script_and_ci_wiring_exist():
    build_script = REPO_ROOT / "scripts" / "build_windows_installer.ps1"
    assert build_script.is_file()
    ps_src = build_script.read_text(encoding="utf-8")
    assert "AIBridgeClient.csproj" in ps_src
    assert "AIBridgeInstaller.csproj" in ps_src
    assert "AIBridgeClientPayload.zip" in ps_src
    assert "AIBridgeSetup.exe" in ps_src

    ci_yml = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "dotnet build apps/windows-installer/AIBridgeInstaller.csproj -c Release" in ci_yml
