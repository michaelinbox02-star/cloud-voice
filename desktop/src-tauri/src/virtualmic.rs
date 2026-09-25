//! Brand the virtual audio cable so call apps show a recognisable microphone.
//!
//! Windows has no built-in virtual microphone, and a real one is a signed
//! kernel-mode driver: a WDK toolchain, an EV code-signing certificate and
//! Microsoft attestation signing. What "easy microphone selection" actually
//! needs is a device that appears in every app picker under a clear name, so
//! this renames the capture endpoint of an existing virtual cable - the same
//! operation the Sound control panel performs when a user renames a device.
//!
//! Writes need administrator rights, so the write is delegated to an elevated
//! PowerShell script. Reads happen in-process.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const BRANDED_NAME: &str = "Cloud Voice Microphone";
const FRIENDLY_NAME: &str = "{a45c254e-df1c-4efd-8020-67d146a850e0},2";
const DEVICE_DESC: &str = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6";
const CAPTURE_ROOT: &str = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture";
const DEVICE_STATE_ACTIVE: u32 = 1;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Endpoint {
    pub key: String,
    pub name: String,
    pub description: String,
    pub state: u32,
}

#[derive(Debug, Serialize)]
pub struct MicrophoneStatus {
    pub available: bool,
    pub branded: bool,
    pub endpoint: Option<String>,
    pub current_name: Option<String>,
    pub driver: Option<String>,
    pub note: String,
}

#[derive(Serialize, Deserialize)]
pub struct Backup {
    pub endpoint: String,
    pub original_name: String,
}

/// Endpoints that act as a virtual cable rather than a physical microphone.
///
/// Deliberately narrow: "virtual audio" alone also matches unrelated products
/// the user may rely on for something else.
pub fn is_virtual_cable(description: &str) -> bool {
    let text = description.to_lowercase();
    text.contains("vb-audio virtual cable") || text.contains("voicemeeter")
}

/// Prefer VB-CABLE, then any other cable, and only consider active devices.
pub fn choose(endpoints: &[Endpoint]) -> Option<Endpoint> {
    let mut candidates: Vec<&Endpoint> = endpoints
        .iter()
        .filter(|endpoint| {
            endpoint.state == DEVICE_STATE_ACTIVE && is_virtual_cable(&endpoint.description)
        })
        .collect();
    candidates.sort_by_key(|endpoint| {
        if endpoint.description.to_lowercase().contains("vb-audio virtual cable") {
            0
        } else {
            1
        }
    });
    candidates.first().map(|endpoint| (*endpoint).clone())
}

#[cfg(windows)]
fn enumerate() -> Result<Vec<Endpoint>, String> {
    use winreg::enums::{HKEY_LOCAL_MACHINE, KEY_READ};
    use winreg::RegKey;

    let root = RegKey::predef(HKEY_LOCAL_MACHINE);
    let capture = root
        .open_subkey_with_flags(CAPTURE_ROOT, KEY_READ)
        .map_err(|error| format!("Could not read the audio endpoint registry: {error}"))?;

    let mut endpoints = Vec::new();
    for key in capture.enum_keys().flatten() {
        let Ok(endpoint_key) = capture.open_subkey_with_flags(&key, KEY_READ) else {
            continue;
        };
        let state: u32 = endpoint_key.get_value("DeviceState").unwrap_or_default();
        let Ok(properties) = endpoint_key.open_subkey_with_flags("Properties", KEY_READ) else {
            continue;
        };
        let name: String = properties.get_value(FRIENDLY_NAME).unwrap_or_default();
        let description: String = properties.get_value(DEVICE_DESC).unwrap_or_default();
        if name.is_empty() && description.is_empty() {
            continue;
        }
        endpoints.push(Endpoint {
            key,
            name,
            description,
            state,
        });
    }
    Ok(endpoints)
}

#[cfg(not(windows))]
fn enumerate() -> Result<Vec<Endpoint>, String> {
    Err("Virtual microphone branding is Windows-only.".into())
}

pub fn status() -> Result<MicrophoneStatus, String> {
    let endpoints = enumerate()?;
    let Some(endpoint) = choose(&endpoints) else {
        return Ok(MicrophoneStatus {
            available: false,
            branded: false,
            endpoint: None,
            current_name: None,
            driver: None,
            note: "No virtual audio cable found. Install VB-CABLE, reboot once, then reopen this page."
                .into(),
        });
    };
    let branded = endpoint.name == BRANDED_NAME;
    Ok(MicrophoneStatus {
        available: true,
        branded,
        endpoint: Some(endpoint.key),
        current_name: Some(endpoint.name.clone()),
        driver: Some(endpoint.description.clone()),
        note: if branded {
            "This cable is already branded, so call apps list it as Cloud Voice Microphone.".into()
        } else {
            format!(
                "Found {}. Renaming it makes every app list it as Cloud Voice Microphone.",
                endpoint.description
            )
        },
    })
}

