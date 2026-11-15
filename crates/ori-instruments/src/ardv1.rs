use crate::{tinysa::now, transport::Connection, *};
use serde_json::json;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::time::Instant;

const RX_SETTLE: Duration = Duration::from_millis(25);

#[derive(Clone, Copy, Debug)]
pub struct ReceiverScope {
    start_hz: u64,
    stop_hz: u64,
    step_hz: u64,
    settle: Duration,
    points: usize,
    vfo_step: Option<&'static str>,
}
impl ReceiverScope {
    pub fn new(
        start_hz: u64,
        stop_hz: u64,
        step_hz: u64,
        settle: Duration,
        auto: bool,
    ) -> Result<Self> {
        frequency_command(start_hz)?;
        frequency_command(stop_hz)?;
        if stop_hz <= start_hz
            || !(10..=1_300_000_000).contains(&step_hz)
            || !step_hz.is_multiple_of(10)
            || settle > Duration::from_secs(1)
        {
            return Err(InstrumentError::InvalidRequest(
                "invalid AR-DV1 scope range/settling".into(),
            ));
        }
        let points = (stop_hz - start_hz).div_ceil(step_hz) + 1;
        if points > 1000 {
            return Err(InstrumentError::InvalidRequest(
                "AR-DV1 scope is limited to 1000 points".into(),
            ));
        }
        let vfo_step = if auto
            && stop_hz - start_hz >= step_hz
            && start_hz.is_multiple_of(step_hz)
            && settle >= Duration::from_millis(15)
        {
            match step_hz {
                25_000 => Some("025.00"),
                100_000 => Some("100.00"),
                500_000 => Some("500.00"),
                _ => None,
            }
        } else {
            None
        };
        Ok(Self {
            start_hz,
            stop_hz,
            step_hz,
            settle,
            points: points as usize,
            vfo_step,
        })
    }
    pub fn points(self) -> usize {
        self.points
    }
}

struct ScopeRun {
    config: ReceiverScope,
    index: usize,
    saved_raster: Option<(String, String)>,
}

