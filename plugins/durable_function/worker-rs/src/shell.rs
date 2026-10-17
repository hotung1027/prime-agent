//! Run a child process with a hard timeout and bounded output capture.
use serde::{Deserialize, Serialize};
use std::process::Stdio;
use std::time::Instant;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

/// Keep the tail only: Temporal payloads are size-limited and the interesting part of a test run is
/// the end (summary + failures), not the first 100k characters of compile output.
const TAIL_BYTES: usize = 24_000;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandRequest {
    pub command: String,
    pub args: Vec<String>,
    pub cwd: String,
    pub timeout_s: u64,
    /// Extra environment variables, e.g. PROTOC for cargo builds.
    #[serde(default)]
    pub env: Vec<(String, String)>,
}

impl CommandRequest {
    pub fn new(cwd: &str, command: &str, args: &[&str], timeout_s: u64) -> Self {
        Self {
            command: command.to_string(),
            args: args.iter().map(|s| s.to_string()).collect(),
            cwd: cwd.to_string(),
            timeout_s,
            env: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CommandResult {
    pub command: String,
    pub cwd: String,
    pub exit_code: i32,
    pub duration_ms: u64,
    pub timed_out: bool,
    pub output_tail: String,
}

impl CommandResult {
    pub fn ok(&self) -> bool {
        self.exit_code == 0 && !self.timed_out
    }
}

pub async fn run(req: &CommandRequest) -> Result<CommandResult, String> {
    let started = Instant::now();
    let mut cmd = Command::new(&req.command);
    cmd.args(&req.args)
        .current_dir(&req.cwd)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null());
    for (k, v) in &req.env {
        cmd.env(k, v);
    }

    let mut child = cmd.spawn().map_err(|e| format!("spawn {}: {e}", req.command))?;
    let mut stdout = child.stdout.take().ok_or("no stdout")?;
    let mut stderr = child.stderr.take().ok_or("no stderr")?;

    let collect = async {
        let mut out = Vec::new();
        let mut err = Vec::new();
        let (a, b) = tokio::join!(stdout.read_to_end(&mut out), stderr.read_to_end(&mut err));
        a.map_err(|e| e.to_string())?;
        b.map_err(|e| e.to_string())?;
        Ok::<_, String>((out, err))
    };

    let (exit_code, timed_out, output) = match tokio::time::timeout(
        std::time::Duration::from_secs(req.timeout_s),
        async {
            let collected = collect.await?;
            let status = child.wait().await.map_err(|e| e.to_string())?;
            Ok::<_, String>((status.code().unwrap_or(-1), collected))
        },
    )
    .await
    {
        Ok(Ok((code, (out, err)))) => {
            let mut text = String::from_utf8_lossy(&out).into_owned();
            let err_text = String::from_utf8_lossy(&err);
            if !err_text.trim().is_empty() {
                text.push_str("\n[stderr]\n");
                text.push_str(&err_text);
            }
            (code, false, text)
        }
        Ok(Err(e)) => (-1, false, format!("io error: {e}")),
        Err(_) => {
            let _ = child.kill().await;
            (-1, true, format!("timeout after {}s", req.timeout_s))
        }
    };

    let tail = if output.len() > TAIL_BYTES {
        format!("[...truncated...]{}", &output[output.len() - TAIL_BYTES..])
    } else {
        output
    };

    Ok(CommandResult {
        command: format!("{} {}", req.command, req.args.join(" ")).trim().to_string(),
        cwd: req.cwd.clone(),
        exit_code,
        duration_ms: started.elapsed().as_millis() as u64,
        timed_out,
        output_tail: tail,
    })
}
