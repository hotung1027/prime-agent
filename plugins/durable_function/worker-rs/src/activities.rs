//! The mechanical pipeline activities. Typed contracts (serde) so the Python orchestrator and this
//! worker agree on payload shape; anything that needs `rlm` (spawning agents) deliberately lives in
//! the Python worker instead.
use crate::shell::{run, CommandRequest, CommandResult};
use serde::{Deserialize, Serialize};
use std::path::Path;
use temporalio_macros::activities;
use temporalio_sdk::activities::{ActivityContext, ActivityError};

/// `ActivityError::from` needs a real error type, so wrap plain strings.
fn act_err(msg: String) -> ActivityError {
    ActivityError::from(anyhow::anyhow!(msg))
}

pub struct PipelineActivities;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RunTestsRequest {
    pub repo: String,
    /// Julia project directory, usually the repo root.
    pub project: String,
    pub timeout_s: u64,
    /// Optional path to a specific test file; defaults to <repo>/test/runtests.jl.
    #[serde(default)]
    pub test_file: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportRequest {
    pub repo: String,
    pub project: String,
    #[serde(default)]
    pub script: Option<String>,
    /// Trajectory files to summarise once the export finishes.
    #[serde(default)]
    pub files: Vec<String>,
    pub timeout_s: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrajectorySummary {
    pub path: String,
    pub n_samples: usize,
    pub dof: usize,
    pub target: Vec<f64>,
    pub direction: Option<Vec<f64>>,
    pub ori_error: f64,
    pub t_end: f64,
    /// L2 distance between the last sample's position and the requested target.
    pub final_position_error: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportResult {
    pub export: CommandResult,
    pub trajectories: Vec<TrajectorySummary>,
}

#[derive(Debug, Deserialize, Serialize)]
struct Traj {
    target: Vec<f64>,
    direction: Option<Vec<f64>>,
    ori_error: f64,
    samples: Vec<Sample>,
}

#[derive(Debug, Deserialize, Serialize)]
struct Sample {
    t: f64,
    q: Vec<f64>,
    p: Vec<f64>,
}

/// Read one exported trajectory and summarise it. Pure Rust - the orchestrator can inspect an
/// artifact without a Julia or Python runtime.
pub fn summarise(path: &str) -> Result<TrajectorySummary, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("read {path}: {e}"))?;
    let traj: Traj = serde_json::from_str(&text).map_err(|e| format!("parse {path}: {e}"))?;
    let last = traj.samples.last().ok_or_else(|| format!("{path}: no samples"))?;
    let final_position_error = if last.p.len() == traj.target.len() {
        last.p
            .iter()
            .zip(traj.target.iter())
            .map(|(a, b)| (a - b) * (a - b))
            .sum::<f64>()
            .sqrt()
    } else {
        f64::NAN
    };
    Ok(TrajectorySummary {
        path: path.to_string(),
        n_samples: traj.samples.len(),
        dof: last.q.len(),
        target: traj.target,
        direction: traj.direction,
        ori_error: traj.ori_error,
        t_end: last.t,
        final_position_error,
    })
}

#[activities]
impl PipelineActivities {
    /// Escape hatch: run a command with a hard timeout and bounded output.
    #[activity(name = "run_command")]
    pub async fn run_command(
        _ctx: ActivityContext,
        req: CommandRequest,
    ) -> Result<CommandResult, ActivityError> {
        run(&req).await.map_err(act_err)
    }

    /// `julia --project=<project> <repo>/test/runtests.jl`
    #[activity(name = "run_tests")]
    pub async fn run_tests(
        _ctx: ActivityContext,
        req: RunTestsRequest,
    ) -> Result<CommandResult, ActivityError> {
        let test_file = req
            .test_file
            .unwrap_or_else(|| format!("{}/test/runtests.jl", req.repo));
        // `--project=<dir>` as ONE argument: with a space, Julia reads `--project` as valueless and
        // then tries to run <dir> as the script.
        let project_arg = format!("--project={}", req.project);
        let cmd = CommandRequest::new(&req.repo, "julia", &[&project_arg, &test_file], req.timeout_s);
        run(&cmd).await.map_err(act_err)
    }

    /// Run the trajectory exporter, then summarise every file it produced.
    #[activity(name = "export_trajectories")]
    pub async fn export_trajectories(
        _ctx: ActivityContext,
        req: ExportRequest,
    ) -> Result<ExportResult, ActivityError> {
        let script = req
            .script
            .unwrap_or_else(|| format!("{}/scripts/export_traj.jl", req.repo));
        let project_arg = format!("--project={}", req.project);
        let cmd = CommandRequest::new(&req.repo, "julia", &[&project_arg, &script], req.timeout_s);
        let export = run(&cmd).await.map_err(act_err)?;
        let mut trajectories = Vec::new();
        for f in &req.files {
            let path = if Path::new(f).is_absolute() {
                f.clone()
            } else {
                format!("{}/{}", req.repo, f)
            };
            // a failed summary is data, not an activity failure: report it and let the pipeline decide
            match summarise(&path) {
                Ok(s) => trajectories.push(s),
                Err(e) => return Err(act_err(e)),
            }
        }
        Ok(ExportResult { export, trajectories })
    }

    /// Summarise an already-exported trajectory.
    #[activity(name = "inspect_trajectory")]
    pub async fn inspect_trajectory(
        _ctx: ActivityContext,
        path: String,
    ) -> Result<TrajectorySummary, ActivityError> {
        summarise(&path).map_err(act_err)
    }
}
