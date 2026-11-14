//! Explicit synthetic adapters. The random stream follows Python's integer
//! seeded MT19937 so the existing repeatable simulator profiles stay useful.
use super::{DeviceError, DeviceResult};
use ori_contracts::{DeviceRole, DeviceStream, JSONObject};
use serde_json::{Value, json};
use std::time::Duration;

pub(super) fn integer(
    settings: &JSONObject,
    key: &str,
    default: u64,
    low: u64,
    high: u64,
) -> DeviceResult<u64> {
    let value = match settings.get(key) {
        None => default,
        Some(value) => value
            .as_u64()
            .ok_or_else(|| DeviceError::Invalid(format!("{key} должно быть целым числом")))?,
    };
    if !(low..=high).contains(&value) {
        return Err(DeviceError::Invalid(format!(
            "{key} должно быть в диапазоне {low}…{high}"
        )));
    }
    Ok(value)
}
pub(super) fn finite(settings: &JSONObject, key: &str, default: f64) -> DeviceResult<f64> {
    let value = match settings.get(key) {
        None => default,
        Some(Value::Bool(value)) => u8::from(*value) as f64,
        Some(Value::String(value)) => value
            .trim()
            .parse()
            .map_err(|_| DeviceError::Invalid(format!("{key} должно быть числом")))?,
        Some(value) => value
            .as_f64()
            .ok_or_else(|| DeviceError::Invalid(format!("{key} должно быть числом")))?,
    };
    if !value.is_finite() {
        return Err(DeviceError::Invalid(format!(
            "{key} должно быть конечным числом"
        )));
    }
    Ok(value)
}
pub(super) fn period(settings: &JSONObject) -> DeviceResult<Duration> {
    let period = finite(settings, "period_s", 0.25)?;
    if !(0.0..=3600.0).contains(&period) {
        return Err(DeviceError::Invalid(
            "period_s должно быть в диапазоне 0…3600".into(),
        ));
    }
    Ok(Duration::from_secs_f64(period))
}
pub(super) enum Simulated {
    Analyzer(Analyzer),
    Receiver(Receiver),
}
impl Simulated {
    pub fn new(stream: &DeviceStream) -> DeviceResult<Self> {
        let settings = stream.settings.clone().unwrap_or_default();
        match settings.get("adapter").and_then(Value::as_str) {
            Some("tinysa-sim") => {
                if stream.role != DeviceRole::SpectrumAnalyzer || stream.level_unit != "dBm" {
                    return Err(DeviceError::Invalid(
                        "tinysa-sim требует роль spectrum_analyzer и исходную единицу dBm".into(),
                    ));
                }
                Ok(Self::Analyzer(Analyzer::new(&settings)?))
            }
            Some("ardv1-sim") => {
                if stream.role != DeviceRole::Receiver || stream.level_unit != "s_meter_count" {
                    return Err(DeviceError::Invalid(
                        "ardv1-sim требует роль receiver и исходную единицу s_meter_count".into(),
                    ));
                }
                Ok(Self::Receiver(Receiver::new(&settings)?))
            }
            _ => Err(DeviceError::Unsupported(
                "Требуется явный поддерживаемый адаптер".into(),
            )),
        }
    }
    pub fn details(&self) -> Value {
        match self {
            Self::Analyzer(value) => value.details(),
            Self::Receiver(value) => value.details(),
        }
    }
    pub fn poll(&mut self, stream: &DeviceStream) -> DeviceResult<Value> {
        let started = crate::utc_now();
        match self {
            Self::Analyzer(value) => {
                let (frequencies, levels) = value.poll()?;
                Ok(
                    json!({"id":crate::new_id(),"session_id":stream.session_id,"stream_id":stream.id,"interval":{"started_at":started,"ended_at":crate::utc_now()},"frequencies_hz":frequencies,"levels":levels,"level_unit":"dBm","settings":{"adapter":"tinysa-sim","rbw_hz":value.rbw,"sequence":value.sequence,"simulated":true,"generator_output":"off"}}),
                )
            }
            Self::Receiver(value) => {
                let level = value.poll()?;
                let decoded = if matches!(
                    value.mode.as_str(),
                    "DMR" | "DPMR" | "NXDN" | "P25" | "DSTAR" | "YSF"
                ) {
                    Some(value.mode.clone())
                } else {
                    None
                };
                Ok(
                    json!({"id":crate::new_id(),"session_id":stream.session_id,"stream_id":stream.id,"interval":{"started_at":started,"ended_at":crate::utc_now()},"frequency_hz":value.frequency,"mode":value.mode,"level":level,"level_unit":"s_meter_count","squelch_open":level>=value.squelch,"decoded_mode":decoded,"metadata":{"adapter":"ardv1-sim","sequence":value.sequence,"tune_sequence":value.tunes,"simulated":true}}),
                )
            }
        }
    }
    pub fn tune(&mut self, frequency: u64, mode: Option<&str>) -> DeviceResult<Value> {
        let Self::Receiver(value) = self else {
            return Err(DeviceError::Worker(
                "Перестройка доступна только потоку receiver".into(),
            ));
        };
        if !(1..=20_000_000_000).contains(&frequency) {
            return Err(DeviceError::Invalid(
                "frequency_hz должно быть в диапазоне 1…20000000000".into(),
            ));
        }
        let mode = mode
            .filter(|v| !v.is_empty())
            .unwrap_or(&value.mode)
            .trim()
            .to_uppercase();
        if mode.is_empty() {
            return Err(DeviceError::Invalid("mode не должно быть пустым".into()));
        }
        value.frequency = frequency;
        value.mode = mode;
        value.tunes += 1;
        Ok(value.details())
    }
}
pub(super) struct Analyzer {
    start: u64,
    stop: u64,
    points: u32,
    rbw: u64,
    noise: f64,
    sigma: f64,
    emitters: Vec<(u64, u64, f64)>,
    random: PythonRandom,
    sequence: u64,
    failures: u64,
}
impl Analyzer {
    fn new(settings: &JSONObject) -> DeviceResult<Self> {
        let start = integer(settings, "start_hz", 88_000_000, 1, 20_000_000_000)?;
        let stop = integer(settings, "stop_hz", 108_000_000, 2, 20_000_000_000)?;
        if stop <= start {
            return Err(DeviceError::Invalid(
                "stop_hz должно быть больше start_hz".into(),
            ));
        }
        let sigma = finite(settings, "noise_sigma_db", 1.5)?;
        if sigma < 0.0 {
            return Err(DeviceError::Invalid(
                "noise_sigma_db не может быть отрицательным".into(),
            ));
        }
        let defaults = json!([{"frequency_hz":102_500_000,"bandwidth_hz":200_000,"level_dbm":-42.0},{"frequency_hz":104_300_000,"bandwidth_hz":25_000,"level_dbm":-55.0}]);
        let emitters = settings
            .get("emitters")
            .filter(|v| !v.is_null())
            .unwrap_or(&defaults)
            .as_array()
            .ok_or_else(|| DeviceError::Invalid("emitters должно быть массивом".into()))?
            .iter()
            .map(|item| {
                let object = item
                    .as_object()
                    .ok_or_else(|| {
                        DeviceError::Invalid("Каждый emitter должен быть объектом".into())
                    })?
                    .iter()
                    .map(|(key, value)| (key.clone(), value.clone()))
                    .collect::<JSONObject>();
                if ["frequency_hz", "bandwidth_hz", "level_dbm"]
                    .iter()
                    .any(|key| !object.contains_key(*key))
                {
                    return Err(DeviceError::Invalid(
                        "Emitter требует frequency_hz, bandwidth_hz и level_dbm".into(),
                    ));
                }
                Ok((
                    integer(&object, "frequency_hz", 0, 1, 20_000_000_000)?,
                    integer(&object, "bandwidth_hz", 0, 1, 20_000_000_000)?,
                    finite(&object, "level_dbm", 0.0)?,
                ))
            })
            .collect::<DeviceResult<_>>()?;
        Ok(Self {
            start,
            stop,
            points: integer(settings, "points", 201, 2, 4096)? as u32,
            rbw: integer(settings, "rbw_hz", 10_000, 1, 1_000_000_000)?,
            noise: finite(settings, "noise_dbm", -100.0)?,
            sigma,
            emitters,
            random: PythonRandom::new(integer(settings, "seed", 7, 0, 2_147_483_647)? as u32),
            sequence: 0,
            failures: integer(settings, "failures_remaining", 0, 0, 10_000)?,
        })
    }
    fn details(&self) -> Value {
        json!({"start_hz":self.start,"stop_hz":self.stop,"points":self.points,"generator_output":"off"})
    }
    fn poll(&mut self) -> DeviceResult<(Vec<u64>, Vec<f64>)> {
        fail(&mut self.failures, "tinysa-sim")?;
        let frequencies = (0..self.points)
            .map(|i| {
                self.start
                    + ((self.stop - self.start) as f64 * i as f64 / (self.points - 1) as f64)
                        .round_ties_even() as u64
            })
            .collect::<Vec<_>>();
        let mut levels = frequencies
            .iter()
            .map(|_| self.noise + self.random.gauss() * self.sigma)
            .collect::<Vec<_>>();
        for (center, width, peak) in &self.emitters {
            let half = (*width as f64 / 2.0).max(1.0);
            for (i, frequency) in frequencies.iter().enumerate() {
                let distance = frequency.abs_diff(*center) as f64;
                if distance <= half {
                    let profile = peak - 30.0 * (distance / half).powi(2);
                    if profile > levels[i] {
                        levels[i] = profile;
                    }
                }
            }
        }
        self.sequence += 1;
        Ok((frequencies, levels))
    }
}
pub(super) struct Receiver {
    frequency: u64,
    mode: String,
    squelch: u32,
    random: PythonRandom,
    sequence: u64,
    tunes: u64,
    failures: u64,
}
impl Receiver {
    fn new(settings: &JSONObject) -> DeviceResult<Self> {
        let mode = settings
            .get("mode")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
            .unwrap_or("FM")
            .trim()
            .to_uppercase();
        if mode.is_empty() {
            return Err(DeviceError::Invalid("mode не должно быть пустым".into()));
        }
        Ok(Self {
            frequency: integer(settings, "frequency_hz", 145_500_000, 1, 20_000_000_000)?,
            mode,
            squelch: integer(settings, "squelch", 10, 0, 255)? as u32,
            random: PythonRandom::new(integer(settings, "seed", 17, 0, 2_147_483_647)? as u32),
            sequence: 0,
            tunes: 0,
            failures: integer(settings, "failures_remaining", 0, 0, 10_000)?,
        })
    }
    fn details(&self) -> Value {
        json!({"frequency_hz":self.frequency,"mode":self.mode,"squelch":self.squelch,"tune_sequence":self.tunes})
    }
    fn poll(&mut self) -> DeviceResult<u32> {
        fail(&mut self.failures, "ardv1-sim")?;
        let level = self.random.receiver_count();
        self.sequence += 1;
        Ok(level)
    }
}
fn fail(remaining: &mut u64, adapter: &str) -> DeviceResult<()> {
    if *remaining == 0 {
        return Ok(());
    }
    *remaining -= 1;
    Err(DeviceError::Worker(format!(
        "Смоделированный сбой {adapter}; осталось сбоев: {remaining}"
    )))
}

