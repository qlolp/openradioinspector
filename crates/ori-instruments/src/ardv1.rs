use crate::{tinysa::now, transport::Connection, *};
use serde_json::json;
use tokio::io::{AsyncRead, AsyncWrite};

/// Documented receive control only. AUTO excludes TETRA; LM remains raw counts.
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
        })
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
            frequency_readback: false,
            firmware: self.firmware.clone(),
            hardware_validation: "not_run".into(),
        }
    }
    pub fn invalidate_device_knowledge(&mut self) {
        self.frequency = None;
        self.mode = None;
        self.source = "unknown";
    }
    fn failed<E>(&mut self, error: InstrumentError) -> Result<E> {
        self.connection.taint();
        self.invalidate_device_knowledge();
        Err(error)
    }
    async fn transact(&mut self, command: &str) -> Result<String> {
        let text = self.connection.line(command).await?;
        match body(&text) {
            Ok(value) => Ok(value.to_owned()),
            Err(error) => self.failed(error),
        }
    }
    async fn acknowledge(&mut self, command: &str) -> Result<()> {
        let raw = self.connection.line(command).await?;
        if let Err(error) = body(&raw) {
            return self.failed(error);
        }
        if raw != "10" && raw != "20" {
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
        Ok(())
    }
    pub async fn poll_receiver(&mut self) -> Result<ReceiverSample> {
        self.connection.ensure()?;
        if self.source == "unconfirmed" {
            self.tune(self.requested.frequency_hz, self.requested.mode)
                .await?;
        }
        let (Some(frequency_hz), Some(mode)) = (self.frequency, self.mode) else {
            return Err(InstrumentError::UnsafeState(
                "receiver frequency/mode unknown; explicit tune required".into(),
            ));
        };
        let started_at = now();
        let result = async {
            let raw_level = self.transact("LM").await?;
            let (level, squelch) = parse_level(&raw_level)?;
            let status = if self.requested.profile == ReceiverProfile::MonitorV1 {
                Some(self.transact("MD").await?)
            } else {
                None
            };
            if let Some(status) = &status {
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
            }
            Ok((level, squelch, status))
        }
        .await;
        let (level, squelch, status) = match result {
            Ok(value) => value,
            Err(error) => return self.failed(error),
        };
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
        let metadata = json!({"receiver_profile":self.requested.profile,"receiver_firmware":self.firmware,
            "raw_mode_status":status,"decoder_source":if status.is_some(){"receiver_MD"}else{"unavailable"},"raw_squelch_status":squelch,
            "adapter":"ardv1-serial","sequence":self.sequence,"tune_sequence":self.tune_sequence,"frequency_source":self.source,
            "mode_source":self.source,"frequency_readback":false,"simulated":false,"hardware_validation":"not_run"});
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
    pub async fn close(&mut self) -> Result<()> {
        if self.connection.closed() {
            return Ok(());
        }
        let result = if self.ready && !self.connection.tainted() {
            self.acknowledge("EX").await
        } else {
            Ok(())
        };
        self.invalidate_device_knowledge();
        let closed = self.connection.close().await;
        result.and(closed)
    }
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