/// Documented receive control only. AUTO excludes TETRA; levels are raw counts.
pub struct ArDv1<T> {
    connection: Connection<T>,
    requested: ReceiverConfig,
    ready: bool,
    frequency: Option<u64>,
    mode: Option<ReceiverMode>,
    source: &'static str,
    firmware: Option<String>,
    sequence: u64,
    tune_sequence: u64,
    verify_frequency: bool,
    settled_at: Option<Instant>,
    scope: Option<ScopeRun>,
    verified_dv1: bool,
}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> ArDv1<T> {
    pub fn new(transport: T, config: ReceiverConfig, limits: IoLimits) -> Result<Self> {
        frequency_command(config.frequency_hz)?;
        validate_mode(config.profile, config.mode)?;
        Ok(Self {
            connection: Connection::new(transport, limits)?,
            requested: config,
            ready: false,
            frequency: None,
            mode: None,
            source: "unconfirmed",
            firmware: None,
            sequence: 0,
            tune_sequence: 0,
            verify_frequency: false,
            settled_at: None,
            scope: None,
            verified_dv1: false,
        })
    }
    pub fn with_frequency_readback(mut self) -> Result<Self> {
        if self.requested.profile != ReceiverProfile::MonitorV1 {
            return Err(InstrumentError::UnsupportedProfile(
                "receiver RX readback requires ardv1-monitor-v1".into(),
            ));
        }
        self.verify_frequency = true;
        Ok(self)
    }
    pub fn with_scope(mut self, config: ReceiverScope) -> Result<Self> {
        if self.requested.mode != ReceiverMode::Fm {
            return Err(InstrumentError::UnsupportedProfile(
                "AR-DV1 scope requires FM".into(),
            ));
        }
        self = self.with_frequency_readback()?;
        self.requested.frequency_hz = config.start_hz;
        self.scope = Some(ScopeRun {
            config,
            index: 0,
            saved_raster: None,
        });
        Ok(self)
    }
    pub fn cancellation_handle(&self) -> CancellationHandle {
        self.connection.cancellation_handle()
    }
    pub(crate) fn execution_scope(
        &self,
        check: ExecutionCheck,
    ) -> Result<crate::transport::ExecutionScope> {
        self.connection.execution_scope(check)
    }
    pub fn state(&self) -> ReceiverState {
        let unknown = self.connection.tainted() || self.connection.closed();
        ReceiverState {
            frequency_hz: if unknown { None } else { self.frequency },
            mode: if unknown { None } else { self.mode },
            requested_frequency_hz: self.requested.frequency_hz,
            requested_mode: self.requested.mode,
            restart_required: self.connection.tainted(),
            closed: self.connection.closed(),
            frequency_source: if unknown { "unknown" } else { self.source }.into(),
            frequency_readback: !unknown && self.source == "receiver_RX",
            firmware: self.firmware.clone(),
            hardware_validation: "not_run".into(),
        }
    }
    pub fn invalidate_device_knowledge(&mut self) {
        self.frequency = None;
        self.mode = None;
        self.source = "unknown";
        self.settled_at = None;
    }
    fn failed<E>(&mut self, error: InstrumentError) -> Result<E> {
        self.connection.taint();
        self.invalidate_device_knowledge();
        Err(error)
    }
    async fn transact(&mut self, command: &str) -> Result<String> {
        let text = self.connection.line(command).await?;
        match body(&text) {
            Ok(value) if self.scope.is_some() && (!text.starts_with("20") || value.is_empty()) => {
                self.failed(InstrumentError::Protocol(
                    "AR-DV1 scope requires a terminal query reply".into(),
                ))
            }
            Ok(value) => Ok(value.to_owned()),
            Err(error) => self.failed(error),
        }
    }
    async fn acknowledge(&mut self, command: &str) -> Result<()> {
        let raw = self.connection.line(command).await?;
        if let Err(error) = body(&raw) {
            return self.failed(error);
        }
        if raw != "20" && (self.scope.is_some() || raw != "10") {
            return self.failed(InstrumentError::Rejected(format!("{command}: {raw}")));
        }
        Ok(())
    }
    pub async fn prepare(&mut self) -> Result<()> {
        self.connection.ensure()?;
        if self.ready {
            return Ok(());
        }
        let result = async {
            self.acknowledge("RE1").await?;
            let firmware = self.transact("VR").await?;
            let firmware = firmware
                .strip_prefix("VR")
                .filter(|s| !s.is_empty())
                .ok_or_else(|| {
                    InstrumentError::Protocol("missing AR-DV1 firmware response".into())
                })?;
            self.firmware = Some(firmware.to_owned());
            if self.requested.profile == ReceiverProfile::MonitorV1 && firmware != "2504A" {
                return Err(InstrumentError::UnsupportedProfile(
                    "ardv1-monitor-v1 requires VR2504A".into(),
                ));
            }
            let serial = self.transact("RN").await?;
            if !serial.starts_with("RN") || serial.len() <= 2 {
                return Err(InstrumentError::Protocol(
                    "missing AR-DV1 serial response".into(),
                ));
            }
            self.verified_dv1 = serial.len() == 10
                && serial.starts_with("RN0952")
                && serial[2..]
                    .parse::<u32>()
                    .is_ok_and(|number| number < 9_523_001);
            self.ready = true;
            Ok(())
        }
        .await;
        if let Err(error) = result {
            return self.failed(error);
        }
        Ok(())
    }
    pub async fn tune(&mut self, frequency_hz: u64, mode: ReceiverMode) -> Result<()> {
        self.connection.ensure()?;
        if self.scope.is_some() {
            return Err(InstrumentError::UnsafeState(
                "end AR-DV1 scope before manual tuning".into(),
            ));
        }
        let command = frequency_command(frequency_hz)?;
        validate_mode(self.requested.profile, mode)?;
        self.requested.frequency_hz = frequency_hz;
        self.requested.mode = mode;
        self.invalidate_device_knowledge();
        let result = async {
            self.prepare().await?;
            self.acknowledge("VFA").await?;
            self.acknowledge(mode_command(mode)).await?;
            self.acknowledge(&command).await?;
            Ok(())
        }
        .await;
        if let Err(error) = result {
            return self.failed(error);
        }
        self.frequency = Some(frequency_hz);
        self.mode = Some(mode);
        self.source = "command_acknowledged";
        self.tune_sequence += 1;
        if self.verify_frequency {
            self.settled_at = Some(Instant::now() + RX_SETTLE);
        }
        Ok(())
    }
    pub async fn poll_receiver(&mut self) -> Result<ReceiverSample> {
        self.connection.ensure()?;
        let scan_command = if self.scope.is_some() {
            match self.tune_scope_point().await {
                Ok(command) => Some(command),
                Err(error) => return self.failed(error),
            }
        } else {
            if self.source == "unconfirmed" {
                self.tune(self.requested.frequency_hz, self.requested.mode)
                    .await?;
            }
            None
        };
        let (Some(frequency_hz), Some(mode)) = (self.frequency, self.mode) else {
            return Err(InstrumentError::UnsafeState(
                "receiver frequency/mode unknown; explicit tune required".into(),
            ));
        };
        if let Some(settled_at) = self.settled_at {
            tokio::time::sleep_until(settled_at).await;
            self.settled_at = None;
        }
        let started_at = now();
        let result = self.read_receiver(frequency_hz, mode).await;
        let (level, squelch, status, raw_rx) = match result {
            Ok(value) => value,
            Err(error) => return self.failed(error),
        };
        let verified = raw_rx.is_some();
        if verified {
            self.source = "receiver_RX";
        }
        self.sequence += 1;
        let decoded_mode = status
            .as_ref()
            .and_then(|status| match status.as_bytes()[2] {
                b'1' => Some("DSTAR"),
                b'2' => Some("YSF"),
                b'3' => Some("ALINCO"),
                b'4' => Some("DCR/NXDN"),
                b'5' => Some("P25"),
                b'6' => Some("DPMR"),
                b'7' => Some("DMR"),
                _ => None,
            })
            .map(str::to_owned);
        let decoder_source = if verified {
            "receiver_RX"
        } else if status.is_some() {
            "receiver_MD"
        } else {
            "unavailable"
        };
        let mut metadata = json!({"receiver_profile":self.requested.profile,"receiver_firmware":self.firmware,
            "raw_mode_status":status,"decoder_source":decoder_source,"raw_squelch_status":squelch,
            "adapter":"ardv1-serial","sequence":self.sequence,"tune_sequence":self.tune_sequence,"frequency_source":self.source,
            "mode_source":self.source,"frequency_readback":verified,"simulated":false,"hardware_validation":"not_run"});
        if let Some(raw) = raw_rx {
            metadata["raw_receiver_status"] = json!(raw);
        }
        if let Some((scope, command)) = self.scope.as_mut().zip(scan_command) {
            metadata["scan_index"] = json!(scope.index);
            metadata["scan_total"] = json!(scope.config.points);
            metadata["scan_tune_command"] = json!(command);
            metadata["scope_source"] = json!("sequential_receiver_readback");
            metadata["settle_s"] = json!(scope.config.settle.as_secs_f64());
            scope.index += 1;
        }
        Ok(ReceiverSample {
            frequency_hz,
            mode,
            level,
            level_unit: "s_meter_count".into(),
            squelch_open: squelch != 0,
            raw_squelch_status: squelch,
            decoded_mode,
            raw_mode_status: status,
            interval: OperationInterval {
                started_at,
                ended_at: now(),
            },
            metadata,
        })
    }
    async fn tune_scope_point(&mut self) -> Result<&'static str> {
        let scope = self.scope.as_ref().expect("scope acquisition");
        let (config, index) = (scope.config, scope.index);
        if index >= config.points {
            return Err(InstrumentError::UnsafeState(
                "AR-DV1 scope has finished".into(),
            ));
        }
        let frequency = (config.start_hz + index as u64 * config.step_hz).min(config.stop_hz);
        let previous = self.frequency;
        self.invalidate_device_knowledge();
        let mut command = "RF";
        if index == 0 {
            self.prepare().await?;
            self.acknowledge("VFA").await?;
            if let Some(step) = config.vfo_step.filter(|_| self.verified_dv1) {
                let saved_step = self.transact("ST").await?;
                let saved_shift = self.transact("SH").await?;
                if !valid_rx_step(&saved_step) || !valid_raster(&saved_shift, "SH") {
                    return Err(InstrumentError::Protocol(
                        "invalid AR-DV1 VFO raster".into(),
                    ));
                }
                self.scope.as_mut().unwrap().saved_raster = Some((saved_step, saved_shift));
                self.acknowledge(&format!(
                    "VFA {} ST{step} SH000.00 MD0F0",
                    frequency_command(frequency)?
                ))
                .await?;
                if self.transact("SH").await? != "SH000.00" {
                    return Err(InstrumentError::Protocol(
                        "AR-DV1 did not confirm zero VFO shift".into(),
                    ));
                }
            } else {
                self.acknowledge("MD0F0").await?;
                self.acknowledge(&frequency_command(frequency)?).await?;
            }
        } else if self.scope.as_ref().unwrap().saved_raster.is_some()
            && previous.is_some_and(|value| value + config.step_hz == frequency)
        {
            self.acknowledge("ZK").await?;
            command = "ZK";
        } else {
            self.acknowledge(&frequency_command(frequency)?).await?;
        }
        self.frequency = Some(frequency);
        self.requested.frequency_hz = frequency;
        self.mode = Some(ReceiverMode::Fm);
        self.source = "command_acknowledged";
        self.tune_sequence += 1;
        self.settled_at = Some(Instant::now() + config.settle);
        Ok(command)
    }
    async fn read_receiver(
        &mut self,
        frequency_hz: u64,
        mode: ReceiverMode,
    ) -> Result<(u32, u8, Option<String>, Option<String>)> {
        if self.verify_frequency {
            let raw = self.transact("RX").await?;
            let parts = raw.split(' ').collect::<Vec<_>>();
            let ["RX", "VFA", frequency, step, status, level] = parts[..] else {
                return Err(InstrumentError::Protocol("invalid RX VFO-A status".into()));
            };
            if frequency != frequency_command(frequency_hz)? || !valid_rx_step(step) {
                return Err(InstrumentError::Protocol(
                    "RX did not confirm the commanded receiver frequency and VFO".into(),
                ));
            }
            if let Some(scope) = &self.scope
                && scope.saved_raster.is_some()
                && step.strip_prefix("ST") != scope.config.vfo_step
            {
                return Err(InstrumentError::Protocol(
                    "AR-DV1 scope raster changed".into(),
                ));
            }
            verify_mode(status, mode)?;
            let (level, squelch) = parse_level(level)?;
            return Ok((level, squelch, Some(status.into()), Some(raw)));
        }
        let (level, squelch) = parse_level(&self.transact("LM").await?)?;
        let status = if self.requested.profile == ReceiverProfile::MonitorV1 {
            let status = self.transact("MD").await?;
            verify_mode(&status, mode)?;
            Some(status)
        } else {
            None
        };
        Ok((level, squelch, status, None))
    }
    pub async fn close(&mut self) -> Result<()> {
        if self.connection.closed() {
            return Ok(());
        }
        let result = if self.ready && !self.connection.tainted() {
            async {
                if let Some((step, shift)) = self
                    .scope
                    .as_mut()
                    .and_then(|scope| scope.saved_raster.take())
                {
                    self.acknowledge(&format!("VFA {step} {shift}")).await?;
                    if self.transact("ST").await? != step || self.transact("SH").await? != shift {
                        return Err(InstrumentError::Protocol(
                            "AR-DV1 did not confirm VFO raster restoration".into(),
                        ));
                    }
                }
                self.acknowledge("EX").await
            }
            .await
        } else {
            Ok(())
        };
        self.invalidate_device_knowledge();
        let closed = self.connection.close().await;
        result.and(closed)
    }
}
fn valid_rx_step(text: &str) -> bool {
    valid_raster(text, "ST")
}
fn valid_raster(text: &str, prefix: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 8
        && &bytes[..2] == prefix.as_bytes()
        && bytes[5] == b'.'
        && bytes[2..5]
            .iter()
            .chain(&bytes[6..])
            .all(u8::is_ascii_digit)
}

