//! RF Explorer Standard host protocol. See the vendor's RFExplorer-for-Python
//! RFEConfiguration.ProcessReceivedString, ReceiveSerialThread, and UpdateDeviceConfig.
use crate::{InstrumentError, IoLimits, OperationInterval, Result, SpectrumKind};
use chrono::Utc;
use serde_json::{Value, json};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub(super) const MIN_HZ: u64 = 15_000_000;
pub(super) const MAX_HZ: u64 = 2_700_000_000;
pub(super) const MIN_SPAN_HZ: u64 = 112_000;
pub(super) const MAX_SPAN_HZ: u64 = 600_000_000;
const MODEL: &[u8] = b"#C2-M:003,005,01.34";

fn protocol(message: &str) -> InstrumentError {
    InstrumentError::Protocol(message.into())
}

fn unsigned(field: &str, width: usize) -> Result<u64> {
    if field.len() != width || !field.bytes().all(|b| b.is_ascii_digit()) {
        return Err(protocol("invalid RF Explorer unsigned field"));
    }
    field
        .parse()
        .map_err(|_| protocol("RF Explorer field overflow"))
}

fn signed(field: &str) -> Result<i16> {
    if field.len() != 4 {
        return Err(protocol("invalid RF Explorer signed field width"));
    }
    let digits = field.strip_prefix('-').unwrap_or(field);
    if !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(protocol("invalid RF Explorer signed field"));
    }
    field
        .parse()
        .map_err(|_| protocol("RF Explorer signed field overflow"))
}

#[derive(Clone, Copy, PartialEq)]
pub(super) struct Detector(u8);

impl Detector {
    fn new(code: u64) -> Result<Self> {
        if code <= 5 {
            Ok(Self(code as u8))
        } else {
            Err(protocol("unknown RF Explorer calculator mode"))
        }
    }
    fn name(self) -> &'static str {
        [
            "normal",
            "max",
            "avg",
            "overwrite",
            "max_hold",
            "max_historical",
        ][self.0 as usize]
    }
    pub(super) fn kind(self) -> SpectrumKind {
        if self.0 == 0 {
            SpectrumKind::RawSpectrum
        } else {
            SpectrumKind::ProcessedTrace
        }
    }
}

pub(super) struct Configuration {
    pub(super) start_hz: u64,
    pub(super) step_hz: u64,
    pub(super) points: usize,
    pub(super) offset_db: i16,
    pub(super) detector: Detector,
    expansion: bool,
    min_hz: u64,
    max_hz: u64,
    max_span_hz: u64,
    top_dbm: i16,
    bottom_dbm: i16,
    rbw_hz: u64,
}

impl Configuration {
    fn parse(fields: &str) -> Result<Self> {
        let fields = fields.strip_suffix(',').unwrap_or(fields);
        let parts: Vec<&str> = fields.split(',').collect();
        let [
            start,
            step,
            top,
            bottom,
            points,
            active,
            mode,
            min,
            max,
            span,
            rbw,
            offset,
            calc,
        ] = parts.as_slice()
        else {
            return Err(protocol("incomplete RF Explorer configuration"));
        };
        let expansion = match *active {
            "0" => false,
            "1" => true,
            _ => return Err(protocol("invalid RF Explorer active board")),
        };
        if *mode != "000" {
            return Err(InstrumentError::UnsafeState(
                "RF Explorer requires spectrum analyzer mode".into(),
            ));
        }
        // Bounds on decimal widths make the Hz conversion and bin arithmetic fit u64.
        let step_width = if step.len() == 8 { 8 } else { 7 };
        let config = Self {
            start_hz: unsigned(start, 7)? * 1000,
            step_hz: unsigned(step, step_width)?,
            points: unsigned(points, 4)? as usize,
            expansion,
            min_hz: unsigned(min, 7)? * 1000,
            max_hz: unsigned(max, 7)? * 1000,
            max_span_hz: unsigned(span, 7)? * 1000,
            top_dbm: signed(top)?,
            bottom_dbm: signed(bottom)?,
            rbw_hz: unsigned(rbw, 5)? * 1000,
            offset_db: signed(offset)?,
            detector: Detector::new(unsigned(calc, 3)?)?,
        };
        let (board_min, board_max) = if expansion {
            (MIN_HZ, MAX_HZ)
        } else {
            (240_000_000, 960_000_000)
        };
        // The vendor's Standard sweep-count commands start at 16 points. The
        // minimum 112 kHz span can report 112 bins separated by exactly 1 kHz.
        if !(16..=u8::MAX as usize).contains(&config.points)
            || config.step_hz * (config.points as u64) < MIN_SPAN_HZ
            || config.min_hz < board_min
            || config.max_hz > board_max
            || config.max_hz <= config.min_hz
            || config.max_span_hz == 0
            || config.max_span_hz > MAX_SPAN_HZ.min(config.max_hz - config.min_hz)
            || config.top_dbm <= config.bottom_dbm
            || !(3_000..=600_000).contains(&config.rbw_hz)
            || !config.contains(config.window())
        {
            return Err(protocol(
                "RF Explorer configuration exceeds active analyzer limits",
            ));
        }
        Ok(config)
    }

