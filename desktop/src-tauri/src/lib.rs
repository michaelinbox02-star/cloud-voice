//! Cloud Voice Studio desktop backend.
//!
//! The desktop never runs ML code. It provisions a GPU worker over SSH, then
//! reaches the worker API through an SSH tunnel so the management port is
//! never exposed publicly and the worker credential never enters the webview.

mod worker;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Mutex;
use tauri::Manager;
use worker::{Connection, Tunnel};

/// Worker revision the installer pins to. Bump alongside `WORKER_RELEASE` in
/// this file whenever a worker change is verified on real hardware.
const WORKER_RELEASE: &str = "0a4fc171cf19112d657e57e8c75d19d0fa96e8e9";

const KEYRING_SERVICE: &str = "cloud-voice-studio";

#[derive(Default)]
struct AppState {
    connection: Mutex<Option<Connection>>,
}

#[derive(Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ServerInput {
    pub host: String,
    pub port: u16,
    pub username: String,
    pub key_path: String,
    pub repository: String,
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
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
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

fn known_hosts_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let data_dir = app.path().app_data_dir().map_err(|error| error.to_string())?;
    fs::create_dir_all(&data_dir).map_err(|error| error.to_string())?;
    Ok(data_dir.join("known_hosts"))
}

pub(crate) fn ssh_command(app: &tauri::AppHandle, input: &ServerInput) -> Result<Command, String> {
    validate(input)?;
    let known_hosts = known_hosts_path(app)?;
    let mut command = Command::new("ssh");
    command
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
        .arg(input.port.to_string());
    Ok(command)
}

fn ssh_run(app: &tauri::AppHandle, input: &ServerInput, remote_command: &str) -> Result<String, String> {
    let mut command = ssh_command(app, input)?;
    let output = command
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

fn store_token(host: &str, token: &str) -> Result<(), String> {
    keyring::Entry::new(KEYRING_SERVICE, host)
        .map_err(|error| format!("Could not open the OS credential store: {error}"))?
        .set_password(token)
        .map_err(|error| format!("Could not save the worker credential: {error}"))
}

fn load_token(host: &str) -> Result<String, String> {
    keyring::Entry::new(KEYRING_SERVICE, host)
        .map_err(|error| format!("Could not open the OS credential store: {error}"))?
        .get_password()
        .map_err(|_| "No worker credential is stored for this host. Install the worker first.".to_string())
}

#[tauri::command]
fn probe_server(app: tauri::AppHandle, input: ServerInput) -> Result<ServerResult, String> {
    let command = "set -eu; . /etc/os-release; printf 'OS: %s %s\\n' \"$NAME\" \"$VERSION_ID\"; \
printf 'GPU: '; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader; \
printf 'Docker: '; docker --version; \
printf 'NVIDIA container toolkit: '; nvidia-container-cli --version | head -1; \
sudo -n true; printf 'Sudo: available\\n'; df -h / | tail -1";
    Ok(ServerResult {
        log: ssh_run(&app, &input, command)?,
        release: None,
    })
}

#[tauri::command]
fn deploy_server(app: tauri::AppHandle, input: ServerInput) -> Result<ServerResult, String> {
    validate(&input)?;
    let command = format!(
        "set -eu; if [ ! -d \"$HOME/cloud-voice/.git\" ]; then git clone --quiet '{}' \"$HOME/cloud-voice\"; fi; \
cd \"$HOME/cloud-voice\"; git fetch --quiet origin; git checkout --detach {} >/dev/null 2>&1; git rev-parse HEAD; bash scripts/bootstrap.sh",
        input.repository, WORKER_RELEASE
    );
    let log = ssh_run(&app, &input, &command)?;
    let token = ssh_run(
        &app,
        &input,
        "sed -n 's/^CLOUD_VOICE_API_TOKEN=//p' \"$HOME/cloud-voice/.env\"",
    )?;
    if token.len() != 64 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("Worker deployed, but its API credential could not be read.".into());
    }
    store_token(&input.host, &token)?;
    Ok(ServerResult {
        log,
        release: Some(WORKER_RELEASE.into()),
    })
}

#[tauri::command]
fn connect_worker(app: tauri::AppHandle, state: tauri::State<'_, AppState>, input: ServerInput) -> Result<Value, String> {
    validate(&input)?;
    let token = load_token(&input.host)?;

    // Drop any previous tunnel before opening a new one.
    if let Some(connection) = state.connection.lock().unwrap().take() {
        connection.shutdown();
    }

    let tunnel = Tunnel::open(&app, &input)?;
    let connection = Connection::new(tunnel, token)?;
    let summary = connection.request("GET", "/v1/system", None, None)?;
    *state.connection.lock().unwrap() = Some(connection);
    Ok(summary)
}

#[tauri::command]
fn disconnect_worker(state: tauri::State<'_, AppState>) -> Result<(), String> {
    if let Some(connection) = state.connection.lock().unwrap().take() {
        connection.shutdown();
    }
    Ok(())
}

fn with_connection<T>(
    state: &tauri::State<'_, AppState>,
    action: impl FnOnce(&Connection) -> Result<T, String>,
) -> Result<T, String> {
    let guard = state.connection.lock().unwrap();
    let connection = guard
        .as_ref()
        .ok_or_else(|| "Not connected to a GPU worker. Open Server and connect first.".to_string())?;
    action(connection)
}

#[tauri::command]
fn worker_system(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    with_connection(&state, |connection| connection.request("GET", "/v1/system", None, None))
}

