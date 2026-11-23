use crate::{tinysa::now, transport::Connection, *};
use serde_json::json;
use sha2::{Digest, Sha256};
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
        let points = scope_points(start_hz, stop_hz, step_hz, settle, SEGMENT_POINTS)?;
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
            points,
            vfo_step,
        })
    }
    pub fn points(self) -> usize {
        self.points
    }
}

const SEGMENT_POINTS: usize = 1000;
const CYCLIC_POINTS: usize = 129_991;

fn scope_points(start: u64, stop: u64, step: u64, settle: Duration, limit: usize) -> Result<usize> {
    frequency_command(start)?;
    frequency_command(stop)?;
    if start >= stop
        || !(10..=1_300_000_000).contains(&step)
        || !step.is_multiple_of(10)
        || settle > Duration::from_secs(1)
    {
        return Err(InstrumentError::InvalidRequest(
            "invalid AR-DV1 scope range/settling".into(),
        ));
    }
    let points = (stop - start).div_ceil(step) + 1;
    if points > limit as u64 {
        return Err(InstrumentError::InvalidRequest(format!(
            "AR-DV1 scope is limited to {limit} points"
        )));
    }
    Ok(points as usize)
}

/// An inclusive logical grid, executed as successive bounded scopes on one VFO.
#[derive(Clone, Copy, Debug)]
pub struct CyclicReceiverScope {
    start: u64,
    stop: u64,
    step: u64,
    settle: Duration,
    auto: bool,
    points: usize,
}
impl CyclicReceiverScope {
    pub fn new(start: u64, stop: u64, step: u64, settle: Duration, auto: bool) -> Result<Self> {
        let points = scope_points(start, stop, step, settle, CYCLIC_POINTS)?;
        Ok(Self {
            start,
            stop,
            step,
            settle,
            auto,
            points,
        })
    }
    pub fn points(self) -> usize {
        self.points
    }
    pub fn start_hz(self) -> u64 {
        self.start
    }
    pub fn stop_hz(self) -> u64 {
        self.stop
    }
    pub fn step_hz(self) -> u64 {
        self.step
    }
    pub fn settle(self) -> Duration {
        self.settle
    }
    pub fn strategy(self) -> &'static str {
        if self.auto { "auto" } else { "rf" }
    }
    pub fn frequency_at(self, index: usize) -> Option<u64> {
        (index < self.points).then(|| (self.start + index as u64 * self.step).min(self.stop))
    }
    /// Give the last segment two points when a full preceding segment would leave one.
    pub fn segment_index(self, index: usize) -> Option<usize> {
        if index >= self.points {
            return None;
        }
        let borrowed = self.points % SEGMENT_POINTS == 1 && index == self.points - 2;
        Some((index + usize::from(borrowed)) / SEGMENT_POINTS)
    }
    fn segment(self, index: usize) -> Result<ScopeRun> {
        let number = self.segment_index(index).ok_or_else(|| {
            InstrumentError::InvalidRequest("AR-DV1 scan index outside plan".into())
        })?;
        let last = (self.points - 1) / SEGMENT_POINTS;
        let singleton = self.points % SEGMENT_POINTS == 1;
        let first = number * SEGMENT_POINTS - usize::from(singleton && number == last);
        let count = (self.points - first).min(SEGMENT_POINTS)
            - usize::from(singleton && number + 1 == last);
        let mut scope = ReceiverScope::new(
            self.frequency_at(first).expect("segment start"),
            self.frequency_at(first + count - 1).expect("segment end"),
            self.step,
            self.settle,
            self.auto,
        )?;
        // A reconnect may enter directly at the clipped top of a segment.
        if !self
            .frequency_at(index)
            .expect("segment point")
            .is_multiple_of(self.step)
        {
            scope.vfo_step = None;
        }
        Ok(ScopeRun::new(scope, index - first))
    }
}

