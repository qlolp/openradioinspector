//! Opt-in USB reference for the release bench. No live core may own this port.
#[cfg(feature = "serial")]
#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    use ori_guard::{ScanBand, ScanPlan, ScanWindow};
    use ori_instruments::{IoLimits, ReceiveConfig, TinySa, serial};
    use serde_json::{Value, json};
    use std::{fs, io::Write, time::Instant};

    let args: Vec<_> = std::env::args().skip(1).collect();
    let [port, band_path, baud, passes, destination] = args.as_slice() else {
        return Err("usage: scan_reference PORT BAND.json BAUD PASSES NEW-REFERENCE.json".into());
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
    let receive = |w: ScanWindow| ReceiveConfig {
        start_hz: w.start_hz,
        stop_hz: w.stop_hz,
        points: w.points,
        rbw_hz: w.rbw_hz,
    };
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(destination)?;
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
    let measured = async {
        instrument.prepare().await?;
        let windows = band.window_count();
        let anchor = passes * windows;
        let mut firmware = Value::Null;
        let mut first = None;
        let start = Instant::now();
        let mut elapsed = std::time::Duration::ZERO;
        for n in 0..=anchor {
            if n == anchor { elapsed = start.elapsed(); }
            let w = band.window(n % windows).ok_or("missing grid window")?;
            let sample = instrument.poll_grid(receive(w)).await?;
            if sample.frequencies_hz.len() != w.points as usize || sample.levels.len() != sample.frequencies_hz.len() {
                return Err("incomplete direct window".into());
            }
            if n == 0 {
                first = Some(sample.interval.started_at);
                firmware = sample.settings["device_identity"]["firmware"].clone();
            }
            if n == anchor {
                let utc_span = (sample.interval.started_at - first.ok_or("missing first point")?)
                    .num_microseconds().ok_or("invalid reference time")?;
                if utc_span <= 0 || firmware.as_str().is_none_or(str::is_empty) || elapsed.is_zero() {
                    return Err("missing reference timing or identity".into());
                }
                return Ok::<_, Box<dyn std::error::Error>>(json!({
                    "format":"ori-direct-scan-v1","band":band,"passes":passes,"baud":baud,
                    "firmware":firmware,"elapsed_seconds":elapsed.as_secs_f64(),"utc_elapsed_us":utc_span,
                    "points":u64::from(band.points)*u64::from(passes),"windows":anchor,
                }));
            }
        }
        Err("missing reference boundary".into())
    }.await;
    let closed = instrument.close().await;
    let result = measured?;
    closed?;
    serde_json::to_writer_pretty(&mut file, &result)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

#[cfg(not(feature = "serial"))]
fn main() {
    eprintln!("scan_reference requires the serial feature");
    std::process::exit(2);
}
