use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;
use std::process::Command;
use tauri::Manager;

const WORKER_RELEASE: &str = "efa11b2abac3392ae51eea3f3844bab3d1e3642e";

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ServerInput {
    host: String,
    port: u16,
    username: String,
    key_path: String,
    repository: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ServerResult {
    log: String,
    release: Option<String>,
}

fn valid_host(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('-')
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
}

fn valid_username(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('-')
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn valid_repository(value: &str) -> bool {
    let Some(path) = value.strip_prefix("https://github.com/") else {
        return false;
    };
    let parts: Vec<_> = path.trim_end_matches(".git").split('/').collect();
    parts.len() == 2
        && parts.iter().all(|part| {
            !part.is_empty()
                && !part.starts_with('.')
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
        })
}

fn validate(input: &ServerInput) -> Result<(), String> {
    if !valid_host(&input.host) || !valid_username(&input.username) || input.port == 0 {
        return Err("Enter a valid SSH host, port, and username.".into());
    }
    if !Path::new(&input.key_path).is_file() {
        return Err("The SSH private key file does not exist or is not readable.".into());
    }
    if !valid_repository(&input.repository) {
        return Err("Enter a GitHub repository URL such as https://github.com/owner/repo.".into());
    }
    Ok(())
}

fn ssh(app: &tauri::AppHandle, input: &ServerInput, remote_command: &str) -> Result<String, String> {
    validate(input)?;
    let data_dir = app.path().app_data_dir().map_err(|error| error.to_string())?;
    fs::create_dir_all(&data_dir).map_err(|error| error.to_string())?;
    let known_hosts = data_dir.join("known_hosts");
    let output = Command::new("ssh")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg("-o")
        .arg("ConnectTimeout=12")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg("-o")
        .arg(format!("UserKnownHostsFile={}", known_hosts.display()))
        .arg("-i")
        .arg(&input.key_path)
        .arg("-p")
        .arg(input.port.to_string())
        .arg(format!("{}@{}", input.username, input.host))
        .arg(remote_command)
        .output()
        .map_err(|error| format!("Could not start SSH: {error}"))?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr);
        return Err(format!("SSH command failed: {}", message.trim()));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

#[tauri::command]
fn probe_server(app: tauri::AppHandle, input: ServerInput) -> Result<ServerResult, String> {
    let command = "set -eu; . /etc/os-release; printf 'OS: %s %s\\n' \"$NAME\" \"$VERSION_ID\"; printf 'GPU: '; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader; printf 'Docker: '; docker --version; printf 'NVIDIA container toolkit: '; nvidia-container-cli --version | head -1; sudo -n true; printf 'Sudo: available\\n'; df -h / | tail -1";
    Ok(ServerResult {
        log: ssh(&app, &input, command)?,
        release: None,
    })
}

#[tauri::command]
fn deploy_server(app: tauri::AppHandle, input: ServerInput) -> Result<ServerResult, String> {
    validate(&input)?;
    let command = format!(
        "set -eu; if [ ! -d \"$HOME/cloud-voice/.git\" ]; then git clone '{}' \"$HOME/cloud-voice\"; fi; cd \"$HOME/cloud-voice\"; git fetch origin; git checkout --detach {}; bash scripts/bootstrap.sh",
        input.repository, WORKER_RELEASE
    );
    let log = ssh(&app, &input, &command)?;
    let token = ssh(
        &app,
        &input,
        "sed -n 's/^CLOUD_VOICE_API_TOKEN=//p' \"$HOME/cloud-voice/.env\"",
    )?;
    if token.len() != 64 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("Worker deployed, but its API credential could not be read.".into());
    }
    let entry = keyring::Entry::new("cloud-voice-studio", &input.host)
        .map_err(|error| format!("Could not open Windows Credential Manager: {error}"))?;
    entry
        .set_password(&token)
        .map_err(|error| format!("Could not save the worker credential: {error}"))?;
    Ok(ServerResult {
        log,
        release: Some(WORKER_RELEASE.into()),
    })
}

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![probe_server, deploy_server])
        .run(tauri::generate_context!())
        .expect("Cloud Voice Studio failed to start");
}
