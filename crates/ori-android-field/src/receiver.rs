use crate::plan::Range;
use ori_instruments::{
    ArDv1, IoLimits, ReceiverConfig, ReceiverMode, ReceiverProfile, ReceiverSample, Result,
};
use serde_json::json;
use std::time::Duration;
use tokio::net::UnixStream;

pub(super) enum ReceiverRun {
    Single(u64),
    List(Vec<u64>),
    Range(Range),
}

pub(super) struct Receiver {
    pub adapter: ArDv1<UnixStream>,
    run: ReceiverRun,
    mode: ReceiverMode,
    continuous: bool,
    total: u64,
    index: u64,
    pass: u64,
    chunk_number: u64,
    chunk_end: u64,
    identified: bool,
}

impl Receiver {
    pub fn new(
        io: UnixStream,
        mode: ReceiverMode,
        settle_ms: u64,
        continuous: bool,
        run: ReceiverRun,
    ) -> Result<Self> {
        let (frequency_hz, total) = match &run {
            ReceiverRun::Single(hz) => (*hz, 1),
            ReceiverRun::List(list) => (list[0], list.len() as u64),
            ReceiverRun::Range(range) => (range.start, range.total),
        };
        let config = ReceiverConfig {
            frequency_hz,
            mode,
            profile: ReceiverProfile::MonitorV1,
        };
        let base = ArDv1::new(io, config, IoLimits::default())?;
        let (adapter, chunk_end) = if let ReceiverRun::Range(range) = &run {
            let chunk = range.chunk(0)?;
            (base.with_scope(chunk)?, chunk.points() as u64)
        } else {
            (
                base.with_readback_settle(Duration::from_millis(settle_ms))?,
                0,
            )
        };
        Ok(Self {
            adapter,
            run,
            mode,
            continuous,
            total,
            index: 0,
            pass: 1,
            chunk_number: 0,
            chunk_end,
            identified: false,
        })
    }

    pub async fn next(&mut self) -> Result<Option<ReceiverSample>> {
        if self.index == self.total {
            if !self.continuous {
                return Ok(None);
            }
            self.index = 0;
            self.pass += 1;
            self.chunk_end = 0;
            self.chunk_number = 0;
        }
        if !self.identified {
            self.adapter.identify_monitor().await?;
            self.identified = true;
        }

        match &self.run {
            ReceiverRun::Single(_) => {} // Adapter tunes once and checks every RX readback.
            ReceiverRun::List(list) => {
                self.adapter
                    .tune(list[self.index as usize], self.mode)
                    .await?
            }
            ReceiverRun::Range(range) if self.index == self.chunk_end => {
                let next = range.chunk(self.index)?;
                self.adapter.continue_scope(next).await?;
                self.chunk_end += next.points() as u64;
                if self.index != 0 {
                    self.chunk_number += 1;
                }
            }
            ReceiverRun::Range(_) => {}
        }
        let mut sample = self.adapter.poll_receiver().await?;
        sample.metadata["scan_pass"] = json!(self.pass);
        sample.metadata["scan_index"] = json!(self.index);
        sample.metadata["scan_total"] = json!(self.total);
        if matches!(self.run, ReceiverRun::Range(_)) {
            sample.metadata["scan_chunk_index"] = json!(self.chunk_number);
            sample.metadata["scan_chunk_total"] = json!(self.total.div_ceil(1000));
        }
        self.index += 1;
        Ok(Some(sample))
    }
}