fn verify_mode(status: &str, mode: ReceiverMode) -> Result<()> {
    let bytes = status.as_bytes();
    if bytes.len() != 5
        || &bytes[..2] != b"MD"
        || !matches!(bytes[2], b'0'..=b'7')
        || !matches!(bytes[3], b'0'..=b'7' | b'F')
        || !matches!(bytes[4], b'0'..=b'6')
    {
        return Err(InstrumentError::Protocol("invalid MD status".into()));
    }
    if mode_command(mode)[3..] != status[3..] {
        return Err(InstrumentError::Protocol(
            "receiver selected mode changed outside core".into(),
        ));
    }
    Ok(())
}
fn validate_mode(profile: ReceiverProfile, mode: ReceiverMode) -> Result<()> {
    if profile == ReceiverProfile::LegacyFmAm
        && !matches!(mode, ReceiverMode::Fm | ReceiverMode::Am)
    {
        return Err(InstrumentError::InvalidRequest(
            "legacy-fm-am supports only FM/AM".into(),
        ));
    }
    Ok(())
}
fn mode_command(mode: ReceiverMode) -> &'static str {
    match mode {
        ReceiverMode::Auto => "MD000",
        ReceiverMode::Dstar => "MD010",
        ReceiverMode::Ysf => "MD020",
        ReceiverMode::Alinco => "MD030",
        ReceiverMode::Nxdn => "MD040",
        ReceiverMode::P25 => "MD050",
        ReceiverMode::Dpmr => "MD060",
        ReceiverMode::Dmr => "MD070",
        ReceiverMode::Fm => "MD0F0",
        ReceiverMode::Am => "MD0F1",
    }
}
fn frequency_command(frequency_hz: u64) -> Result<String> {
    if !(100_000..=1_300_000_000).contains(&frequency_hz) || !frequency_hz.is_multiple_of(10) {
        return Err(InstrumentError::InvalidRequest(
            "AR-DV1 requires 0.1..1300 MHz and integer 10 Hz steps".into(),
        ));
    }
    let units = frequency_hz / 10;
    Ok(format!("RF{:04}.{:05}", units / 100_000, units % 100_000))
}
fn body(text: &str) -> Result<&str> {
    if !text.is_ascii() {
        return Err(InstrumentError::Protocol(
            "non-ASCII AR-DV1 response".into(),
        ));
    }
    let bytes = text.as_bytes();
    if bytes.len() >= 2 && bytes[0].is_ascii_digit() && matches!(bytes[1], b'0' | b'1') {
        let kind = bytes[0] - b'0';
        let rest = &text[2..];
        let plausible = rest.is_empty()
            || rest.starts_with(['?', ' '])
            || rest.as_bytes()[0].is_ascii_alphabetic();
        if (1..=6).contains(&kind) && plausible {
            if kind >= 3 {
                return Err(InstrumentError::Rejected(text.into()));
            }
            if bytes[1] == b'1' {
                return Err(InstrumentError::Protocol(
                    "continued AR-DV1 frame is unsupported".into(),
                ));
            }
            return Ok(rest.trim());
        }
    }
    if text == "?" {
        return Err(InstrumentError::Rejected(text.into()));
    }
    Ok(text)
}
fn parse_level(text: &str) -> Result<(u32, u8)> {
    let Some(payload) = text.strip_prefix("LM") else {
        return Err(InstrumentError::Protocol("invalid LM response".into()));
    };
    if !(2..=7).contains(&payload.len()) || !payload.bytes().all(|c| c.is_ascii_digit()) {
        return Err(InstrumentError::Protocol("invalid raw LM count".into()));
    }
    let squelch = payload.as_bytes()[payload.len() - 1] - b'0';
    if squelch > 3 {
        return Err(InstrumentError::Protocol(
            "invalid LM squelch status".into(),
        ));
    }
    let level = payload[..payload.len() - 1]
        .parse()
        .map_err(|_| InstrumentError::Protocol("invalid LM count".into()))?;
    Ok((level, squelch))
}