/// The script the elevated helper runs. Kept as one auditable action, with a
/// dry-run mode so the target can be checked without changing anything.
pub fn branding_script(endpoint: &str, target_name: &str) -> String {
    let mut script = String::new();
    script.push_str("param([switch]$DryRun)\n");
    script.push_str("$ErrorActionPreference = 'Stop'\n");
    script.push_str(&format!("$endpoint = '{endpoint}'\n"));
    script.push_str(&format!("$valueName = '{FRIENDLY_NAME}'\n"));
    script.push_str(
        "$keyPath = \"HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\Audio\\Capture\\$endpoint\\Properties\"\n",
    );
    script.push_str(
        "$current = (Get-ItemProperty -LiteralPath $keyPath -Name $valueName -ErrorAction SilentlyContinue).$valueName\n",
    );
    script.push_str("if ($DryRun) {\n");
    script.push_str("  Write-Output \"endpoint=$endpoint\"\n");
    script.push_str("  Write-Output \"current=$current\"\n");
    script.push_str(&format!("  Write-Output \"target={target_name}\"\n"));
    script.push_str("  exit 0\n");
    script.push_str("}\n");
    script.push_str(&format!(
        "Set-ItemProperty -LiteralPath $keyPath -Name $valueName -Value '{target_name}' -Type String\n"
    ));
    script.push_str("$written = (Get-ItemProperty -LiteralPath $keyPath -Name $valueName).$valueName\n");
    script.push_str("Write-Output \"written=$written\"\n");
    script
}

pub fn backup_path(data_dir: &Path) -> PathBuf {
    data_dir.join("virtual-microphone.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Captured from a real machine: one VB-CABLE, an unrelated virtual device,
    /// a physical microphone, and an inactive duplicate.
    fn fixture() -> Vec<Endpoint> {
        vec![
            Endpoint {
                key: "{17614af0-9aa1-43b7-bedb-f38656418717}".into(),
                name: "CABLE Output".into(),
                description: "VB-Audio Virtual Cable".into(),
                state: 1,
            },
            Endpoint {
                key: "{e1deed9f-721f-4310-b6dc-0ca2557dd5b4}".into(),
                name: "VoiceWave Microphone".into(),
                description: "Pyzra Virtual Audio".into(),
                state: 1,
            },
            Endpoint {
                key: "{5bc74438-0c11-423b-9018-6ea83d8f8886}".into(),
                name: "Microphone Array".into(),
                description: "Intel(R) Smart Sound Technology".into(),
                state: 1,
            },
            Endpoint {
                key: "{dead-0000}".into(),
                name: "CABLE Output".into(),
                description: "VB-Audio Virtual Cable".into(),
                state: 4,
            },
        ]
    }

    #[test]
    fn picks_the_vb_cable() {
        let chosen = choose(&fixture()).expect("a cable should be found");
        assert_eq!(chosen.key, "{17614af0-9aa1-43b7-bedb-f38656418717}");
        assert_eq!(chosen.name, "CABLE Output");
    }

    #[test]
    fn ignores_unrelated_virtual_devices() {
        assert!(!is_virtual_cable("Pyzra Virtual Audio"));
    }

    #[test]
    fn ignores_inactive_endpoints() {
        let only_inactive = vec![Endpoint {
            key: "{dead-0000}".into(),
            name: "CABLE Output".into(),
            description: "VB-Audio Virtual Cable".into(),
            state: 4,
        }];
        assert!(choose(&only_inactive).is_none());
    }

    #[test]
    fn prefers_vb_cable_over_other_cables() {
        let mut endpoints = fixture();
        endpoints.push(Endpoint {
            key: "{vm}".into(),
            name: "Voicemeeter Out B1".into(),
            description: "VoiceMeeter Virtual Audio Device".into(),
            state: 1,
        });
        let chosen = choose(&endpoints).expect("a cable should be found");
        assert!(chosen.description.contains("VB-Audio Virtual Cable"));
    }

    #[test]
    fn script_targets_the_expected_endpoint() {
        let script = branding_script("{17614af0-9aa1-43b7-bedb-f38656418717}", BRANDED_NAME);
        assert!(script.contains("{17614af0-9aa1-43b7-bedb-f38656418717}"));
        assert!(script.contains(FRIENDLY_NAME));
        assert!(script.contains(BRANDED_NAME));
        assert!(script.contains("Set-ItemProperty"));
    }

    /// Reads this machine's real audio endpoints. Run with:
    /// `cargo test --manifest-path desktop/src-tauri/Cargo.toml -- --ignored --nocapture`
    #[test]
    #[ignore = "depends on the host's installed audio devices"]
    fn reports_this_machine() {
        let status = status().expect("registry should be readable");
        println!("available={} branded={}", status.available, status.branded);
        println!("endpoint={:?}", status.endpoint);
        println!("current_name={:?}", status.current_name);
        println!("driver={:?}", status.driver);
        println!("note={}", status.note);
    }

    /// Proves the generated script parses, resolves the endpoint key and reads
    /// the current name, without writing anything.
    #[test]
    #[ignore = "requires PowerShell and an installed virtual cable"]
    fn dry_runs_the_branding_script() {
        let status = status().expect("registry should be readable");
        let endpoint = status.endpoint.clone().expect("a cable should be installed");
        let script = branding_script(&endpoint, BRANDED_NAME);
        let path = std::env::temp_dir().join("cloud-voice-mic-dryrun.ps1");
        std::fs::write(&path, script).expect("script should be writable");

        let output = std::process::Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
            .arg(&path)
            .arg("-DryRun")
            .output()
            .expect("PowerShell should be available");
        let text = String::from_utf8_lossy(&output.stdout);
        let errors = String::from_utf8_lossy(&output.stderr);
        println!("{text}");
        assert!(output.status.success(), "script failed: {errors}");
        assert!(text.contains(&endpoint), "endpoint missing from output");
        assert_eq!(
            status.current_name.as_deref(),
            Some("CABLE Output"),
            "unexpected starting name"
        );
        assert!(text.contains(BRANDED_NAME), "target missing from output");
    }
}
