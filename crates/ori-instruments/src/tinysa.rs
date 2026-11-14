use crate::{transport::Connection, *};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use std::collections::HashSet;
use tokio::io::{AsyncRead, AsyncWrite};

pub const RECEIVE_PROFILE: &str = "tinysa-ultra-zs405-receive-v1";
pub const CONTROL_PROFILE: &str = "tinysa-ultra-zs405-control-v1";
pub const FIRMWARE: &str = "tinySA4_v1.4-235-gea71dfd";
/// One independently owned serial connection. A failed or dropped exchange
/// permanently taints it; recovering requires a newly opened transport.
pub struct TinySa<T> {
    connection: Connection<T>,
    receive: ReceiveConfig,
    ready: bool,
    device_mode: DeviceMode,
    control_mode: ControlMode,
    commands: Option<HashSet<String>>,
    settings: Value,
    identity: Value,
    identity_verified: bool,
    sequence: u64,
    output_frequency: bool,
    output_level: bool,
    source: &'static str,
    trace_processed: bool,
}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> TinySa<T> {
    pub fn new(transport: T, receive: ReceiveConfig, limits: IoLimits) -> Result<Self> {
        validate_receive(receive)?;
        Ok(Self {
            connection: Connection::new(transport, limits)?,
            receive,
            ready: false,
            device_mode: DeviceMode::Unknown,
            control_mode: ControlMode::Monitoring,
            commands: None,
            settings: json!({"start_hz":receive.start_hz,"stop_hz":receive.stop_hz,"points":receive.points,"rbw_hz":receive.rbw_hz,
                "attenuation_db":"auto","lna":false,"ultra":false,"calculation":"off"}),
            identity: json!({}),
            identity_verified: false,
            sequence: 0,
            output_frequency: false,
            output_level: false,
            source: "unknown",
            trace_processed: false,
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
    pub fn state(&self) -> TinySaState {
        let unknown = self.connection.tainted() || self.connection.closed();
        TinySaState {
            device_mode: if unknown {
                DeviceMode::Unknown
            } else {
                self.device_mode
            },
            control_mode: self.control_mode,
            restart_required: self.connection.tainted(),
            closed: self.connection.closed(),
            profile_verified: self.ready && !unknown,
            control_profile_verified: self.identity_verified && self.commands.is_some() && !unknown,
            settings_source: if unknown { "unknown" } else { self.source }.into(),
            settings: self.settings.clone(),
            device_mode_readback: false,
            generator_output_readback: false,
            settings_readback: false,
            hardware_validation: "not_run".into(),
        }
    }
    /// Explicitly invalidate knowledge after an external/manual change. This
    /// cannot establish input/output, and does not send an arbitrary command.
    pub fn invalidate_device_knowledge(&mut self) {
        self.device_mode = DeviceMode::Unknown;
        self.source = "unknown";
        self.trace_processed = true;
        self.output_frequency = false;
        self.output_level = false;
    }
    fn failed<E>(&mut self, error: InstrumentError) -> Result<E> {
        self.connection.taint();
        self.invalidate_device_knowledge();
        Err(error)
    }
    async fn ack(&mut self, command: &str) -> Result<()> {
        let body = self
            .connection
            .text(command, self.connection.limits.text_timeout, false)
            .await?;
        if !body.is_empty() {
            return self.failed(InstrumentError::Rejected(format!("{command}: {body}")));
        }
        self.source = "command_acknowledged";
        if command == "mode input" {
            self.device_mode = DeviceMode::Input;
        }
        if command == "mode output" {
            self.device_mode = DeviceMode::Output;
        }
        Ok(())
    }
    pub async fn prepare(&mut self) -> Result<()> {
        self.connection.ensure()?;
        if self.ready {
            return Ok(());
        }
        let result = async {
            self.verify_identity(RECEIVE_PROFILE).await?;
            for command in [
                "output off".to_owned(),
                "mode input".to_owned(),
                "lna off".to_owned(),
                "attenuate auto".to_owned(),
                format!("rbw {}", self.receive.rbw_hz / 1000),
                "calc off".to_owned(),
            ] {
                self.ack(&command).await?;
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
    async fn verify_identity(&mut self, profile: &str) -> Result<()> {
        self.connection.ensure()?;
        if self.identity_verified {
            return Ok(());
        }
        let timeout = self.connection.limits.text_timeout;
        let info = self.connection.text("info", timeout, false).await?;
        let firmware = self.connection.text("version", timeout, false).await?;
        let deviceid = self.connection.text("deviceid", timeout, false).await?;
        self.identity = json!({"info":info,"firmware":firmware,"deviceid":deviceid});
        if !matches_identity(&info, &firmware) || deviceid.trim().is_empty() {
            return Err(InstrumentError::UnsupportedProfile(profile.into()));
        }
        self.identity_verified = true;
        Ok(())
    }
    /// Query-only inspection. Unlike Python's cold controller initialization,
    /// this never prepares the receive path or establishes hardware input mode.
    pub async fn inspect(&mut self) -> Result<TinySaInspection> {
        self.connection.ensure()?;
        if self.commands.is_none() {
            let result = async {
                self.verify_identity(CONTROL_PROFILE).await?;
                let text = self
                    .connection
                    .text("help", self.connection.limits.text_timeout, false)
                    .await?;
                self.commands = Some(help_commands(&text));
                Ok(())
            }
            .await;
            if let Err(error) = result {
                return self.failed(error);
            }
        }
        let mut commands: Vec<String> = self
            .commands
            .as_ref()
            .expect("queried help")
            .iter()
            .cloned()
            .collect();
        commands.sort_unstable();
        Ok(TinySaInspection {
            state: self.state(),
            commands,
        })
    }
    async fn capabilities(&mut self) -> Result<()> {
        self.prepare().await?;
        if self.commands.is_none() {
            self.inspect().await?;
        }
        Ok(())
    }
    fn require_command(&self, command: &str) -> Result<()> {
        if !self.commands.as_ref().is_some_and(|commands| {
            commands.contains(command.split_whitespace().next().unwrap_or(""))
        }) {
            return Err(InstrumentError::UnsupportedCommand(command.into()));
        }
        Ok(())
    }
    async fn checked_ack(&mut self, command: &str) -> Result<()> {
        self.require_command(command)?;
        self.ack(command).await
    }
    fn require_lab(&self) -> Result<()> {
        if self.control_mode != ControlMode::Lab {
            return Err(InstrumentError::UnsafeState(
                "exclusive lab mode is required; caller must enforce lease".into(),
            ));
        }
        Ok(())
    }
    fn require_input(&self) -> Result<()> {
        self.connection.ensure()?;
        if !self.ready || self.device_mode != DeviceMode::Input {
            return Err(InstrumentError::UnsafeState(
                "acquisition requires acknowledged mode input; explicitly resume monitoring".into(),
            ));
        }
        Ok(())
    }
    fn provenance(&self) -> Value {
        json!({"adapter":"tinysa-serial","receive_profile":if self.commands.is_some(){CONTROL_PROFILE}else{RECEIVE_PROFILE},"device_identity":self.identity,
            "rbw_hz":self.settings["rbw_hz"],"rbw_requested_hz":self.settings["rbw_hz"],"rbw_applied_state":"unknown",
            "settings_source":self.source,"settings_readback":false,"sequence":self.sequence,"simulated":false,
            "generator_output":"unknown","generator_output_source":"command_acknowledged_no_physical_readback",
            "generator_output_readback":false,"device_mode":self.device_mode,"device_mode_readback":false,
            "hardware_validation":"not_run","instrument_settings":self.settings})
    }
    pub async fn poll_spectrum(&mut self) -> Result<SpectrumSample> {
        self.connection.ensure()?;
        if !self.ready {
            self.prepare().await?;
        }
        self.require_input()?;
        if self.control_mode != ControlMode::Monitoring {
            return Err(InstrumentError::UnsafeState(
                "monitoring poll is paused in lab mode".into(),
            ));
        }
        if self.receive.start_hz == self.receive.stop_hz {
            let sample = self
                .acquire_raw(RawScanConfig {
                    start_hz: self.receive.start_hz,
                    stop_hz: self.receive.stop_hz,
                    points: self.receive.points,
                })
                .await?;
            return Ok(SpectrumSample {
                kind: SpectrumKind::ZeroSpan,
                frequencies_hz: sample.frequencies_hz,
                levels: sample.levels,
                level_unit: sample.level_unit,
                settings: sample.settings,
                interval: sample.interval,
            });
        }
        let start = self.receive.start_hz;
        let stop = self.receive.stop_hz;
        let points = self.receive.points;
        let command = format!(
            "scan {} {} {points} 3",
            frequency_token(start),
            frequency_token(stop)
        );
        let started_at = now();
        let body = self
            .connection
            .text(&command, self.connection.limits.scan_timeout, true)
            .await?;
        let (frequencies_hz, levels) = match parse_scan(&body, points) {
            Ok(value) => value,
            Err(error) => return self.failed(error),
        };
        if frequencies_hz.first() != Some(&start) || frequencies_hz.last() != Some(&stop) {
            return self.failed(InstrumentError::Protocol(
                "scan frequency edges differ from requested range".into(),
            ));
        }
        self.sequence += 1;
        let unit = self.settings["trace_unit"]
            .as_str()
            .unwrap_or("dBm")
            .to_owned();
        let mut settings = self.provenance();
        settings["artifact_kind"] = json!(if self.trace_processed
            || self.settings["calculation"] != "off"
            || unit != "dBm"
        {
            "processed_trace"
        } else {
            "raw_spectrum"
        });
        let kind = if self.trace_processed || self.settings["calculation"] != "off" || unit != "dBm"
        {
            SpectrumKind::ProcessedTrace
        } else {
            SpectrumKind::RawSpectrum
        };
        Ok(SpectrumSample {
            kind,
            frequencies_hz,
            levels,
            level_unit: unit,
            settings,
            interval: OperationInterval {
                started_at,
                ended_at: now(),
            },
        })
    }
    pub async fn acquire_raw(&mut self, request: RawScanConfig) -> Result<RawSpectrumSample> {
        self.require_input()?;
        let maximum = if self.settings["ultra"] == true {
            6_000_000_000
        } else {
            800_000_000
        };
        if request.start_hz < 100_000
            || request.start_hz > request.stop_hz
            || request.stop_hz > maximum
            || !(1..=65536).contains(&request.points)
        {
            return Err(InstrumentError::InvalidRequest(
                "scanraw grid is outside explicitly selected profile".into(),
            ));
        }
        self.capabilities().await?;
        self.require_command("zero")?;
        self.require_command("scanraw")?;
        let zero = self
            .connection
            .text("zero", self.connection.limits.text_timeout, false)
            .await?;
        let zero_db = zero
            .strip_prefix("zero {level}\n")
            .and_then(|rest| rest.strip_suffix("dBm"))
            .filter(|n| !n.is_empty() && n.len() <= 4 && n.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|n| n.parse::<u16>().ok())
            .filter(|&n| n <= 2047);
        let Some(zero_db) = zero_db else {
            return self.failed(InstrumentError::Protocol(
                "ambiguous zero readback; scanraw not sent".into(),
            ));
        };
        let command = format!(
            "scanraw {} {} {} 0",
            request.start_hz, request.stop_hz, request.points
        );
        let started_at = now();
        let wire = self
            .connection
            .binary(
                &command,
                Some(3 * request.points as usize + 2),
                self.connection.limits.scan_timeout,
            )
            .await?;
        let (frequencies_hz, levels) = match decode_scanraw(
            &wire,
            request.start_hz,
            request.stop_hz,
            request.points,
            zero_db,
        ) {
            Ok(value) => value,
            Err(error) => return self.failed(error),
        };
        let mut settings = self.provenance();
        settings["requested_stop_hz"] = json!(request.stop_hz);
        settings["stop_is_inclusive"] = if request.start_hz == request.stop_hz {
            Value::Null
        } else {
            json!(false)
        };
        settings["grid_source"] = json!("firmware_ea71dfd_scanraw");
        settings["sample_offsets_seconds"] = Value::Null;
        settings["timing_source"] = json!("interval_only");
        Ok(RawSpectrumSample {
            kind: if request.start_hz == request.stop_hz {
                SpectrumKind::ZeroSpan
            } else {
                SpectrumKind::RawSpectrum
            },
            frequencies_hz,
            levels,
            level_unit: "dBm".into(),
            settings,
            wire,
            zero_db,
            interval: OperationInterval {
                started_at,
                ended_at: now(),
            },
        })
    }
    pub async fn read_trace(&mut self, trace: u8) -> Result<DeviceTextSample> {
        if !(1..=4).contains(&trace) {
            return Err(InstrumentError::InvalidRequest("trace must be 1..4".into()));
        }
        self.capabilities().await?;
        self.require_command("trace")?;
        let started_at = now();
        let body = self
            .connection
            .text(
                &format!("trace {trace} value"),
                self.connection.limits.scan_timeout,
                false,
            )
            .await?;
        Ok(DeviceTextSample {
            body,
            level_unit: "internal_dBm".into(),
            interval: OperationInterval {
                started_at,
                ended_at: now(),
            },
            metadata: json!({"readback":"firmware_trace_value","trace":trace,"hardware_validation":"not_run"}),
        })
    }
    pub async fn marker(&mut self, command: MarkerCommand) -> Result<DeviceTextSample> {
        let wire = marker_command(&command)?;
        self.capabilities().await?;
        self.require_command("marker")?;
        let started_at = now();
        let body = self
            .connection
            .text(&wire, self.connection.limits.scan_timeout, false)
            .await?;
        Ok(DeviceTextSample {
            body,
            level_unit: self.settings["trace_unit"].as_str().unwrap_or("dBm").into(),
            interval: OperationInterval {
                started_at,
                ended_at: now(),
            },
            metadata: json!({"source":"device_marker_response","hardware_validation":"not_run"}),
        })
    }
    pub async fn read_rbw(&mut self) -> Result<f64> {
        self.capabilities().await?;
        self.require_command("rbw")?;
        let body = self
            .connection
            .text("rbw", self.connection.limits.text_timeout, false)
            .await?;
        let value = body
            .rsplit_once('\n')
            .and_then(|(_, line)| line.strip_suffix("Hz"))
            .filter(|v| {
                let (whole, fraction) = match v.split_once('.') {
                    Some((whole, fraction)) => (whole, Some(fraction)),
                    None => (*v, None),
                };
                !whole.is_empty()
                    && whole.bytes().all(|b| b.is_ascii_digit())
                    && fraction.is_none_or(|fraction| {
                        !fraction.is_empty() && fraction.bytes().all(|b| b.is_ascii_digit())
                    })
            })
            .and_then(|v| v.parse::<f64>().ok());
        match value {
            Some(value) if value.is_finite() && value > 0.0 => Ok(value),
            _ => self.failed(InstrumentError::Protocol(
                "missing actual RBW readback".into(),
            )),
        }
    }
    pub async fn read_sd(&mut self, filename: &str) -> Result<Vec<u8>> {
        validate_filename(filename)?;
        self.capabilities().await?;
        self.require_command("sd_read")?;
        self.connection
            .binary(
                &format!("sd_read {filename}"),
                None,
                self.connection.limits.scan_timeout,
            )
            .await
    }
    pub async fn list_sd(&mut self) -> Result<String> {
        self.capabilities().await?;
        self.require_command("sd_list")?;
        self.connection
            .text("sd_list", self.connection.limits.scan_timeout, false)
            .await
    }
    pub async fn capture_screen(&mut self) -> Result<Vec<u8>> {
        self.capabilities().await?;
        self.require_command("capture")?;
        self.connection
            .binary(
                "capture",
                Some(480 * 320 * 2),
                self.connection.limits.scan_timeout,
            )
            .await
    }
    async fn safe_off(&mut self) -> Result<()> {
        self.require_command("output")?;
        for command in ["output off", "caloutput off", "release"] {
            if self.require_command(command).is_ok() {
                self.ack(command).await?;
            }
        }
        self.settings["output_enabled"] = json!(false);
        self.settings["caloutput_mhz"] = json!("off");
        self.output_frequency = false;
        self.output_level = false;
        Ok(())
    }
    pub async fn execute(&mut self, control: TinySaControl) -> Result<TinySaState> {
        self.connection.ensure()?;
        // Bound strings and validate all request data before profile/mutation I/O.
        validate_control_request(&control)?;
        if let TinySaControl::Set(setting) = &control {
            self.validate_setting_context(setting)?;
        }
        // Complete merged validation precedes even profile-query I/O.
        let preset = if let TinySaControl::ApplyReceivePreset(request) = &control {
            Some(self.receive_preset_plan(request)?)
        } else {
            None
        };
        match &control {
            TinySaControl::EnterLab | TinySaControl::OutputOff | TinySaControl::Trace(_) => {}
            TinySaControl::Set(setting) if !setting_command(setting)?.requires_lab => {}
            _ => self.require_lab()?,
        }
        self.capabilities().await?;
        let result = async {
            match control {
                TinySaControl::EnterLab => {
                    if self.control_mode == ControlMode::Lab {
                        return Err(InstrumentError::UnsafeState(
                            "lab mode already active".into(),
                        ));
                    }
                    self.require_command("output")?;
                    self.checked_ack("pause").await?;
                    self.safe_off().await?;
                    self.control_mode = ControlMode::Lab;
                }
                TinySaControl::ReenterLab => {
                    self.require_command("output")?;
                    self.checked_ack("pause").await?;
                    self.safe_off().await?;
                }
                TinySaControl::OutputOff => self.safe_off().await?,
                TinySaControl::ResumeMonitoring => {
                    self.safe_off().await?;
                    self.checked_ack("mode input").await?;
                    for command in [
                        "lna off",
                        "attenuate auto",
                        "calc off",
                        "trigger auto",
                        "trace dBm",
                        "refresh off",
                    ] {
                        if self.require_command(command).is_ok() {
                            self.ack(command).await?;
                        }
                    }
                    self.settings["lna"] = json!(false);
                    self.settings["attenuation_db"] = json!("auto");
                    self.settings["calculation"] = json!("off");
                    self.settings["trigger_mode"] = json!("auto");
                    self.settings["trace_unit"] = json!("dBm");
                    self.control_mode = ControlMode::Monitoring;
                }
                TinySaControl::Set(setting) => self.apply_setting(setting).await?,
                TinySaControl::ApplyReceivePreset(_) => {
                    let (mut merged, commands) = preset.expect("validated preset plan");
                    for wire in &commands {
                        self.require_command(wire)?;
                    }
                    // Validate every supported command before the first mutation.
                    for wire in ["output", "mode", "lna", "ultra", "sweep"] {
                        self.require_command(wire)?;
                    }
                    self.safe_off().await?;
                    for wire in commands {
                        self.ack(&wire).await?;
                    }
                    self.receive.start_hz = merged["start_hz"].as_u64().unwrap();
                    self.receive.stop_hz = merged["stop_hz"].as_u64().unwrap();
                    self.receive.points = merged["points"].as_u64().unwrap() as u32;
                    if let Some(rbw) = merged["rbw_hz"].as_u64() {
                        self.receive.rbw_hz = rbw as u32;
                    }
                    // The safe-off ACK is known even if a prior lab setting was on.
                    merged["output_enabled"] = json!(false);
                    merged["caloutput_mhz"] = json!("off");
                    self.settings = merged;
                }
                TinySaControl::Trace(command) => {
                    let wire = trace_command(&command)?;
                    self.checked_ack(&wire).await?;
                    self.trace_processed = true;
                }
                TinySaControl::GeneratorSweep(request) => self.generator_sweep(request).await?,
                TinySaControl::DeleteSd { filename, .. } => {
                    self.require_command("sd_delete")?;
                    let wire = format!("sd_delete {filename}");
                    let body = self
                        .connection
                        .text(&wire, self.connection.limits.scan_timeout, false)
                        .await?;
                    if body != format!("delete: {filename} OK") {
                        return self.failed(InstrumentError::Rejected(format!(
                            "SD deletion not acknowledged: {body}"
                        )));
                    }
                }
                TinySaControl::Release => {
                    self.checked_ack("release").await?;
                    self.invalidate_device_knowledge();
                }
                TinySaControl::MenuHome => {
                    self.checked_ack("menu").await?;
                    self.invalidate_device_knowledge();
                }
                TinySaControl::Keypad { text } => {
                    self.checked_ack(&format!("text {text}")).await?;
                    self.invalidate_device_knowledge();
                }
                TinySaControl::SavePreset { slot, .. } => {
                    self.checked_ack(&format!("save {slot}")).await?;
                    self.output_frequency = false;
                    self.output_level = false;
                    self.source = "unknown";
                }
                TinySaControl::Touch { x, y } => {
                    self.checked_ack(&format!("touch {x} {y}")).await?;
                    self.invalidate_device_knowledge();
                }
                TinySaControl::LoadPreset { slot, .. } => {
                    self.checked_ack(&format!("recall {slot}")).await?;
                    self.invalidate_device_knowledge();
                }
                TinySaControl::SelfTest { test, .. } => {
                    self.safe_off().await?;
                    self.checked_ack(&format!("selftest {test}")).await?;
                    self.invalidate_device_knowledge();
                }
            }
            Ok(())
        }
        .await;
        if let Err(error) = result {
            if self.connection.tainted() {
                self.invalidate_device_knowledge();
            }
            return Err(error);
        }
        Ok(self.state())
    }
    async fn generator_sweep(&mut self, request: GeneratorSweepConfig) -> Result<()> {
        let format = crate::settings::format_generator;
        let commands = vec![
            "mode output".into(),
            "output off".into(),
            format!(
                "sweep {} {} {}",
                request.start_hz, request.stop_hz, request.points
            ),
            format!("level {}", format(request.level_dbm)),
            format!("levelchange {}", format(request.delta_db)),
            format!("sweeptime {}", format(request.seconds)),
            "output on".into(),
            "sweep go".into(),
            "resume".into(),
        ];
        for wire in &commands {
            self.require_command(wire)?;
        }
        self.safe_off().await?;
        for wire in commands {
            self.ack(&wire).await?;
        }
        self.settings["output_enabled"] = json!(true);
        self.settings["generator_sweep"] = json!(request);
        Ok(())
    }
    fn receive_preset_plan(&self, request: &ReceivePreset) -> Result<(Value, Vec<String>)> {
        validate_receive_preset(request)?;
        let mut merged = self.settings.clone();
        for setting in &request.settings {
            let (key, value) = crate::settings::setting_value(setting)?;
            merged[key] = value;
        }
        let start = merged["start_hz"].as_u64().unwrap_or(0);
        let stop = merged["stop_hz"].as_u64().unwrap_or(0);
        let points = merged["points"].as_u64().unwrap_or(0);
        let maximum = if merged["ultra"] == true {
            6_000_000_000
        } else {
            800_000_000
        };
        if start < 100_000 || start > stop || stop > maximum || !(2..=450).contains(&points) {
            return Err(InstrumentError::InvalidRequest(
                "invalid merged receive preset range/points".into(),
            ));
        }
        if merged["lna"] == true
            && merged["attenuation_db"] != 0
            && merged["attenuation_db"] != "auto"
        {
            return Err(InstrumentError::InvalidRequest(
                "receive preset LNA/attenuator conflict".into(),
            ));
        }
        let mut commands = vec![
            "mode input".into(),
            "lna off".into(),
            format!(
                "ultra {}",
                if merged["ultra"] == true { "on" } else { "off" }
            ),
            format!("sweep {start} {stop} {points}"),
        ];
        for setting in &request.settings {
            if !matches!(
                setting,
                Setting::StartHz(_)
                    | Setting::StopHz(_)
                    | Setting::Points(_)
                    | Setting::Ultra(_)
                    | Setting::Lna(_)
            ) {
                commands.push(setting_command(setting)?.wire);
            }
        }
        if merged["lna"] == true {
            commands.push("lna on".into());
        }
        Ok((merged, commands))
    }
    fn validate_setting_context(&self, setting: &Setting) -> Result<()> {
        let error = |text: &str| InstrumentError::InvalidRequest(text.into());
        let maximum = if self.settings["ultra"] == true {
            6_000_000_000
        } else {
            800_000_000
        };
        match setting {
            Setting::StartHz(value) if *value >= self.receive.stop_hz || *value > maximum => {
                return Err(error(
                    "start must be below stop and within explicitly selected Ultra range",
                ));
            }
            Setting::StopHz(value) if *value <= self.receive.start_hz || *value > maximum => {
                return Err(error(
                    "stop must be above start and within explicitly selected Ultra range",
                ));
            }
            Setting::CenterHz(_) | Setting::SpanHz(_) => {
                self.updated_grid(setting)?;
            }
            Setting::Ultra(false) if self.receive.stop_hz > 800_000_000 => {
                return Err(error("return range below 800 MHz before disabling Ultra"));
            }
            Setting::Lna(true)
                if self.settings["attenuation_db"] != "auto"
                    && self.settings["attenuation_db"] != 0 =>
            {
                return Err(error("LNA requires auto or zero attenuation"));
            }
            Setting::AttenuationDb(value)
                if self.settings["lna"] == true && !matches!(value, Attenuation::Db(0)) =>
            {
                return Err(error("disable LNA before changing attenuation"));
            }
            Setting::ModulationHz(value)
                if self.settings["modulation"] == "fm" && *value > 1000 =>
            {
                return Err(error("FM modulation frequency must not exceed 1000 Hz"));
            }
            Setting::Modulation(Modulation::Fm)
                if self.settings["modulation_hz"].as_u64().unwrap_or(1000) > 1000 =>
            {
                return Err(error("reduce modulation frequency before selecting FM"));
            }
            Setting::OutputEnabled(true) if !self.output_frequency || !self.output_level => {
                return Err(InstrumentError::UnsafeState(
                    "explicitly set generator frequency and level before enabling output".into(),
                ));
            }
            _ => {}
        }
        Ok(())
    }
    fn updated_grid(&self, setting: &Setting) -> Result<(u64, u64)> {
        let current_span = self.receive.stop_hz - self.receive.start_hz;
        let span = if let Setting::SpanHz(span) = setting {
            *span
        } else {
            current_span
        };
        let center = if let Setting::CenterHz(center) = setting {
            *center
        } else {
            (self.receive.start_hz + self.receive.stop_hz) / 2
        };
        let maximum = if self.settings["ultra"] == true {
            6_000_000_000
        } else {
            800_000_000
        };
        let start = center.checked_sub(span / 2);
        let stop = center.checked_add(span - span / 2);
        match (start, stop) {
            (Some(start), Some(stop)) if start >= 100_000 && start <= stop && stop <= maximum => {
                Ok((start, stop))
            }
            _ => Err(InstrumentError::InvalidRequest(
                "center/span outside explicitly selected profile".into(),
            )),
        }
    }
    async fn apply_setting(&mut self, setting: Setting) -> Result<()> {
        let command = setting_command(&setting)?;
        let (key, value) = crate::settings::setting_value(&setting)?;
        let switches_output = matches!(
            setting,
            Setting::OutputHz(_)
                | Setting::OutputDbm(_)
                | Setting::OutputEnabled(true)
                | Setting::OutputDeltaDb(_)
                | Setting::Modulation(_)
                | Setting::ModulationHz(_)
                | Setting::AmDepthPercent(_)
                | Setting::FmDeviationHz(_)
        );
        // Validate every command in a multi-command change before the first byte.
        self.require_command(&command.wire)?;
        if switches_output && self.device_mode != DeviceMode::Output {
            self.require_command("mode")?;
            self.require_command("output")?;
            for wire in ["output off", "mode output", "output off"] {
                self.ack(wire).await?;
            }
        }
        if let Setting::Points(points) = setting {
            self.checked_ack(&format!(
                "sweep {} {} {points}",
                self.receive.start_hz, self.receive.stop_hz
            ))
            .await?;
        } else {
            self.checked_ack(&command.wire).await?;
        }
        match setting {
            Setting::StartHz(hz) => self.receive.start_hz = hz,
            Setting::StopHz(hz) => self.receive.stop_hz = hz,
            Setting::CenterHz(_) | Setting::SpanHz(_) => {
                let (start, stop) = self.updated_grid(&setting)?;
                self.receive.start_hz = start;
                self.receive.stop_hz = stop;
                self.settings["start_hz"] = json!(start);
                self.settings["stop_hz"] = json!(stop);
            }
            Setting::Points(points) => self.receive.points = points,
            Setting::RbwHz(Rbw::Hz(hz)) => self.receive.rbw_hz = hz,
            Setting::OutputHz(_) => self.output_frequency = true,
            Setting::OutputDbm(_) => self.output_level = true,
            _ => {}
        }
        self.settings[key] = value;
        Ok(())
    }
    /// Orderly lab shutdown attempts RF/CAL off only on an untainted connection.
    pub async fn close(&mut self) -> Result<()> {
        if self.connection.closed() {
            return Ok(());
        }
        let off = if !self.connection.tainted() && self.control_mode == ControlMode::Lab {
            self.safe_off().await
        } else {
            Ok(())
        };
        self.invalidate_device_knowledge();
        let closed = self.connection.close().await;
        off.and(closed)
    }
}
pub(crate) fn matches_identity(info: &str, firmware: &str) -> bool {
    let versions: Vec<&str> = firmware.trim().lines().collect();
    info.lines()
        .any(|line| line.trim().eq_ignore_ascii_case("tinySA Ultra ZS405"))
        && (versions == [FIRMWARE] || versions == [FIRMWARE, "HW Version:V0.4.5.1"])
}
/// Exactly Python re.findall(r'[a-z][a-z0-9_]*', text), including a letter
/// following a leading digit/underscore and the lowercase suffix of a word.
fn help_commands(text: &str) -> HashSet<String> {
    let bytes = text.as_bytes();
    let mut index = 0;
    let mut words = HashSet::new();
    while index < bytes.len() {
        if !bytes[index].is_ascii_lowercase() {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        while index < bytes.len()
            && (bytes[index].is_ascii_lowercase()
                || bytes[index].is_ascii_digit()
                || bytes[index] == b'_')
        {
            index += 1;
        }
        words.insert(text[start..index].into());
    }
    words
}
fn validate_receive(config: ReceiveConfig) -> Result<()> {
    if !(100_000..=800_000_000).contains(&config.start_hz)
        || config.stop_hz > 800_000_000
        || config.stop_hz <= config.start_hz
        || !(2..=290).contains(&config.points)
        || !matches!(config.rbw_hz, 10_000 | 30_000)
    {
        return Err(InstrumentError::InvalidRequest(format!(
            "receive request violates {RECEIVE_PROFILE}"
        )));
    }
    Ok(())
}
pub(crate) fn now() -> DateTime<Utc> {
    let time = Utc::now();
    DateTime::from_timestamp_micros(time.timestamp_micros()).expect("current UTC timestamp")
}
fn frequency_token(value: u64) -> String {
    for (scale, suffix) in [(1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")] {
        if value.is_multiple_of(scale) {
            return format!("{}{suffix}", value / scale);
        }
    }
    value.to_string()
}
fn parse_frequency(token: &str) -> Result<u64> {
    if let Ok(value) = token.parse::<u64>()
        && value > 0
    {
        return Ok(value);
    }
    let scale = match token.chars().last() {
        Some('k') => 1000,
        Some('M') => 1_000_000,
        Some('G') => 1_000_000_000,
        _ => return Err(InstrumentError::Protocol("invalid scan frequency".into())),
    };
    let value = token[..token.len() - 1]
        .parse::<f64>()
        .map_err(|_| InstrumentError::Protocol("invalid scan frequency".into()))?
        * scale as f64;
    if !value.is_finite() || value <= 0.0 || value > 20_000_000_000.0 {
        return Err(InstrumentError::Protocol("invalid scan frequency".into()));
    }
    Ok(value.round_ties_even() as u64)
}
fn parse_scan(text: &str, points: u32) -> Result<(Vec<u64>, Vec<f64>)> {
    let mut hz = Vec::new();
    let mut levels = Vec::new();
    for line in text.lines().filter(|line| !line.trim().is_empty()) {
        let line = line.replace(',', " ");
        let tokens: Vec<&str> = line.split_whitespace().collect();
        if tokens.len() < 2 {
            return Err(InstrumentError::Protocol("incomplete scan row".into()));
        }
        let frequency = parse_frequency(tokens[0])?;
        let level = tokens[1]
            .strip_suffix("dBm")
            .unwrap_or(tokens[1])
            .parse::<f64>()
            .map_err(|_| InstrumentError::Protocol("invalid scan level".into()))?;
        if !level.is_finite() || hz.last().is_some_and(|&last| frequency < last) {
            return Err(InstrumentError::Protocol(
                "nonfinite level or nonmonotonic scan grid".into(),
            ));
        }
        hz.push(frequency);
        levels.push(level);
    }
    if hz.len() != points as usize {
        return Err(InstrumentError::Protocol(
            "scan point count differs from request".into(),
        ));
    }
    Ok((hz, levels))
}
/// Exact ea71dfd scanraw codec: little-endian u16/32 minus read-back zero;
/// float32 multiplication of an integer-divided step, with an open stop.
pub fn decode_scanraw(
    raw: &[u8],
    start_hz: u64,
    stop_hz: u64,
    points: u32,
    zero_db: u16,
) -> Result<(Vec<u64>, Vec<f64>)> {
    if !(1..=65536).contains(&points)
        || start_hz > stop_hz
        || stop_hz > 20_000_000_000
        || zero_db > 2047
        || raw.len() != 3 * points as usize + 2
        || raw.first() != Some(&b'{')
        || raw.last() != Some(&b'}')
    {
        return Err(InstrumentError::Protocol(
            "invalid or incomplete scanraw frame/grid".into(),
        ));
    }
    let step = ((stop_hz - start_hz) / points as u64) as f32;
    let mut hz = Vec::with_capacity(points as usize);
    let mut levels = Vec::with_capacity(points as usize);
    for index in 0..points as usize {
        let offset = 1 + 3 * index;
        if raw[offset] != b'x' {
            return Err(InstrumentError::Protocol(
                "invalid scanraw sample marker".into(),
            ));
        }
        hz.push(start_hz + (step * (index as f32)) as u64);
        levels.push(
            u16::from_le_bytes([raw[offset + 1], raw[offset + 2]]) as f64 / 32.0 - zero_db as f64,
        );
    }
    Ok((hz, levels))
}

pub(crate) fn validate_filename(filename: &str) -> Result<()> {
    if filename.is_empty()
        || filename.len() > 64
        || !filename.as_bytes()[0].is_ascii_alphanumeric()
        || filename.contains("..")
        || !filename
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    {
        return Err(InstrumentError::InvalidRequest(
            "bounded SD filename without paths required".into(),
        ));
    }
    Ok(())
}
fn validate_keypad(text: &str) -> Result<()> {
    if text.len() > 21 || !text.is_ascii() {
        return Err(InstrumentError::InvalidRequest(
            "numeric keypad text required".into(),
        ));
    }
    let number = text.strip_prefix('-').unwrap_or(text);
    let number = if number.ends_with(['k', 'M', 'G', 'm', 'u']) {
        &number[..number.len() - 1]
    } else {
        number
    };
    let mut parts = number.split('.');
    let whole = parts.next().unwrap_or("");
    let fraction = parts.next();
    if !(1..=12).contains(&whole.len())
        || !whole.bytes().all(|b| b.is_ascii_digit())
        || parts.next().is_some()
        || fraction
            .is_some_and(|f| f.is_empty() || f.len() > 6 || !f.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(InstrumentError::InvalidRequest(
            "numeric keypad text required".into(),
        ));
    }
    Ok(())
}

fn trace_command(command: &TraceCommand) -> Result<String> {
    let (trace, wire) = match command {
        TraceCommand::Copy {
            trace,
            source_trace,
        }
        | TraceCommand::Subtract {
            trace,
            source_trace,
        } => {
            if !(1..=4).contains(source_trace) {
                return Err(InstrumentError::InvalidRequest(
                    "source trace must be 1..4".into(),
                ));
            }
            (
                *trace,
                format!(
                    "trace {trace} {} {source_trace}",
                    if matches!(command, TraceCommand::Copy { .. }) {
                        "copy"
                    } else {
                        "subtract"
                    }
                ),
            )
        }
        TraceCommand::Freeze { trace, enabled } | TraceCommand::View { trace, enabled } => (
            *trace,
            format!(
                "trace {trace} {} {}",
                if matches!(command, TraceCommand::Freeze { .. }) {
                    "freeze"
                } else {
                    "view"
                },
                if *enabled { "on" } else { "off" }
            ),
        ),
        TraceCommand::Calculation { trace, calculation } => (
            *trace,
            format!(
                "calc {trace} {}",
                serde_json::to_value(calculation).unwrap().as_str().unwrap()
            ),
        ),
    };
    if !(1..=4).contains(&trace) {
        return Err(InstrumentError::InvalidRequest("trace must be 1..4".into()));
    }
    Ok(wire)
}
pub(crate) fn marker_command(command: &MarkerCommand) -> Result<String> {
    let (marker, tail) = match command {
        MarkerCommand::Read { marker } => (*marker, String::new()),
        MarkerCommand::On { marker } => (*marker, " on".into()),
        MarkerCommand::Off { marker } => (*marker, " off".into()),
        MarkerCommand::Peak { marker } => (*marker, " peak".into()),
        MarkerCommand::Frequency {
            marker,
            frequency_hz,
        } => {
            if !(100_000..=6_000_000_000).contains(frequency_hz) {
                return Err(InstrumentError::InvalidRequest(
                    "marker frequency outside profile".into(),
                ));
            }
            (*marker, format!(" {frequency_hz}"))
        }
        MarkerCommand::Delta { marker, reference } | MarkerCommand::Trace { marker, reference } => {
            if !(1..=4).contains(reference) {
                return Err(InstrumentError::InvalidRequest(
                    "marker reference must be 1..4".into(),
                ));
            }
            (
                *marker,
                format!(
                    " {} {reference}",
                    if matches!(command, MarkerCommand::Delta { .. }) {
                        "delta"
                    } else {
                        "trace"
                    }
                ),
            )
        }
        MarkerCommand::Noise { marker, enabled }
        | MarkerCommand::Tracking { marker, enabled }
        | MarkerCommand::TraceAverage { marker, enabled } => {
            let action = match command {
                MarkerCommand::Noise { .. } => "noise",
                MarkerCommand::Tracking { .. } => "tracking",
                _ => "trace_aver",
            };
            (
                *marker,
                format!(" {action} {}", if *enabled { "on" } else { "off" }),
            )
        }
    };
    if !(1..=8).contains(&marker) {
        return Err(InstrumentError::InvalidRequest(
            "marker must be 1..8".into(),
        ));
    }
    Ok(format!("marker {marker}{tail}"))
}
fn validate_generator(request: &GeneratorSweepConfig) -> Result<()> {
    if !request.confirmed
        || request.start_hz < 100_000
        || request.start_hz >= request.stop_hz
        || request.stop_hz > 800_000_000
        || !(2..=450).contains(&request.points)
        || !request.seconds.is_finite()
        || !(0.003..=60.0).contains(&request.seconds)
        || !request.level_dbm.is_finite()
        || !(-115.0..=-19.0).contains(&request.level_dbm)
        || !request.delta_db.is_finite()
        || !(-90.0..=90.0).contains(&request.delta_db)
        || !(-115.0..=-19.0).contains(&(request.level_dbm + request.delta_db))
    {
        return Err(InstrumentError::InvalidRequest(
            "generator sweep requires explicit RF confirmation and bounded profile parameters"
                .into(),
        ));
    }
    Ok(())
}

pub(crate) fn validate_control_request(control: &TinySaControl) -> Result<()> {
    match control {
        TinySaControl::Touch { x, y } if *x >= 480 || *y >= 320 => {
            return Err(InstrumentError::InvalidRequest(
                "coordinates outside Ultra screen".into(),
            ));
        }
        TinySaControl::LoadPreset { slot, confirmed }
        | TinySaControl::SavePreset { slot, confirmed }
            if *slot > 4 || !*confirmed =>
        {
            return Err(InstrumentError::InvalidRequest(
                "preset slot 0..4 and explicit confirmation required".into(),
            ));
        }
        TinySaControl::SelfTest { test, confirmed } if !(1..=3).contains(test) || !*confirmed => {
            return Err(InstrumentError::InvalidRequest(
                "self-test 1..3 and explicit RF confirmation required".into(),
            ));
        }
        TinySaControl::Keypad { text } => validate_keypad(text)?,
        TinySaControl::GeneratorSweep(request) => validate_generator(request)?,
        TinySaControl::Trace(command) => {
            trace_command(command)?;
        }
        TinySaControl::DeleteSd {
            filename,
            confirmed,
        } => {
            validate_filename(filename)?;
            if !confirmed {
                return Err(InstrumentError::InvalidRequest(
                    "explicit SD deletion confirmation required".into(),
                ));
            }
        }
        TinySaControl::Set(setting) => {
            setting_command(setting)?;
        }
        TinySaControl::ApplyReceivePreset(request) => validate_receive_preset(request)?,
        _ => {}
    }
    Ok(())
}

fn validate_receive_preset(request: &ReceivePreset) -> Result<()> {
    if !request.confirmed || request.settings.len() > 29 {
        return Err(InstrumentError::InvalidRequest(
            "explicit receive preset confirmation required".into(),
        ));
    }
    let mut seen = HashSet::new();
    for setting in &request.settings {
        let command = setting_command(setting)?;
        let (key, _) = crate::settings::setting_value(setting)?;
        if command.requires_lab
            || matches!(setting, Setting::CenterHz(_) | Setting::SpanHz(_))
            || !seen.insert(key)
        {
            return Err(InstrumentError::InvalidRequest(
                "receive preset contains forbidden or duplicate setting".into(),
            ));
        }
    }
    Ok(())
}
