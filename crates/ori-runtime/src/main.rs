use ori_runtime::{
    Error, Result,
    service::{self, AdminCommand, ServiceConfig},
};
use std::path::Path;
#[tokio::main]
async fn main() {
    if let Err(error) = execute().await {
        eprintln!("ori-supervisor: {error}");
        std::process::exit(1);
    }
}
async fn execute() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let usage = || {
        Error::Unsafe("usage: ori-supervisor run CONFIG.json | status DATA_DIR | shutdown DATA_DIR | cutover DATA_DIR CANDIDATE.json [--acknowledge-coverage-gap]".into())
    };
    match args.as_slice() {
        [operation] if operation == "macos-agent" => ori_runtime::macos_agent::run().await,
        [operation, configuration] if operation == "run" => {
            let bytes = std::fs::read(configuration)?;
            if bytes.len() > 1024 * 1024 {
                return Err(Error::Unsafe("configuration exceeds bound".into()));
            }
            let configuration: ServiceConfig = serde_json::from_slice(&bytes)?;
            service::run(configuration).await
        }
        [operation, data] if operation == "status" || operation == "shutdown" => {
            let command = if operation == "status" {
                AdminCommand::Status
            } else {
                AdminCommand::Shutdown
            };
            let response = service::admin(Path::new(data), command).await?;
            println!("{}", serde_json::to_string(&response)?);
            if response["ok"] == true {
                Ok(())
            } else {
                Err(Error::Protocol("operation failed".into()))
            }
        }
        [operation, data, candidate, rest @ ..] if operation == "cutover" => {
            let acknowledge_coverage_gap = match rest {
                [] => false,
                [flag] if flag == "--acknowledge-coverage-gap" => true,
                _ => return Err(usage()),
            };
            let bytes = std::fs::read(candidate)?;
            if bytes.len() > 1024 * 1024 {
                return Err(Error::Unsafe(
                    "candidate specification exceeds bound".into(),
                ));
            }
            let candidate = serde_json::from_slice(&bytes)?;
            let response = service::admin(
                Path::new(data),
                AdminCommand::Cutover {
                    candidate,
                    acknowledge_coverage_gap,
                },
            )
            .await?;
            println!("{}", serde_json::to_string(&response)?);
            if response["ok"] == true {
                Ok(())
            } else {
                Err(Error::Protocol("cutover failed or rolled back".into()))
            }
        }
        _ => Err(usage()),
    }
}
