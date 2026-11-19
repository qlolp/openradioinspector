//! Opt-in USB reference for the release bench. No live core may own this port.
#[cfg(feature = "serial")]
mod reference {
    use chrono::{DateTime, Utc};
    use ori_guard::{ScanBand, ScanPlan, ScanWindow};
    use ori_instruments::{IoLimits, ReceiveConfig, SpectrumKind, SpectrumSample, TinySa, serial};
    use serde_json::{Value, json};
    use std::{
        collections::BTreeMap,
        error::Error,
        fs,
        io::Write,
        path::Path,
        time::{Duration, Instant},
    };
    use tokio::io::{AsyncRead, AsyncWrite};

    type Result<T> = std::result::Result<T, Box<dyn Error>>;

    fn receive(w: ScanWindow) -> ReceiveConfig {
        ReceiveConfig {
            start_hz: w.start_hz,
            stop_hz: w.stop_hz,
            points: w.points,
            rbw_hz: w.rbw_hz,
        }
    }

    /// Keep only the recognized model line, firmware and opaque unit identifier.
    /// The adapter also verifies the exact revision before acknowledging wide RX.
    #[derive(PartialEq)]
    struct Identity {
        model: String,
        firmware: String,
        unit: String,
    }

    #[derive(Default)]
    struct ReceivePaths {
        identity: Option<Identity>,
        paths: BTreeMap<&'static str, Value>,
    }

    impl ReceivePaths {
        fn record(&mut self, sample: &SpectrumSample, window: ScanWindow) -> Result<()> {
            if sample.kind != SpectrumKind::RawSpectrum
                || sample.level_unit != "dBm"
                || sample.levels.len() != window.points as usize
                || sample.frequencies_hz.len() != sample.levels.len()
                || sample.levels.iter().any(|v| !v.is_finite())
                || sample
                    .frequencies_hz
                    .iter()
                    .enumerate()
                    .any(|(i, hz)| *hz != window.start_hz + i as u64 * 10_000)
            {
                return Err("incomplete direct raw window".into());
            }
            let settings = &sample.settings;
            let evidence = &settings["scan_rx_evidence"];
            let unit = evidence["device_id_sha256"]
                .as_str()
                .ok_or("missing unit digest")?;
            let firmware = settings["device_identity"]["firmware"]
                .as_str()
                .ok_or("missing firmware")?;
            let model = settings["device_identity"]["info"]
                .as_str()
                .and_then(|info| {
                    info.lines()
                        .map(str::trim)
                        .find(|line| line.eq_ignore_ascii_case("tinySA Ultra ZS405"))
                })
                .ok_or("missing ZS405 model")?;
            let identity = Identity {
                model: model.into(),
                firmware: firmware.into(),
                unit: unit.into(),
            };
            if self
                .identity
                .as_ref()
                .is_some_and(|original| original != &identity)
            {
                return Err("hardware identity changed during reference".into());
            }
            self.identity.get_or_insert(identity);
            let ultra = evidence["ultra"].as_bool().ok_or("missing RX path")?;
            if ultra != (window.stop_hz > 800_000_000)
                || evidence["source"] != "command_acknowledged"
                || evidence["device_mode"] != "input"
                || evidence["rbw_hz"] != window.rbw_hz
            {
                return Err("direct receive settings differ from grid request".into());
            }
            let path = self
                .paths
                .entry(if ultra { "ultra" } else { "normal" })
                .or_insert_with(|| evidence.clone());
            if path != evidence {
                return Err("acknowledged settings changed during reference".into());
            }
            Ok(())
        }
    }

    /// Both reported durations use starts of actual instrument reads. A boundary
    /// poll may switch the Ultra input before the adapter timestamps that read.
    fn sample_start(call: (Instant, DateTime<Utc>), started: DateTime<Utc>) -> Result<Instant> {
        let micros = (started - call.1)
            .num_microseconds()
            .ok_or("invalid poll time")?;
        let offset = Duration::from_micros(micros.try_into()?);
        call.0
            .checked_add(offset)
            .filter(|at| *at <= Instant::now())
            .ok_or_else(|| "reference clocks disagree within a boundary poll".into())
    }

    async fn measure<T: AsyncRead + AsyncWrite + Unpin + Send>(
        instrument: &mut TinySa<T>,
        band: &ScanBand,
        baud: u32,
        passes: u32,
    ) -> Result<Value> {
        instrument.prepare().await?;
        let windows = band.window_count();
        let anchor = passes * windows;
        let mut first = None;
        let mut paths = ReceivePaths::default();
        let mut points = 0u64;
        for n in 0..=anchor {
            let w = band.window(n % windows).ok_or("missing grid window")?;
            let call = (n == 0 || n == anchor).then(|| (Instant::now(), Utc::now()));
            let sample = instrument.poll_grid(receive(w)).await?;
            paths.record(&sample, w)?;
            if let Some(call) = call {
                let utc = sample.interval.started_at;
                let mono = sample_start(call, utc)?;
                if let Some((first_mono, first_utc)) = first {
                    let elapsed = mono
                        .checked_duration_since(first_mono)
                        .ok_or("invalid monotonic interval")?;
                    let utc_span = utc
                        .signed_duration_since(first_utc)
                        .num_microseconds()
                        .ok_or("invalid UTC interval")?;
                    if utc_span <= 0
                        || elapsed.is_zero()
                        || points != u64::from(band.points) * u64::from(passes)
                    {
                        return Err("incomplete direct pass or timing".into());
                    }
                    let identity = paths.identity.ok_or("missing reference identity")?;
                    return Ok(json!({
                        "format":"ori-direct-scan-v2", "band":band, "passes":passes, "baud":baud,
                        "model":identity.model, "firmware":identity.firmware, "receive_paths":paths.paths,
                        "elapsed_seconds":elapsed.as_secs_f64(), "utc_elapsed_us":utc_span,
                        "points":points, "windows":anchor,
                    }));
                }
                first = Some((mono, utc));
            }
            points += sample.levels.len() as u64;
        }
        Err("missing reference boundary".into())
    }

