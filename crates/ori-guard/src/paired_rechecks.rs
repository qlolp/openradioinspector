//! Re-evaluate operator-selected archived pairs. Only immutable measurement IDs
//! cross the input boundary; units, intervals and both levels come from SQLite.
use crate::{
    GuardError, GuardService, Result, qualification,
    util::{decode, integer, invalid, s},
};
use ori_contracts::TimeInterval;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

const METHOD: &str = "paired-local-recheck-v1";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Pair {
    sweep_id: String,
    observation_ids: Vec<String>,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Controls {
    lower: Pair,
    upper: Pair,
}
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Recipe {
    initial: Pair,
    neighbors: Vec<Pair>,
    controls: Controls,
    repeats: Vec<Pair>,
}

impl Recipe {
    fn validate(&self) -> Result<()> {
        if !(10..=22).contains(&self.neighbors.len()) || !(1..=2).contains(&self.repeats.len()) {
            return Err(invalid(
                "Нужны 10–22 фоновые частоты и один или два повторных визита",
            ));
        }
        let pairs = std::iter::once(&self.initial)
            .chain(&self.neighbors)
            .chain([&self.controls.lower, &self.controls.upper])
            .chain(&self.repeats);
        let mut observations = HashSet::new();
        let valid_id = |s: &str| !s.trim().is_empty() && s.chars().count() <= 200;
        for pair in pairs {
            if !valid_id(&pair.sweep_id)
                || !(1..=32).contains(&pair.observation_ids.len())
                || !pair
                    .observation_ids
                    .iter()
                    .all(|id| valid_id(id) && observations.insert(id))
            {
                return Err(invalid(
                    "Требуются уникальные ID исходных наблюдений и ID свипа",
                ));
            }
        }
        Ok(())
    }
}

#[derive(PartialEq)]
struct Profile {
    analyzer: Value,
    receiver: Value,
}
struct Sample {
    hz: u64,
    tiny: f64,
    ar: f64,
    start: i64,
    end: i64,
}
struct Context<'a> {
    session: &'a str,
    analyzer: &'a str,
    receiver: &'a str,
    simulated: bool,
    profile: Option<Profile>,
}

fn median(mut values: Vec<f64>) -> f64 {
    values.sort_by(f64::total_cmp);
    let n = values.len();
    (values[(n - 1) / 2] + values[n / 2]) / 2.
}
fn background(values: Vec<f64>, floor: f64) -> (f64, f64) {
    let center = median(values.clone());
    let mad = median(values.into_iter().map(|v| (v - center).abs()).collect());
    (center, (1.4826 * mad).max(floor))
}
fn interval(source: &Value) -> Result<(i64, i64)> {
    let t: TimeInterval = decode(&source["interval"])?;
    Ok((t.started_at.unix_micros(), t.ended_at.unix_micros()))
}

fn analyzer_profile(settings: &Value) -> Result<Value> {
    let mut stable = settings
        .as_object()
        .cloned()
        .ok_or_else(|| invalid("Неизвестны настройки анализатора"))?;
    for k in [
        "sequence",
        "connection_id",
        "segment_id",
        "tune_sequence",
        "measurement_qualification",
        "scan_window_index",
        "scan_window_count",
    ] {
        stable.remove(k);
    }
    if let Some(instrument) = stable
        .get_mut("instrument_settings")
        .and_then(Value::as_object_mut)
    {
        for k in ["start_hz", "stop_hz", "points"] {
            instrument.remove(k);
        }
    }
    Ok(Value::Object(stable))
}

