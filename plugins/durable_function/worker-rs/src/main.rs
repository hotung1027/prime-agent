//! Worker for the prime-agent delivery pipeline's mechanical activities.
//!
//! Build:  PROTOC=~/.cache/protoc/bin/protoc cargo build --release
//! Run:    TEMPORAL_ADDRESS=localhost:7233 TEMPORAL_NAMESPACE=default \
//!             ./target/release/pipeline-workers [task-queue]
//!
//! Task queue defaults to `rust-tools`; the Python kernel worker serves `prime-pipeline` and owns the
//! activities that spawn agents (they need `rlm`). Temporal routes an activity to a queue, so one
//! workflow can mix both.
mod activities;
mod shell;

use activities::PipelineActivities;
use temporalio_client::{ClientOptions, Connection, envconfig::LoadClientConfigProfileOptions};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};

const DEFAULT_TASK_QUEUE: &str = "rust-tools";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let task_queue = std::env::args()
        .nth(1)
        .unwrap_or_else(|| DEFAULT_TASK_QUEUE.to_string());

    let runtime = Runtime::from_current_tokio(Default::default())?;
    let (conn_options, client_options) =
        ClientOptions::load_from_config(LoadClientConfigProfileOptions::default())?;
    let connection = Connection::connect(conn_options).await?;
    let client = temporalio_client::Client::new(connection, client_options)?;

    let worker_options = WorkerOptions::new(task_queue.clone())
        .register_activities(PipelineActivities)
        .build();

    println!("pipeline-workers: polling task queue '{task_queue}'");
    Worker::new(&runtime, client, worker_options)?.run().await?;
    Ok(())
}