    pub(super) fn window(&self) -> (u64, u64) {
        (
            self.start_hz,
            self.start_hz + self.step_hz * (self.points as u64 - 1),
        )
    }

    pub(super) fn contains(&self, (start, stop): (u64, u64)) -> bool {
        self.min_hz <= start
            && start < stop
            && stop <= self.max_hz
            && stop - start <= self.max_span_hz
    }

    pub(super) fn same_board(&self, other: &Self) -> bool {
        self.expansion == other.expansion
            && self.min_hz == other.min_hz
            && self.max_hz == other.max_hz
            && self.max_span_hz == other.max_span_hz
    }

    pub(super) fn confirms_tuning(&self, original: &Self, requested: (u64, u64)) -> bool {
        // RBW is selected automatically by the device for the new span.
        self.window() == requested
            && self.same_board(original)
            && self.points == original.points
            && self.top_dbm == original.top_dbm
            && self.bottom_dbm == original.bottom_dbm
            && self.offset_db == original.offset_db
            && self.detector == original.detector
    }

    pub(super) fn tune_command(&self, (start, stop): (u64, u64)) -> Vec<u8> {
        // Only these four fields belong to the Standard C2-F receive command.
        let body = format!(
            "C2-F:{:07},{:07},{:04},{:04}",
            start / 1000,
            stop / 1000,
            self.top_dbm,
            self.bottom_dbm
        );
        let mut frame = vec![b'#', (body.len() + 2) as u8];
        frame.extend_from_slice(body.as_bytes());
        frame
    }

    pub(super) fn settings(&self) -> Value {
        json!({
            "adapter": "rf-explorer-3g-serial",
            "receive_profile": "rf-explorer-3g-combo-standard-1.34",
            "device_identity": {"main_model": 3, "expansion_model": 5, "firmware": "01.34"},
            "device_mode": "spectrum_analyzer",
            "active_board": if self.expansion { "expansion" } else { "main" },
            "active_input": if self.expansion { "right" } else { "left" },
            "board_min_hz": self.min_hz, "board_max_hz": self.max_hz,
            "board_max_span_hz": self.max_span_hz,
            "start_hz": self.start_hz, "step_hz": self.step_hz, "stop_hz": self.window().1,
            "points": self.points, "rbw_hz": self.rbw_hz,
            "amplitude_top_dbm": self.top_dbm, "amplitude_bottom_dbm": self.bottom_dbm,
            "offset_db": self.offset_db, "level_encoding": "dBm = offset_db - u8 / 2",
            "detector": self.detector.name(), "detector_code": self.detector.0,
            "artifact_kind": self.detector.kind(), "settings_source": "device_readback",
            "settings_readback": true, "timing_source": "host_frame_read", "simulated": false
        })
    }
}

pub(super) enum Frame {
    Model,
    Configuration(Configuration),
    Sweep(Vec<u8>, OperationInterval),
    Detector(Detector),
    Reset,
    Auxiliary,
}

async fn read_crlf(io: &mut (impl AsyncRead + Unpin)) -> Result<()> {
    let mut terminal = [0; 2];
    io.read_exact(&mut terminal).await?;
    if terminal != *b"\r\n" {
        return Err(protocol("missing RF Explorer binary frame terminator"));
    }
    Ok(())
}