struct ScopeRun {
    config: ReceiverScope,
    index: usize,
    saved_raster: Option<(String, String)>,
    fresh: bool,
}
impl ScopeRun {
    fn new(config: ReceiverScope, index: usize) -> Self {
        Self {
            config,
            index,
            saved_raster: None,
            fresh: true,
        }
    }
    fn frequency(&self) -> Option<u64> {
        (self.index < self.config.points).then(|| {
            (self.config.start_hz + self.index as u64 * self.config.step_hz)
                .min(self.config.stop_hz)
        })
    }
}

/// The same physical receiver can be recognized inside one stream without
/// publishing its serial or linking independent runs through a stable tag.
struct CyclicStreamIdentity {
    stream_id: String,
    fingerprint: Option<String>,
}
impl CyclicStreamIdentity {
    fn confirm(&mut self, serial: &str) -> Result<()> {
        let mut hash = Sha256::new();
        hash.update(self.stream_id.as_bytes());
        hash.update([0]);
        hash.update(serial.as_bytes());
        let actual: String = hash
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        if self
            .fingerprint
            .as_ref()
            .is_some_and(|saved| saved != &actual)
        {
            return Err(InstrumentError::UnsupportedProfile(
                "AR-DV1 identity differs from the archived cyclic run".into(),
            ));
        }
        self.fingerprint = Some(actual);
        Ok(())
    }
}

