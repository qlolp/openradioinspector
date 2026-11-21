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

/// Header bytes after the leading '$'. None means another header byte is required.
fn binary_length(header: &[u8]) -> Result<Option<usize>> {
    match header {
        [b'S' | b'q', len] | [b'C', b'c' | b'd', len] => Ok(Some(usize::from(*len))),
        [b'Q', lo, hi] => Ok(Some(usize::from(u16::from_le_bytes([*lo, *hi])))),
        [b'C', b'b', _address, blocks] => Ok(Some(5 + (usize::from(*blocks) + 1) * 16)),
        [b'S' | b'q' | b'Q' | b'C'] | [b'Q', _] | [b'C', b'b' | b'c' | b'd'] | [b'C', b'b', _] => {
            Ok(None)
        }
        _ => Err(protocol("unsupported RF Explorer binary framing")),
    }
}

async fn read_binary(
    io: &mut (impl AsyncRead + Unpin),
    limit: usize,
    started_at: chrono::DateTime<Utc>,
) -> Result<Frame> {
    let mut header = [0u8; 4];
    let mut received = 0;
    let length = loop {
        header[received] = io.read_u8().await?;
        received += 1;
        if let Some(length) = binary_length(&header[..received])? {
            break length;
        }
    };
    if length > limit {
        return Err(protocol(
            "RF Explorer binary frame exceeds configured limit",
        ));
    }
    if header[0] == b'S' {
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
    } else if text.starts_with("(C) Ariel Rocholl ") || text.starts_with("RF Explorer ") {
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

// C0 can arrive while the Standard is already pushing sweeps. Only during
// startup, retain a bounded prefix so a misleading binary header at open can
// be checked by its declared length and CRLF without losing a later banner.
const MAX_STARTUP_BYTES: usize = 8 * 1024;

struct StartupPrefix<'a, T> {
    io: &'a mut T,
    bytes: Vec<u8>,
}

impl<T: AsyncRead + Unpin> StartupPrefix<'_, T> {
    async fn fill(&mut self, end: usize) -> Result<()> {
        if end > MAX_STARTUP_BYTES {
            return Err(protocol(
                "RF Explorer C0 banner exceeds startup byte budget",
            ));
        }
        let len = self.bytes.len();
        if end > len {
            self.bytes.resize(end, 0);
            self.io.read_exact(&mut self.bytes[len..]).await?;
        }
        Ok(())
    }

    async fn line_end(&mut self, start: usize) -> Result<usize> {
        let mut end = start + 2;
        loop {
            self.fill(end).await?;
            if self.bytes[end - 2..end] == *b"\r\n" {
                return Ok(end);
            }
            end += 1;
        }
    }

    /// Skip entire documented binary records, including arbitrary banner-like
    /// bytes inside them. A false header in an orphaned payload can have the
    /// wrong terminator. Its bytes stay available for locating the true boundary.
    async fn binary_end(&mut self, start: usize, limit: usize) -> Result<Option<usize>> {
        let mut header_end = start + 2;
        let length = loop {
            self.fill(header_end).await?;
            let header = &self.bytes[start + 1..header_end];
            // Other vendor modes can leave extended sweeps or a 128x64 screen
            // dump queued. Skip them before C0; the live profile accepts only $S.
            let size = match header {
                [b's' | b'z'] | [b'z', _] => Ok(None),
                [b's', blocks] => Ok(Some(
                    16 * if *blocks == 0 {
                        256
                    } else {
                        usize::from(*blocks)
                    },
                )),
                [b'z', hi, lo] => Ok(Some(usize::from(u16::from_be_bytes([*hi, *lo])))),
                [b'D'] => Ok(Some(128 * 8)),
                _ => binary_length(header),
            };
            match size {
                Ok(Some(length)) => break length,
                Ok(None) => header_end += 1,
                Err(_) => return Ok(None),
            }
        };
        if length > limit {
            return Err(protocol(
                "RF Explorer startup binary frame exceeds configured limit",
            ));
        }
        let end = header_end + length + 2;
        self.fill(end).await?;
        Ok((self.bytes[end - 2..end] == *b"\r\n").then_some(end))
    }
}

fn dotted_pairs(text: &str, count: usize) -> bool {
    text.len() == count * 3 - 1
        && text
            .split('.')
            .all(|pair| pair.len() == 2 && pair.bytes().all(|b| b.is_ascii_digit()))
}

fn is_startup_banner(line: &[u8]) -> Result<bool> {
    let Some(fields) = line.strip_prefix(b"RF Explorer ") else {
        return Ok(false);
    };
    let Ok(fields) = std::str::from_utf8(fields) else {
        return Ok(false);
    };
    let mut fields = fields.split(' ');
    let (Some(firmware), Some(date), Some(build), None) =
        (fields.next(), fields.next(), fields.next(), fields.next())
    else {
        return Ok(false);
    };
    if !dotted_pairs(firmware, 2)
        || date.len() != 9
        || chrono::NaiveDate::parse_from_str(date, "%d-%b-%y").is_err()
        || !dotted_pairs(build, 3)
    {
        return Ok(false);
    }
    if firmware != "01.34" {
        return Err(InstrumentError::UnsupportedProfile(
            "expected RF Explorer Standard firmware 01.34 in C0 banner".into(),
        ));
    }
    Ok(true)
}

pub(super) async fn synchronize(
    io: &mut (impl AsyncRead + Unpin),
    limits: &IoLimits,
) -> Result<Vec<u8>> {
    let mut prefix = StartupPrefix {
        io,
        bytes: Vec::new(),
    };
    let mut start = 0;
    loop {
        prefix.fill(start + 1).await?;
        if start == 0 && prefix.bytes[0] == b'\n' {
            // The port can open on the second byte of a preceding CRLF.
            start = 1;
            continue;
        }
        if prefix.bytes[start] == b'$'
            && let Some(end) = prefix.binary_end(start, limits.max_binary_bytes).await?
        {
            start = end;
            continue;
        }
        let end = prefix.line_end(start).await?;
        if is_startup_banner(&prefix.bytes[start..end - 2])? {
            return Ok(prefix.bytes.split_off(end));
        }
        start = end;
    }
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
