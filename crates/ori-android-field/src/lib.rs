//! Android owns the USB connection. A connected local socket carries its serial
//! bytes to one bounded, receive-only instrument adapter per capture worker.
#![cfg(unix)]
mod bridge;
mod plan;
mod receiver;
use ori_instruments::{IoLimits, ReceiveConfig, Result, RfExplorer, TinySa};
use plan::{Plan, bounds, invalid};
use receiver::{Receiver, ReceiverRun};
use serde_json::{Value, json};
use tokio::net::UnixStream;

enum Capture {
    Explorer(RfExplorer<UnixStream>),
    Tiny(Box<TinySa<UnixStream>>),
    Receiver(Box<Receiver>),
}

impl Capture {
    fn new(io: UnixStream, plan: Plan) -> Result<Self> {
        let limits = IoLimits::default();
        let receiver = match plan {
            Plan::RfExplorerSweep { start_hz, stop_hz } => {
                return Ok(Self::Explorer(RfExplorer::new(
                    io,
                    bounds(start_hz, stop_hz)?,
                    limits,
                )?));
            }
            Plan::RfExplorerSurvey {
                start_hz,
                stop_hz,
                step_hz,
            } => {
                if step_hz.is_some_and(|step| step != 10_000) {
                    return Err(invalid("RF Explorer survey requires 10 kHz spacing"));
                }
                return Ok(Self::Explorer(RfExplorer::new_survey(
                    io,
                    bounds(start_hz, stop_hz)?,
                    limits,
                )?));
            }
            Plan::TinysaRx {
                start_hz,
                stop_hz,
                points,
                rbw_hz,
            } => {
                return Ok(Self::Tiny(Box::new(TinySa::new(
                    io,
                    ReceiveConfig {
                        start_hz,
                        stop_hz,
                        points,
                        rbw_hz,
                    },
                    limits,
                )?)));
            }
            Plan::Ardv1Single {
                frequency_hz,
                mode,
                settle_ms,
                continuous,
            } => Receiver::new(
                io,
                mode,
                settle_ms,
                continuous,
                ReceiverRun::Single(frequency_hz),
            )?,
            Plan::Ardv1List {
                frequencies_hz,
                mode,
                settle_ms,
                continuous,
            } => {
                plan::validate_list(&frequencies_hz)?;
                Receiver::new(
                    io,
                    mode,
                    settle_ms,
                    continuous,
                    ReceiverRun::List(frequencies_hz),
                )?
            }
            Plan::Ardv1Range {
                start_hz,
                stop_hz,
                step_hz,
                settle_ms,
                mode,
                continuous,
            } => {
                let range = plan::Range::new(start_hz, stop_hz, step_hz, settle_ms)?;
                Receiver::new(io, mode, settle_ms, continuous, ReceiverRun::Range(range))?
            }
        };
        Ok(Self::Receiver(Box::new(receiver)))
    }

    async fn next(&mut self) -> Result<Option<Value>> {
        match self {
            Self::Explorer(adapter) => {
                adapter.prepare().await?;
                Ok(Some(json!(adapter.poll().await?)))
            }
            Self::Tiny(adapter) => Ok(Some(json!(adapter.poll_spectrum().await?))),
            Self::Receiver(receiver) => Ok(receiver.next().await?.map(|sample| json!(sample))),
        }
    }

    async fn close(&mut self) -> Result<()> {
        match self {
            Self::Explorer(adapter) => adapter.close().await,
            Self::Tiny(adapter) => adapter.close().await,
            Self::Receiver(receiver) => receiver.adapter.close().await,
        }
    }
}