/// Documented receive control only. AUTO excludes TETRA; levels are raw counts.
pub struct ArDv1<T> {
    connection: Connection<T>,
    acquisition_window: Option<AcquisitionWindow>,
    requested: ReceiverConfig,
    ready: bool,
    remote_mode_acknowledged: bool,
    frequency: Option<u64>,
    mode: Option<ReceiverMode>,
    source: &'static str,
    firmware: Option<String>,
    sequence: u64,
    tune_sequence: u64,
    verify_frequency: bool,
    tune_settle: Duration,
    mode_readback: bool,
    settled_at: Option<Instant>,
    scope: Option<ScopeRun>,
    cyclic_scope: Option<CyclicReceiverScope>,
    cyclic_identity: Option<CyclicStreamIdentity>,
    verified_dv1: bool,
}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> ArDv1<T> {
    pub fn new(transport: T, config: ReceiverConfig, limits: IoLimits) -> Result<Self> {
        frequency_command(config.frequency_hz)?;
        validate_mode(config.profile, config.mode)?;
        Ok(Self {
            connection: Connection::new(transport, limits)?,
            acquisition_window: None,
            requested: config,
            ready: false,
            remote_mode_acknowledged: false,
            frequency: None,
            mode: None,
            source: "unconfirmed",
            firmware: None,
            sequence: 0,
            tune_sequence: 0,
            verify_frequency: false,
            tune_settle: RX_SETTLE,
            mode_readback: false,
            settled_at: None,
            scope: None,
            cyclic_scope: None,
            cyclic_identity: None,
            verified_dv1: false,
        })
    }
    pub fn with_frequency_readback(self) -> Result<Self> {
        self.with_readback_settle(RX_SETTLE)
    }

    pub fn with_readback_settle(mut self, settle: Duration) -> Result<Self> {
        if settle > Duration::from_secs(1) {
            return Err(InstrumentError::InvalidRequest(
                "AR-DV1 settling exceeds one second".into(),
            ));
        }
        self.tune_settle = settle;
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
        self.scope = Some(ScopeRun::new(config, 0));
        Ok(self)
    }
    pub fn with_cyclic_scope(mut self, plan: CyclicReceiverScope) -> Result<Self> {
        if self.requested.mode != ReceiverMode::Fm {
            return Err(InstrumentError::UnsupportedProfile(
                "AR-DV1 scope requires FM".into(),
            ));
        }
        self = self.with_frequency_readback()?;
        self.requested.frequency_hz = plan.start;
        self.cyclic_scope = Some(plan);
        Ok(self)
    }
    pub fn with_acquisition_window(mut self, window: AcquisitionWindow) -> Self {
        self.acquisition_window = Some(window);
        self
    }
    pub fn with_cyclic_stream(
        self,
        plan: CyclicReceiverScope,
        stream_id: &str,
        archived_identity: Option<String>,
    ) -> Result<Self> {
        let mut adapter = self.with_cyclic_scope(plan)?;
        adapter.cyclic_identity = Some(CyclicStreamIdentity {
            stream_id: stream_id.into(),
            fingerprint: archived_identity,
        });
        Ok(adapter)
    }
    /// The owner supplies the next global index from its committed archive cursor.
    /// Re-entry after manual tuning or reconnect prepares the selected VFO explicitly.
    pub async fn poll_cyclic_point(&mut self, index: usize) -> Result<ReceiverSample> {
        self.connection.ensure()?;
        let plan = self.cyclic_scope.ok_or_else(|| {
            InstrumentError::UnsafeState("AR-DV1 cyclic scan is not configured".into())
        })?;
        let expected = plan.frequency_at(index).ok_or_else(|| {
            InstrumentError::InvalidRequest("AR-DV1 scan index outside plan".into())
        })?;
        if self
            .scope
            .as_ref()
            .is_none_or(|run| run.index == run.config.points)
        {
            self.restore_scope_raster().await?;
            self.scope = Some(plan.segment(index)?);
            self.requested.mode = ReceiverMode::Fm;
        }
        if self.scope.as_ref().and_then(ScopeRun::frequency) != Some(expected) {
            return Err(InstrumentError::UnsafeState(
                "AR-DV1 scan cursor differs from the prepared segment".into(),
            ));
        }
        self.poll_receiver().await
    }
    pub async fn hold_cyclic_scope(&mut self) -> Result<()> {
        self.connection.ensure()?;
        if self.cyclic_scope.is_none() {
            return Err(InstrumentError::UnsafeState(
                "AR-DV1 cyclic scan is not configured".into(),
            ));
        }
        self.restore_scope_raster().await?;
        self.scope = None;
        Ok(())
    }
    pub(crate) fn scanning(&self) -> bool {
        self.scope.is_some()
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
            observed_mode: if !unknown && self.mode_readback {
                self.mode
            } else {
                None
            },
            scan_total_points: self.scope.as_ref().map(|scope| scope.config.points),
            scan_strategy: self.scope.as_ref().map(|scope| {
                if self.verified_dv1 && scope.config.vfo_step.is_some() {
                    "ZK"
                } else {
                    "RF"
                }
                .into()
            }),
            scan_completed_points: self.scope.as_ref().map(|scope| scope.index),
            firmware: self.firmware.clone(),
            hardware_validation: "not_run".into(),
        }
    }
    pub fn invalidate_device_knowledge(&mut self) {
        self.frequency = None;
        self.mode = None;
        self.source = "unknown";
        self.mode_readback = false;
        self.settled_at = None;
    }
    fn failed<E>(&mut self, error: InstrumentError) -> Result<E> {
        if !matches!(error, InstrumentError::AcquisitionWindowClosed) {
            self.connection.taint();
        }
        self.invalidate_device_knowledge();
        Err(error)
    }
    async fn line(&mut self, phase: &'static str, command: &str) -> Result<String> {
        // Cleanup only restores the saved raster and leaves remote mode.
        let window = if phase == "close" {
            None
        } else {
            self.acquisition_window.as_ref()
        };
        self.connection.line(command, window).await
    }
    async fn transact(&mut self, phase: &'static str, command: &str) -> Result<String> {
        let text = self.line(phase, command).await?;
        match body(phase, command, &text) {
            Ok((code, value))
                if self.verify_frequency && (code != Some("20") || value.is_empty()) =>
            {
                self.failed(InstrumentError::Protocol(
                    "AR-DV1 readback requires a terminal query reply".into(),
                ))
            }
            Ok((_, value)) => Ok(value.to_owned()),
            Err(error) => self.failed(error),
        }
    }
    async fn acknowledge(&mut self, phase: &'static str, command: &str) -> Result<()> {
        let raw = self.line(phase, command).await?;
        let code = match body(phase, command, &raw) {
            Ok((code, _)) => code,
            Err(error) => return self.failed(error),
        };
        if raw != "20" && (self.verify_frequency || raw != "10") {
            return self.failed(rejection(
                phase,
                command,
                code.unwrap_or("unexpected acknowledgement"),
            ));
        }
        Ok(())
    }
    /// On generic USB bridges, establish the supported receiver family using only
    /// queries before enabling result codes or sending any receive controls.
    pub async fn identify_monitor(&mut self) -> Result<()> {
        if self.requested.profile != ReceiverProfile::MonitorV1 {
            return Err(InstrumentError::UnsupportedProfile(
                "expected ardv1-monitor-v1".into(),
            ));
        }
        for command in ["VR", "RN"] {
            let reply = self.line("identify", command).await?;
            let (code, value) = body("identify", command, &reply)?;
            let recognized = match command {
                "VR" => value == "VR2504A",
                _ => value.strip_prefix("RN").is_some_and(|serial| {
                    serial.len() == 8
                        && (serial.starts_with("0952") || serial.starts_with("09560"))
                        && serial.bytes().all(|b| b.is_ascii_digit())
                }),
            };
            if !matches!(code, None | Some("20")) || !recognized {
                return self.failed(InstrumentError::UnsupportedProfile(
                    "unknown AR-DV1 monitor identity".into(),
                ));
            }
        }
        Ok(())
    }

    pub async fn prepare(&mut self) -> Result<()> {
        self.connection.ensure()?;
        if self.ready {
            return Ok(());
        }
        let result = async {
            self.acknowledge("prepare", "RE1").await?;
            self.remote_mode_acknowledged = true;
            let firmware = self.transact("prepare", "VR").await?;
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
            let serial = self.transact("prepare", "RN").await?;
            if !serial.starts_with("RN") || serial.len() <= 2 {
                return Err(InstrumentError::Protocol(
                    "missing AR-DV1 serial response".into(),
                ));
            }
            if let Some(identity) = &mut self.cyclic_identity {
                identity.confirm(&serial)?;
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
            self.acknowledge("tune", "VFA").await?;
            self.acknowledge("tune", mode_command(mode)).await?;
            self.acknowledge("tune", &command).await?;
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
            self.settled_at = Some(Instant::now() + self.tune_settle);
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
        self.mode_readback = status.is_some();
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
            "mode_source":if self.mode_readback { decoder_source } else { self.source },
            "requested_frequency_hz":self.requested.frequency_hz, "requested_mode":self.requested.mode,
            "observed_frequency_hz":verified.then_some(frequency_hz), "observed_mode":self.mode_readback.then_some(mode),
            "frequency_readback":verified,"simulated":false,"hardware_validation":"not_run"});
        if let Some(raw) = raw_rx {
            metadata["raw_receiver_status"] = json!(raw);
        }
        if let Some((scope, command)) = self.scope.as_mut().zip(scan_command) {
            metadata["scan_index"] = json!(scope.index);
            metadata["scan_total"] = json!(scope.config.points);
            metadata["scan_tune_command"] = json!(command);
            metadata["scope_source"] = json!("sequential_receiver_readback");
            if let Some(identity) = &self.cyclic_identity {
                metadata["receiver_run_unit"] = json!(identity.fingerprint);
            }
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
        let config = scope.config;
        let fresh = scope.fresh;
        let frequency = scope
            .frequency()
            .ok_or_else(|| InstrumentError::UnsafeState("AR-DV1 scope has finished".into()))?;
        let previous = self.frequency;
        self.requested.frequency_hz = frequency;
        self.invalidate_device_knowledge();
        let mut command = "RF";
        if fresh {
            self.prepare().await?;
            self.acknowledge("scan", "VFA").await?;
            if let Some(step) = config
                .vfo_step
                .filter(|_| self.verified_dv1 && frequency.is_multiple_of(config.step_hz))
            {
                let saved_step = self.transact("scan", "ST").await?;
                let saved_shift = self.transact("scan", "SH").await?;
                if !valid_rx_step(&saved_step) || !valid_raster(&saved_shift, "SH") {
                    return Err(InstrumentError::Protocol(
                        "invalid AR-DV1 VFO raster".into(),
                    ));
                }
                self.scope.as_mut().unwrap().saved_raster = Some((saved_step, saved_shift));
                self.acknowledge(
                    "scan",
                    &format!(
                        "VFA {} ST{step} SH000.00 MD0F0",
                        frequency_command(frequency)?
                    ),
                )
                .await?;
                if self.transact("scan", "SH").await? != "SH000.00" {
                    return Err(InstrumentError::Protocol(
                        "AR-DV1 did not confirm zero VFO shift".into(),
                    ));
                }
            } else {
                self.acknowledge("scan", "MD0F0").await?;
                self.acknowledge("scan", &frequency_command(frequency)?)
                    .await?;
            }
        } else if self.scope.as_ref().unwrap().saved_raster.is_some()
            && previous.is_some_and(|value| value + config.step_hz == frequency)
        {
            self.acknowledge("scan", "ZK").await?;
            command = "ZK";
        } else {
            self.acknowledge("scan", &frequency_command(frequency)?)
                .await?;
        }
        self.scope.as_mut().expect("scope acquisition").fresh = false;
        self.frequency = Some(frequency);
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
            let raw = self.transact("sample", "RX").await?;
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
        let (level, squelch) = parse_level(&self.transact("sample", "LM").await?)?;
        let status = if self.requested.profile == ReceiverProfile::MonitorV1 {
            let status = self.transact("sample", "MD").await?;
            verify_mode(&status, mode)?;
            Some(status)
        } else {
            None
        };
        Ok((level, squelch, status, None))
    }
    async fn restore_scope_raster(&mut self) -> Result<()> {
        if let Some((step, shift)) = self
            .scope
            .as_ref()
            .and_then(|scope| scope.saved_raster.clone())
        {
            self.acknowledge("close", &format!("VFA {step} {shift}"))
                .await?;
            if self.transact("close", "ST").await? != step
                || self.transact("close", "SH").await? != shift
            {
                return self.failed(InstrumentError::Protocol(
                    "AR-DV1 did not confirm VFO raster restoration".into(),
                ));
            }
            self.scope.as_mut().expect("restored scope").saved_raster = None;
        }
        Ok(())
    }

    /// Continue a completed bounded scope on the same identified connection.
    pub async fn continue_scope(&mut self, next: ReceiverScope) -> Result<()> {
        self.connection.ensure()?;
        if !self
            .scope
            .as_ref()
            .is_some_and(|scope| scope.index == scope.config.points)
        {
            return Err(InstrumentError::UnsafeState(
                "previous AR-DV1 scope is incomplete".into(),
            ));
        }
        self.restore_scope_raster().await?;
        self.scope = Some(ScopeRun::new(next, 0));
        Ok(())
    }

    pub async fn close(&mut self) -> Result<()> {
        if self.connection.closed() {
            return Ok(());
        }
        let result = if self.remote_mode_acknowledged && !self.connection.tainted() {
            async {
                self.restore_scope_raster().await?;
                self.acknowledge("close", "EX").await
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
fn rejection(phase: &str, command: &str, code: &str) -> InstrumentError {
    // Command arguments and reply payloads belong only in the private wire trace.
    let name = if command == "RE1" {
        command
    } else {
        command
            .split(|c: char| !c.is_ascii_uppercase())
            .next()
            .unwrap_or(command)
    };
    InstrumentError::Rejected(format!("AR-DV1 {phase} {name}: {code}"))
}

fn body<'a>(phase: &str, command: &str, text: &'a str) -> Result<(Option<&'a str>, &'a str)> {
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
                let code = if rest.starts_with('?') {
                    &text[..3]
                } else {
                    &text[..2]
                };
                return Err(rejection(phase, command, code));
            }
            if bytes[1] == b'1' {
                return Err(InstrumentError::Protocol(
                    "continued AR-DV1 frame is unsupported".into(),
                ));
            }
            return Ok((Some(&text[..2]), rest.trim()));
        }
    }
    if text == "?" {
        return Err(rejection(phase, command, "?"));
    }
    Ok((None, text))
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