impl GuardService {
    async fn paired_sample(&self, pair: &Pair, ctx: &mut Context<'_>) -> Result<Sample> {
        let sweep = self
            .archive
            .get_sweep(&pair.sweep_id)
            .await?
            .ok_or(GuardError::NotFound)?;
        let settings = &sweep["settings"];
        let adapter = if ctx.simulated {
            "tinysa-sim"
        } else {
            "tinysa-serial"
        };
        let bins = sweep["frequencies_hz"]
            .as_array()
            .ok_or_else(|| invalid("Отсутствует частотная сетка"))?;
        if sweep["session_id"] != ctx.session
            || sweep["stream_id"] != ctx.analyzer
            || sweep["level_unit"] != "dBm"
            || settings["adapter"] != adapter
            || settings["simulated"] != ctx.simulated
            || settings["incomplete"] == true
            || settings["overload"] == true
            || settings["replay"] == true
            || !settings["rbw_hz"].is_u64()
            || bins.is_empty()
            || bins.windows(2).any(|w| w[0].as_u64() >= w[1].as_u64())
        {
            return Err(invalid("Несовместимый исходный свип tinySA"));
        }
        let start_hz = bins
            .first()
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid("Частотная сетка"))?;
        let stop_hz = bins
            .last()
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid("Частотная сетка"))?;
        if !ctx.simulated
            && qualification::measurement_qualification(Some(adapter), start_hz, stop_hz)["status"]
                != "supported"
        {
            return Err(qualification::unverified_reference());
        }
        let (start, end) = interval(&sweep)?;
        let analyzer = analyzer_profile(settings)?;
        let mut frequency = None;
        let mut counts = Vec::with_capacity(pair.observation_ids.len());
        for id in &pair.observation_ids {
            let o = self
                .archive
                .get_observation(id)
                .await?
                .ok_or(GuardError::NotFound)?;
            let m = &o["metadata"];
            let hz = integer(&o, "frequency_hz")?;
            let (os, oe) = interval(&o)?;
            if o["session_id"] != ctx.session
                || o["stream_id"] != ctx.receiver
                || o["level_unit"] != "s_meter_count"
                || m["simulated"] != ctx.simulated
                || m["replay"] == true
                || start.max(os) >= end.min(oe)
                || m["adapter"]
                    != if ctx.simulated {
                        "ardv1-sim"
                    } else {
                        "ardv1-serial"
                    }
                || (!ctx.simulated
                    && (m["frequency_readback"] != true
                        || m["frequency_source"] != "receiver_RX"
                        || m["mode_source"] != "receiver_RX"
                        || m["observed_frequency_hz"] != hz
                        || m["observed_mode"] != o["mode"]))
                || frequency.replace(hz).is_some_and(|previous| previous != hz)
            {
                return Err(invalid(
                    "Не подтверждены частота, пересечение интервалов или источник AR-DV1",
                ));
            }
            let count = o["level"]
                .as_f64()
                .filter(|n| (0.0..=f64::from(u32::MAX)).contains(n) && n.fract() == 0.)
                .ok_or_else(|| invalid("Нужны исходные целые отсчёты S-метра"))?;
            counts.push(count);
            let receiver = json!([
                o["mode"],
                m["adapter"],
                m["receiver_profile"],
                m["receiver_firmware"]
            ]);
            if let Some(reference) = &ctx.profile {
                if reference.analyzer != analyzer || reference.receiver != receiver {
                    return Err(invalid("Настройки приёмного тракта различаются"));
                }
            } else {
                ctx.profile = Some(Profile {
                    analyzer: analyzer.clone(),
                    receiver,
                });
            }
        }
        let hz = frequency.ok_or_else(|| invalid("Нет наблюдений"))?;
        let index = bins
            .binary_search_by_key(&hz, |x| x.as_u64().unwrap_or(u64::MAX))
            .map_err(|_| invalid("Частота AR-DV1 отсутствует в исходном свипе"))?;
        let tiny = sweep["levels"][index]
            .as_f64()
            .filter(|x| x.is_finite())
            .ok_or_else(|| invalid("Нет отсчёта в dBm"))?;
        Ok(Sample {
            hz,
            tiny,
            ar: median(counts),
            start,
            end,
        })
    }

    // Look up the submitted original directly, including archives which predate
    // the dedicated peak record. This is independent of the event's page count.
    async fn paired_peak(
        &self,
        project: &str,
        event: &Value,
        sweep_id: &str,
    ) -> Result<Option<Value>> {
        let event_id = s(event, "id")?;
        let current = self.archive.monitoring("event-peak", event_id).await?;
        let linked = if let Some(peak) = current {
            if peak["project_id"] != project
                || peak["sweep_id"] != sweep_id
                || peak["center_frequency_hz"] != event["center_frequency_hz"]
                || peak["peak_level"] != event["peak_level"]
            {
                return Ok(None);
            }
            Some(peak)
        } else {
            self.archive
                .monitoring("event-source", &format!("{event_id}.{sweep_id}"))
                .await?
                .filter(|row| {
                    row["event_id"] == event_id
                        && row["project_id"] == project
                        && row["sweep_id"] == sweep_id
                })
                .or_else(|| {
                    event["source_refs"]
                        .as_array()?
                        .iter()
                        .find(|row| row["sweep_id"] == sweep_id)
                        .cloned()
                })
        };
        let Some(linked) = linked else {
            return Ok(None);
        };
        let Some(raw) = self.archive.get_sweep(sweep_id).await? else {
            return Ok(None);
        };
        let session = s(&raw, "session_id")?;
        let center = integer(event, "center_frequency_hz")?;
        if raw["session_id"] != linked["session_id"]
            || raw["stream_id"] != linked["stream_id"]
            || raw["level_unit"] != "dBm"
            || event["run_session_id"]
                .as_str()
                .is_some_and(|sid| sid != session)
            || ![
                "scan_plan_id",
                "scan_plan_revision",
                "scan_band_id",
                "scan_window_index",
            ]
            .iter()
            .all(|key| raw["settings"][*key] == event[*key])
            || !crate::signals::contains_catalog_peak(
                &raw,
                center,
                crate::util::number(event, "peak_level")?,
                &crate::util::timestamp(s(event, "first_seen")?)?,
                &crate::util::timestamp(s(event, "last_seen")?)?,
            )?
            || !self.archive.monitoring_member(project, session).await?
        {
            return Ok(None);
        }
        Ok(Some(
            json!({"sweep_id":sweep_id,"session_id":session,"stream_id":raw["stream_id"],
            "center_frequency_hz":center,"peak_level":event["peak_level"]}),
        ))
    }

    pub async fn paired_rechecks(&self, project: &str, event: &str) -> Result<Value> {
        let current = self.get_record("event", event, Some(project)).await?;
        let head = format!("event-{event}");
        if let Some(pointer) = self.archive.monitoring("paired-recheck", &head).await? {
            if pointer["project_id"] != project {
                return Err(GuardError::NotFound);
            }
            let result = self
                .get_record("paired-recheck", s(&pointer, "result_id")?, Some(project))
                .await?;
            if result["event_id"] == event
                && self
                    .paired_peak(
                        project,
                        &current,
                        s(&result["recipe"]["initial"], "sweep_id")?,
                    )
                    .await?
                    .is_some()
            {
                return Ok(result);
            }
        }
        Ok(json!({"project_id":project,"event_id":event,"state":"missing_evidence","visits":[]}))
    }

    pub async fn save_paired_rechecks(
        &self,
        project: &str,
        event_id: &str,
        payload: Value,
    ) -> Result<Value> {
        let recipe: Recipe = serde_json::from_value(payload)
            .map_err(|_| invalid("Рецепт должен содержать только ссылки на исходники"))?;
        recipe.validate()?;
        let event = self.get_record("event", event_id, Some(project)).await?;
        // Qualification is checked on every actual archived window, including
        // the initial peak. A later project plan cannot change its geometry.
        let peak = self
            .paired_peak(project, &event, &recipe.initial.sweep_id)
            .await?
            .ok_or_else(|| invalid("Первый свип должен быть исходником пика события"))?;
        let hz = integer(&event, "center_frequency_hz")?;
        let session = s(&peak, "session_id")?;
        let run = self
            .archive
            .get_session(session)
            .await?
            .ok_or(GuardError::NotFound)?;
        let streams = run["streams"]
            .as_array()
            .ok_or_else(|| invalid("Нет потоков сеанса"))?;
        let analyzer = streams
            .iter()
            .find(|v| v["id"] == peak["stream_id"] && v["role"] == "spectrum_analyzer")
            .ok_or(GuardError::NotFound)?;
        let mut receivers = streams.iter().filter(|v| v["role"] == "receiver");
        let receiver = receivers.next().ok_or(GuardError::NotFound)?;
        if receivers.next().is_some() {
            return Err(invalid("Неоднозначный приёмник в сеансе события"));
        }
        let simulated = match (
            analyzer["settings"]["adapter"].as_str(),
            receiver["settings"]["adapter"].as_str(),
        ) {
            (Some("tinysa-sim"), Some("ardv1-sim")) => true,
            (Some("tinysa-serial"), Some("ardv1-serial")) => false,
            _ => return Err(invalid("Требуется одна совместимая пара tinySA и AR-DV1")),
        };
        let mut ctx = Context {
            session,
            analyzer: s(analyzer, "id")?,
            receiver: s(receiver, "id")?,
            simulated,
            profile: None,
        };
        let initial = self.paired_sample(&recipe.initial, &mut ctx).await?;
        if initial.hz != hz || event["peak_level"].as_f64() != Some(initial.tiny) {
            return Err(invalid("Отсчёт исходного пика не совпадает с событием"));
        }
        let mut nearby = Vec::new();
        let mut freq = HashSet::new();
        for pair in &recipe.neighbors {
            let s = self.paired_sample(pair, &mut ctx).await?;
            let offset = s.hz.abs_diff(hz);
            if !(20_000..=120_000).contains(&offset)
                || !offset.is_multiple_of(10_000)
                || !freq.insert(s.hz)
            {
                return Err(invalid(
                    "Фон требует разных частот на локальной сетке 10 кГц",
                ));
            }
            nearby.push(s);
        }
        if !freq.iter().any(|v| *v < hz) || !freq.iter().any(|v| *v > hz) {
            return Err(invalid("Фон нужен по обе стороны сигнала"));
        }
        let (tiny, sigma_t) = background(nearby.iter().map(|v| v.tiny).collect(), 2.);
        let (ar, sigma_a) = background(nearby.iter().map(|v| v.ar).collect(), 4.);
        let tiny_gate = (3. * sigma_t).max(10.);
        let ar_gate = (3. * sigma_a).max(18.);
        let td = initial.tiny - tiny;
        let ad = ar - initial.ar;
        let on_tiny = if td >= tiny_gate && ad.abs() <= 4. {
            true
        } else if ad >= ar_gate && td.abs() <= 3. {
            false
        } else {
            return Err(invalid(
                "Исходные данные не удовлетворяют критерию расхождения",
            ));
        };
        let lo = self.paired_sample(&recipe.controls.lower, &mut ctx).await?;
        let hi = self.paired_sample(&recipe.controls.upper, &mut ctx).await?;
        let end_initial = nearby.iter().map(|s| s.end).fold(initial.end, i64::max);
        if hz.checked_sub(100_000) != Some(lo.hz)
            || hz.checked_add(100_000) != Some(hi.hz)
            || lo.start < end_initial
            || hi.start < end_initial
        {
            return Err(invalid(
                "Для повторов нужны последующие контрольные пары на ±100 кГц",
            ));
        }
        let mut visits = Vec::new();
        let mut prior_end = end_initial;
        let (mut signals, mut differences) = (0, 0);
        for r in &recipe.repeats {
            let v = self.paired_sample(r, &mut ctx).await?;
            if v.hz != hz || v.start < prior_end || v.start >= v.end {
                return Err(invalid(
                    "Повторные визиты должны быть последовательными отдельными измерениями центра",
                ));
            }
            prior_end = v.end;
            let td = v.tiny - (lo.tiny + hi.tiny) / 2.;
            let ad = (lo.ar + hi.ar) / 2. - v.ar;
            let signal = if on_tiny {
                td >= tiny_gate
            } else {
                ad >= ar_gate
            };
            let discrepancy = signal
                && if on_tiny {
                    ad.abs() <= 4.
                } else {
                    td.abs() <= 3.
                };
            signals += i32::from(signal);
            differences += i32::from(discrepancy);
            visits.push(
                json!({"sweep_id":r.sweep_id,"observation_ids":r.observation_ids,"tiny_delta_db":td,
                "ar_drop_counts":ad,"signal_repeated":signal,"discrepancy_repeated":discrepancy}),
            );
        }
        let recipe = serde_json::to_value(&recipe).map_err(|e| invalid(e.to_string()))?;
        let identity = serde_json::to_vec(&json!([METHOD, project, event_id, &recipe]))
            .map_err(|e| invalid(e.to_string()))?;
        let hash = Sha256::digest(identity)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        let id = format!("result-{hash}");
        let _writer = self.coordinator.lock().await;
        self.ensure_open()?;
        let current = self.event_record(project, event_id).await?;
        if self
            .paired_peak(project, &current, s(&peak, "sweep_id")?)
            .await?
            .as_ref()
            != Some(&peak)
        {
            return Err(GuardError::Conflict(
                "За время расчёта изменился исходный пик события".into(),
            ));
        }
        if let Some(saved) = self.archive.monitoring("paired-recheck", &id).await? {
            return Ok(saved);
        }
        let result = json!({
            "id":id,"project_id":project,"event_id":event_id,"created_at":self.clock.now().as_str(),
            "methodology":METHOD,"state":"evaluated","frequency_hz":hz,"instrument":if on_tiny{"tinySA"}else{"AR-DV1"},
            "simulated":simulated,"visits":visits,"signal_repeats":signals,"discrepancy_repeats":differences,"repeat_total":visits.len(),
            "recipe":recipe,"initial_tiny_delta_db":td,"initial_ar_drop_counts":ad,
            "local_background":{"tiny_dbm":tiny,"ar_s_meter_count":ar,"tiny_robust_sigma_db":sigma_t,"ar_robust_sigma_counts":sigma_a}
        });
        let head = json!({"id":format!("event-{event_id}"),"project_id":project,"created_at":result["created_at"],"result_id":id});
        self.archive
            .apply(vec![
                Self::mutation("paired-recheck", result.clone()),
                Self::mutation("paired-recheck", head),
            ])
            .await?;
        Ok(result)
    }
}
