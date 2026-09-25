//! SSH tunnel and worker API client.
//!
//! The management API binds to loopback on the GPU host, so the desktop
//! reaches it through an SSH local port forward. Nothing is exposed publicly
//! and no extra firewall rule is required.

use crate::ServerInput;
use serde_json::Value;
use std::fs::File;
use std::io::Read;
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const REMOTE_API_PORT: u16 = 8765;
const REMOTE_SIGNALING_PORT: u16 = 8791;

pub struct Tunnel {
    child: Mutex<Child>,
    log_path: PathBuf,
    pub api_port: u16,
    pub signaling_port: u16,
}

impl Tunnel {
    /// `data_dir` holds the tunnel's known-hosts and log file. Passing it in
    /// rather than the app handle lets the connection be rebuilt from a worker
    /// thread, which is what auto-reconnect needs.
    pub fn open(input: &ServerInput, data_dir: &Path) -> Result<Self, String> {
        let api_port = free_port()?;
        let signaling_port = free_port()?;
        std::fs::create_dir_all(data_dir).map_err(|error| error.to_string())?;
        let log_path = data_dir.join(format!("tunnel-{api_port}.log"));
        let log = File::create(&log_path).map_err(|error| error.to_string())?;
        let log_copy = log.try_clone().map_err(|error| error.to_string())?;

        let mut command = crate::ssh_command_for(input, data_dir)?;
        command
            .arg("-N")
            .arg("-o")
            .arg("ExitOnForwardFailure=yes")
            .arg("-o")
            .arg("ServerAliveInterval=20")
            .arg("-L")
            .arg(format!("127.0.0.1:{api_port}:127.0.0.1:{REMOTE_API_PORT}"))
            // Realtime signalling is a second loopback service on the worker.
            .arg("-L")
            .arg(format!(
                "127.0.0.1:{signaling_port}:127.0.0.1:{REMOTE_SIGNALING_PORT}"
            ))
            .arg(format!("{}@{}", input.username, input.host))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(log));
        hide_console_window(&mut command);

        let child = command
            .spawn()
            .map_err(|error| format!("Could not start the SSH tunnel: {error}"))?;