async fn read_binary(
    io: &mut (impl AsyncRead + Unpin),
    limit: usize,
    started_at: chrono::DateTime<Utc>,
) -> Result<Frame> {
    let kind = io.read_u8().await?;
    let length = match kind {
        b'S' | b'q' => usize::from(io.read_u8().await?),
        b'Q' => usize::from(io.read_u16_le().await?),
        b'C' => match io.read_u8().await? {
            b'c' | b'd' => usize::from(io.read_u8().await?),
            b'b' => {
                let _address = io.read_u8().await?;
                let blocks = usize::from(io.read_u8().await?) + 1;
                5 + blocks * 16 // five remaining header bytes, then payload
            }
            _ => return Err(protocol("unknown RF Explorer calibration framing")),
        },
        _ => return Err(protocol("unsupported RF Explorer binary framing")),
    };
    if length > limit {
        return Err(protocol(
            "RF Explorer binary frame exceeds configured limit",
        ));
    }
    if kind == b'S' {
        let mut counts = vec![0; length];
        io.read_exact(&mut counts).await?;
        read_crlf(io).await?;
        // FW 01.34 can abort a partial sweep with the vendor EEOT sequence.
        // A following record can supply the expected length and CRLF by chance.
        const EARLY_END: &[u8] = b"\xff\xfe\xff\xfe\x00";
        if counts
            .windows(EARLY_END.len())
            .any(|bytes| bytes == EARLY_END)
        {
            return Err(protocol("RF Explorer interrupted the sweep"));
        }
        let ended_at = Utc::now();
        if ended_at < started_at {
            return Err(protocol(
                "host UTC regressed while reading RF Explorer sweep",
            ));
        }
        return Ok(Frame::Sweep(
            counts,
            OperationInterval {
                started_at,
                ended_at,
            },
        ));
    }
    // Calibration can contain any binary data, including CRLF and valid-looking headers.
    let mut discarded = [0; 256];
    let mut remaining = length;
    while remaining > 0 {
        let take = remaining.min(discarded.len());
        io.read_exact(&mut discarded[..take]).await?;
        remaining -= take;
    }
    read_crlf(io).await?;
    Ok(Frame::Auxiliary)
}

fn text_frame(line: &[u8]) -> Result<Frame> {
    // Only printable ASCII was admitted by the line reader.
    let text = std::str::from_utf8(line).expect("validated ASCII");
    if line == MODEL {
        Ok(Frame::Model)
    } else if let Some(body) = text.strip_prefix("#C2-F:") {
        Configuration::parse(body).map(Frame::Configuration)
    } else if let Some(code) = text.strip_prefix("#C+") {
        Detector::new(unsigned(code, 1)?).map(Frame::Detector)
    } else if text.starts_with("#C2-M") || text.starts_with("#C3") {
        Err(InstrumentError::UnsupportedProfile(
            "expected 3G Combo Standard 003/005 firmware 01.34".into(),
        ))
    } else if line.starts_with(b"#C") && line.get(2).is_some_and(u8::is_ascii_digit) {
        Err(protocol("unsupported RF Explorer configuration format"))
    } else if text.starts_with("#a") && text != "#a0" {
        Err(protocol("unsupported RF Explorer input stage"))
    } else if text.starts_with("(C) Ariel Rocholl ") {
        Ok(Frame::Reset)
    } else {
        // Printable banner, serial number, calibration flags and status notifications.
        Ok(Frame::Auxiliary)
    }
}

pub(super) async fn next_frame(
    io: &mut (impl AsyncRead + Unpin),
    limits: &IoLimits,
) -> Result<Frame> {
    let started_at = Utc::now();
    let mut byte = io.read_u8().await?;
    if byte == b'$' {
        return read_binary(io, limits.max_binary_bytes, started_at).await;
    }
    let mut line = Vec::new();
    while byte != b'\r' {
        if !(0x20..=0x7e).contains(&byte) || line.len() >= limits.max_text_bytes.min(512) {
            return Err(protocol("non-ASCII or oversized RF Explorer text frame"));
        }
        line.push(byte);
        byte = io.read_u8().await?;
    }
    if io.read_u8().await? != b'\n' {
        return Err(protocol("missing RF Explorer text frame terminator"));
    }
    text_frame(&line)
}

pub(super) async fn send(
    io: &mut (impl AsyncWrite + Unpin),
    frame: &[u8],
    limits: &IoLimits,
) -> Result<()> {
    tokio::time::timeout(limits.write_timeout, async {
        io.write_all(frame).await?;
        io.flush().await
    })
    .await
    .map_err(|_| InstrumentError::Timeout)??;
    Ok(())
}