#[tauri::command]
fn list_voices(state: tauri::State<'_, AppState>) -> Result<Value, String> {
    with_connection(&state, |connection| connection.request("GET", "/v1/voices", None, None))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct VoiceDraft {
    name: String,
    engine: String,
    description: Option<String>,
    language: Option<String>,
    settings: Option<String>,
    reference_path: Option<String>,
}

#[tauri::command]
fn create_voice(state: tauri::State<'_, AppState>, draft: VoiceDraft) -> Result<Value, String> {
    let settings = draft.settings.unwrap_or_else(|| "{}".to_string());
    let mut fields: Vec<(String, String)> = vec![
        ("name".into(), draft.name),
        ("engine".into(), draft.engine),
        ("settings".into(), settings),
    ];
    if let Some(description) = draft.description {
        fields.push(("description".into(), description));
    }
    if let Some(language) = draft.language {
        fields.push(("language".into(), language));
    }
    let reference = draft.reference_path.map(PathBuf::from);
    with_connection(&state, |connection| {
        connection.request_multipart("/v1/voices", fields, "reference", reference.as_deref(), 600)
    })
}

#[tauri::command]
fn delete_voice(state: tauri::State<'_, AppState>, voice_id: String) -> Result<Value, String> {
    with_connection(&state, |connection| {
        connection.request("DELETE", &format!("/v1/voices/{voice_id}"), None, None)
    })
}

#[tauri::command]
fn list_jobs(state: tauri::State<'_, AppState>, limit: Option<u32>) -> Result<Value, String> {
    let limit = limit.unwrap_or(25).clamp(1, 200);
    with_connection(&state, |connection| {
        connection.request("GET", &format!("/v1/jobs?limit={limit}"), None, None)
    })
}

#[tauri::command]
fn get_job(state: tauri::State<'_, AppState>, job_id: String) -> Result<Value, String> {
    with_connection(&state, |connection| {
        connection.request("GET", &format!("/v1/jobs/{job_id}"), None, None)
    })
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ConversionDraft {
    voice_id: String,
    engine: String,
    source_path: String,
    params: Option<String>,
}

#[tauri::command]
fn start_conversion(state: tauri::State<'_, AppState>, draft: ConversionDraft) -> Result<Value, String> {
    let source = PathBuf::from(&draft.source_path);
    if !source.is_file() {
        return Err("Choose a source audio file that exists.".into());
    }
    let fields = vec![
        ("voice_id".to_string(), draft.voice_id),
        ("engine".to_string(), draft.engine),
        ("params".to_string(), draft.params.unwrap_or_else(|| "{}".to_string())),
    ];
    with_connection(&state, |connection| {
        connection.request_multipart("/v1/conversions", fields, "source", Some(&source), 3600)
    })
}

/// One download command for every worker artifact: job audio or reference clip.
#[tauri::command]
fn worker_download(
    state: tauri::State<'_, AppState>,
    path: String,
    destination_path: String,
) -> Result<String, String> {
    if !path.starts_with("/v1/") {
        return Err("Downloads are limited to worker API paths.".into());
    }
    let destination = PathBuf::from(&destination_path);
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    with_connection(&state, |connection| {
        connection.download(&path, &destination)
    })?;
    Ok(destination.to_string_lossy().into_owned())
}

/// Fetch an artifact into the app cache for in-app playback. `file_name` only
/// supplies a sanitised label; callers cannot write outside the cache folder.
#[tauri::command]
fn worker_fetch_artifact(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    path: String,
    file_name: String,
) -> Result<String, String> {
    if !path.starts_with("/v1/") {
        return Err("Downloads are limited to worker API paths.".into());
    }
    let safe_name: String = file_name
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '-' | '_'))
        .collect();
    if safe_name.is_empty() || safe_name.starts_with('.') {
        return Err("Choose a plain file name for the downloaded artifact.".into());
    }
    let directory = app
        .path()
        .app_cache_dir()
        .map_err(|error| error.to_string())?
        .join("artifacts");
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let destination = directory.join(safe_name);
    with_connection(&state, |connection| connection.download(&path, &destination))?;
    Ok(destination.to_string_lossy().into_owned())
}

#[tauri::command]
fn reveal_path(path: String) -> Result<(), String> {
    if !Path::new(&path).exists() {
        return Err("That file is no longer on disk.".into());
    }
    #[cfg(windows)]
    {
        let mut command = Command::new("explorer");
        command.arg(format!("/select,{path}"));
        let _ = command.spawn();
    }
    Ok(())
}

/// Copy a user-selected file into the app cache so the UI can render a
/// waveform and play it back through the asset protocol. The original path is
/// what gets uploaded, so large media is never duplicated on the worker.
#[tauri::command]
fn stage_preview(app: tauri::AppHandle, source_path: String) -> Result<String, String> {
    let source = PathBuf::from(&source_path);
    if !source.is_file() {
        return Err("That file no longer exists.".into());
    }
    let directory = app
        .path()
        .app_cache_dir()
        .map_err(|error| error.to_string())?
        .join("preview");
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let name = source
        .file_name()
        .ok_or_else(|| "That path has no file name.".to_string())?;
    let destination = directory.join(name);
    fs::copy(&source, &destination).map_err(|error| error.to_string())?;
    Ok(destination.to_string_lossy().into_owned())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            probe_server,
            deploy_server,
            connect_worker,
            disconnect_worker,
            worker_system,
            list_voices,
            create_voice,
            delete_voice,
            list_jobs,
            get_job,
            start_conversion,
            worker_download,
            worker_fetch_artifact,
            reveal_path,
            stage_preview,
        ])
        .run(tauri::generate_context!())
        .expect("Cloud Voice Studio failed to start");
}