struct PythonRandom {
    state: [u32; 624],
    index: usize,
    gaussian: Option<f64>,
}
impl PythonRandom {
    fn new(seed: u32) -> Self {
        let mut value = Self {
            state: [0; 624],
            index: 624,
            gaussian: None,
        };
        value.state[0] = 19_650_218;
        for i in 1..624 {
            value.state[i] = 1_812_433_253u32
                .wrapping_mul(value.state[i - 1] ^ (value.state[i - 1] >> 30))
                .wrapping_add(i as u32);
        }
        let mut i = 1;
        for _ in 0..624 {
            value.state[i] = (value.state[i]
                ^ ((value.state[i - 1] ^ (value.state[i - 1] >> 30)).wrapping_mul(1_664_525)))
            .wrapping_add(seed);
            i += 1;
            if i == 624 {
                value.state[0] = value.state[623];
                i = 1;
            }
        }
        for _ in 0..623 {
            value.state[i] = (value.state[i]
                ^ ((value.state[i - 1] ^ (value.state[i - 1] >> 30)).wrapping_mul(1_566_083_941)))
            .wrapping_sub(i as u32);
            i += 1;
            if i == 624 {
                value.state[0] = value.state[623];
                i = 1;
            }
        }
        value.state[0] = 0x8000_0000;
        value
    }
    fn next(&mut self) -> u32 {
        if self.index == 624 {
            for i in 0..624 {
                let y = (self.state[i] & 0x8000_0000) | (self.state[(i + 1) % 624] & 0x7fff_ffff);
                self.state[i] = self.state[(i + 397) % 624]
                    ^ (y >> 1)
                    ^ if y & 1 == 0 { 0 } else { 0x9908_b0df };
            }
            self.index = 0;
        }
        let mut value = self.state[self.index];
        self.index += 1;
        value ^= value >> 11;
        value ^= (value << 7) & 0x9d2c_5680;
        value ^= (value << 15) & 0xefc6_0000;
        value ^ value >> 18
    }
    fn random(&mut self) -> f64 {
        let high = self.next() >> 5;
        let low = self.next() >> 6;
        (high as f64 * 67_108_864.0 + low as f64) / 9_007_199_254_740_992.0
    }
    fn gauss(&mut self) -> f64 {
        if let Some(value) = self.gaussian.take() {
            return value;
        }
        let angle = self.random() * std::f64::consts::TAU;
        let magnitude = (-2.0 * (1.0 - self.random()).ln()).sqrt();
        self.gaussian = Some(angle.sin() * magnitude);
        angle.cos() * magnitude
    }
    fn receiver_count(&mut self) -> u32 {
        loop {
            let value = self.next() >> 26;
            if value <= 60 {
                return value;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn synthetic_random_stream_matches_existing_python_integer_seeds() {
        for (seed, random, counts) in [
            (
                0,
                [0.8444218515250481, 0.7579544029403025, 0.420571580830845],
                [54, 24, 48, 56],
            ),
            (
                7,
                [0.32383276483316237, 0.15084917392450192, 0.6509344730398537],
                [20, 60, 9, 25],
            ),
            (
                17,
                [0.5219839097124932, 0.8066907771186791, 0.9604947743238768],
                [33, 26, 51, 19],
            ),
            (
                2_147_483_647,
                [0.3177580158172969, 0.8173550078299876, 0.1963472909507713],
                [20, 59, 52, 7],
            ),
        ] {
            let mut rng = PythonRandom::new(seed);
            for expected in random {
                assert_eq!(rng.random(), expected);
            }
            let mut rng = PythonRandom::new(seed);
            for expected in counts {
                assert_eq!(rng.receiver_count(), expected);
            }
        }
        let mut rng = PythonRandom::new(7);
        for expected in [
            -0.2558802884476004,
            0.511431512516514,
            -0.2260961647831047,
            -0.3150684223311854,
            -0.9300181903227674,
        ] {
            assert_eq!(rng.gauss(), expected);
        }
    }
}