        let mut tunnel = Tunnel {
            child: Mutex::new(child),
            log_path,
            api_port,
            signaling_port,
        };
        tunnel.wait_until_ready(&log_copy)?;
        Ok(tunnel)
    }

    fn wait_until_ready(&mut self, _log: &File) -> Result<(), String> {
        let deadline = Instant::now() + Duration::from_secs(25);
        while Instant::now() < deadline {
            if !self.is_alive() {
                return Err(format!(
                    "The SSH tunnel exited immediately. {}",
                    self.diagnostics()
                ));
            }
            if TcpStream::connect_timeout(
                &format!("127.0.0.1:{}", self.api_port).parse().unwrap(),
                Duration::from_millis(300),
            )
            .is_ok()
            {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        Err(format!(
            "Timed out opening the SSH tunnel to the worker. {}",
            self.diagnostics()
        ))
    }

    /// True while the ssh process is still running. A dead tunnel is the usual
    /// cause of "unexpected disconnect", and it can simply be reopened.
    pub fn is_alive(&self) -> bool {
        matches!(self.child.lock().unwrap().try_wait(), Ok(None))
    }

    fn diagnostics(&self) -> String {
        let mut text = String::new();
        if let Ok(mut file) = File::open(&self.log_path) {
            let _ = file.read_to_string(&mut text);
        }
        let text = text.trim();
        if text.is_empty() {
            "Check that the host is reachable and the SSH key is still authorized.".to_string()
        } else {
            format!("SSH reported: {text}")
        }
    }

    pub fn shutdown(self) {
        let mut child = self.child.into_inner().unwrap_or_else(|error| error.into_inner());
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|error| error.to_string())?;
    let port = listener.local_addr().map_err(|error| error.to_string())?.port();
    drop(listener);
    Ok(port)
}

#[cfg(windows)]
fn hide_console_window(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console_window(_: &mut Command) {}

pub struct Connection {
    base_url: String,
    signaling_url: String,
    signaling_port: u16,
    token: String,
    client: reqwest::blocking::Client,
    tunnel: Tunnel,
}

impl Connection {
    pub fn new(tunnel: Tunnel, token: String) -> Result<Self, String> {
        let client = reqwest::blocking::Client::builder()
            // The tunnel is loopback-only; a system proxy must not intercept it.
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(|error| error.to_string())?;
        let connection = Connection {
            base_url: format!("http://127.0.0.1:{}", tunnel.api_port),
            signaling_url: format!("http://127.0.0.1:{}", tunnel.signaling_port),
            signaling_port: tunnel.signaling_port,
            token,
            client,
            tunnel,
        };
        connection.request("GET", "/v1/health", None, Some(20))?;
        Ok(connection)
    }

    pub fn shutdown(self) {
        self.tunnel.shutdown();
    }

    pub fn is_alive(&self) -> bool {
        self.tunnel.is_alive()
    }

    /// Loopback port the desktop uses for realtime signalling and tunnelled audio.
    pub fn signaling_port(&self) -> u16 {
        self.signaling_port
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// Realtime signalling carries its own short-lived ticket rather than the
    /// worker credential, so no bearer token is attached to these calls.
    pub fn signaling_request(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
        timeout_seconds: Option<u64>,
    ) -> Result<Value, String> {
        let request = self.client.request(
            method.parse().map_err(|_| format!("Unsupported method {method}"))?,
            format!("{}{}", self.signaling_url, path),
        );
        let request = match body {
            Some(value) => request.json(&value),
            None => request,
        };
        let request = match timeout_seconds {
            Some(seconds) => request.timeout(Duration::from_secs(seconds)),
            None => request,
        };
        let response = request
            .send()
            .map_err(|error| format!("Realtime request failed: {error}"))?;
        decode(response)
    }

    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
        timeout_seconds: Option<u64>,
    ) -> Result<Value, String> {
        let request = self
            .client
            .request(method.parse().map_err(|_| format!("Unsupported method {method}"))?, self.url(path))
            .bearer_auth(&self.token);
        let request = match body {
            Some(value) => request.json(&value),
            None => request,
        };
        let request = match timeout_seconds {
            Some(seconds) => request.timeout(Duration::from_secs(seconds)),
            None => request,
        };
        let response = request
            .send()
            .map_err(|error| format!("Worker request failed: {error}"))?;
        decode(response)
    }

    pub fn request_multipart(
        &self,
        path: &str,
        fields: Vec<(String, String)>,
        files: Vec<(String, PathBuf)>,
        timeout_seconds: u64,
    ) -> Result<Value, String> {
        let mut form = reqwest::blocking::multipart::Form::new();
        for (name, value) in fields {
            form = form.text(name, value);
        }
        for (field, path) in files {
            form = form
                .file(field, &path)
                .map_err(|error| format!("Could not read {}: {error}", path.display()))?;
        }
        let response = self
            .client
            .post(self.url(path))
            .bearer_auth(&self.token)
            .multipart(form)
            .timeout(Duration::from_secs(timeout_seconds))
            .send()
            .map_err(|error| format!("Worker upload failed: {error}"))?;
        decode(response)
    }

    pub fn download(&self, path: &str, destination: &Path) -> Result<(), String> {
        let mut response = self
            .client
            .get(self.url(path))
            .bearer_auth(&self.token)
            .send()
            .map_err(|error| format!("Download failed: {error}"))?;
        if !response.status().is_success() {
            return Err(describe_error(response));
        }
        let mut file = File::create(destination).map_err(|error| error.to_string())?;
        std::io::copy(&mut response, &mut file).map_err(|error| error.to_string())?;
        Ok(())
    }
}

fn decode(response: reqwest::blocking::Response) -> Result<Value, String> {
    if !response.status().is_success() {
        return Err(describe_error(response));
    }
    if response.status() == reqwest::StatusCode::NO_CONTENT {
        return Ok(Value::Null);
    }
    response
        .json::<Value>()
        .map_err(|error| format!("Worker returned an unexpected response: {error}"))
}

fn describe_error(response: reqwest::blocking::Response) -> String {
    let status = response.status();
    let body = response.text().unwrap_or_default();
    let detail = serde_json::from_str::<Value>(&body)
        .ok()
        .and_then(|value| value.get("detail").map(|detail| detail.to_string()))
        .unwrap_or_else(|| body.trim().trim_matches('"').to_string());
    if detail.is_empty() {
        format!("Worker returned HTTP {status}")
    } else {
        format!("{detail} (HTTP {status})")
    }
}