    pub async fn run() -> Result<()> {
        let args: Vec<_> = std::env::args().skip(1).collect();
        let [port, band_path, baud, passes, destination] = args.as_slice() else {
            return Err(
                "usage: scan_reference PORT BAND.json BAUD PASSES NEW-REFERENCE.json".into(),
            );
        };
        if fs::metadata(band_path)?.len() > 16384 {
            return Err("oversize band configuration".into());
        }
        let band: ScanBand = serde_json::from_slice(&fs::read(band_path)?)?;
        let passes: u32 = passes.parse()?;
        let baud: u32 = baud.parse()?;
        if !(1..=20).contains(&passes)
            || band.step_hz != Some(10_000)
            || !(9600..=115200).contains(&baud)
        {
            return Err("expected 1..20 passes, 10 kHz grid and supported serial baud".into());
        }
        ScanPlan {
            name: "Isolated receive reference".into(),
            bands: vec![band.clone()],
        }
        .validate(1_300_000_000)?;
        let destination = Path::new(destination);
        if destination.try_exists()? {
            return Err("reference destination already exists".into());
        }
        let directory = destination
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        let mut file = tempfile::NamedTempFile::new_in(directory)?;
        let transport = serial::open(&serial::SerialOptions {
            path: port.clone(),
            baud,
        })?;
        let mut instrument = TinySa::new_for_grid(
            transport,
            receive(band.window(0).ok_or("empty grid")?),
            IoLimits::default(),
            band.stop_hz > 800_000_000,
        )?;
        let measured = measure(&mut instrument, &band, baud, passes).await;
        let closed = instrument.close().await;
        let result = measured?;
        closed?;
        serde_json::to_writer_pretty(&mut file, &result)?;
        file.write_all(b"\n")?;
        file.as_file().sync_all()?;
        file.persist_noclobber(destination)?;
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

        #[tokio::test]
        async fn normal_and_ultra_passes_record_the_observed_paths_and_hide_the_device_id() {
            let (host, peer) = tokio::io::duplex(16384);
            let target = tokio::spawn(async move {
                let mut peer = BufReader::new(peer);
                loop {
                    let mut request = Vec::new();
                    if peer.read_until(b'\r', &mut request).await.unwrap() == 0 {
                        break;
                    }
                    let command = std::str::from_utf8(&request).unwrap().trim_end();
                    let body = match command {
                        "info" => "tinySA Ultra ZS405\r\n".to_owned(),
                        "version" => "tinySA4_v1.4-235-gea71dfd\r\nHW Version:V0.4.5.1\r\n".into(),
                        "deviceid" => "separate-test-unit\r\n".into(),
                        "scan 797110k 799990k 289 3" => (0..289)
                            .map(|i| format!("{} -45.5\r\n", 797_110_000 + i * 10_000))
                            .collect(),
                        "scan 800M 800010k 2 3" => "800000000 -73\r\n800010000 -81.25\r\n".into(),
                        "ultra on" | "ultra off" | "output off" | "mode input" | "lna off"
                        | "attenuate auto" | "rbw 30" | "calc off" => String::new(),
                        other => panic!("unexpected reference command {other}"),
                    };
                    peer.get_mut()
                        .write_all(format!("{command}\r\n{body}ch> ").as_bytes())
                        .await
                        .unwrap();
                }
            });
            let band: ScanBand = serde_json::from_value(json!({"id":"crossing", "name":"fixture",
                "start_hz":797110000, "stop_hz":800010000, "step_hz":10000, "points":291,
                "rbw_hz":30000, "max_revisit_seconds":10}))
            .unwrap();
            let mut instrument = TinySa::new_for_grid(
                host,
                receive(band.window(0).unwrap()),
                IoLimits::default(),
                true,
            )
            .unwrap();
            let record = measure(&mut instrument, &band, 115200, 2).await.unwrap();
            drop(instrument);
            target.await.unwrap();
            assert_eq!(record["format"], "ori-direct-scan-v2");
            assert_eq!(record["points"], 582);
            assert_eq!(record["windows"], 4);
            assert_eq!(record["receive_paths"]["ultra"]["ultra"], true);
            assert_eq!(record["receive_paths"]["normal"]["ultra"], false);
            let normal = &record["receive_paths"]["normal"];
            assert_eq!(normal["device_id_sha256"].as_str().unwrap().len(), 64);
            assert_eq!(
                normal["device_id_sha256"],
                record["receive_paths"]["ultra"]["device_id_sha256"]
            );
            assert!(!record.to_string().contains("separate-test-unit"));
            assert!(record["elapsed_seconds"].as_f64().unwrap() > 0.);
            assert!(record["utc_elapsed_us"].as_i64().unwrap() > 0);
        }
    }
}

#[cfg(feature = "serial")]
#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    reference::run().await
}

#[cfg(not(feature = "serial"))]
fn main() {
    eprintln!("scan_reference requires the serial feature");
    std::process::exit(2);
}
