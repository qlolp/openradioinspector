use crate::{
    GuardError, GuardService, Result, measurement_qualification,
    qualification::{
        analyzer_adapter, eligible_windows, source_qualification, unverified_reference,
    },
    util::*,
};
use serde_json::{Value, json};

pub(crate) const MONITOR_MODES: &[&str] = &[
    "AUTO", "DSTAR", "YSF", "ALINCO", "NXDN", "P25", "DPMR", "DMR", "FM", "AM",
];
pub(crate) const CONTROL_PROFILE: &str = "tinysa-ultra-zs405-control-v1";
pub(crate) fn receive_settings(settings: &Value) -> Result<Value> {
    if settings
        .get("receive_profile")
        .is_some_and(|p| p != "tinysa-ultra-zs405-receive-v1")
    {
        return Err(invalid("tinySA: неизвестный receive_profile"));
    }
    let maximum = if let Some(record) = settings.get("scan_plan") {
        let plan: ori_monitoring::ScanPlan = serde_json::from_value(record["config"].clone())
            .map_err(|_| invalid("Повреждён сохранённый план обзора"))?;
        plan.validate_receive_plan(800_000_000)?;
        if plan.bands[0].step_hz.is_some() {
            1_300_000_000
        } else {
            800_000_000
        }
    } else {
        800_000_000
    };
    let mut out = json!({});
    for (key, default, low, high) in [
        ("start_hz", 88_000_000, 100_000, maximum),
        ("stop_hz", 108_000_000, 100_000, maximum),
        ("points", 201, 2, 290),
        ("rbw_hz", 10_000, 10_000, 30_000),
    ] {
        let v = settings
            .get(key)
            .map(Value::as_u64)
            .unwrap_or(Some(default))
            .filter(|v| *v >= low && *v <= high)
            .ok_or_else(|| {
                invalid(format!(
                    "tinySA {key}: консервативный профиль требует целое {low}…{high}"
                ))
            })?;
        out[key] = json!(v);
    }
    if out["stop_hz"].as_u64() <= out["start_hz"].as_u64()
        || ![json!(10000), json!(30000)].contains(&out["rbw_hz"])
    {
        return Err(invalid(
            "Некорректная полоса/RBW консервативного RX профиля",
        ));
    }
    Ok(out)
}
fn float_compatible(v: &Value) -> Option<f64> {
    match v {
        Value::Bool(v) => Some(if *v { 1. } else { 0. }),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => v.as_f64(),
    }
    .filter(|x| x.is_finite())
}
fn integer_compatible(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(v) => Some(i64::from(*v)),
        Value::String(s) => s.trim().parse().ok(),
        _ => v.as_i64().or_else(|| {
            v.as_f64()
                .filter(|x| x.is_finite() && *x >= i64::MIN as f64 && *x < i64::MAX as f64)
                .map(|x| x as i64)
        }),
    }
}
fn worker_settings(settings: &Value) -> bool {
    float_compatible(settings.get("period_s").unwrap_or(&json!(0.25)))
        .is_some_and(|v| (0.0..=3600.).contains(&v))
        && settings
            .get("max_consecutive_errors")
            .map(Value::as_u64)
            .unwrap_or(Some(3))
            .is_some_and(|n| (1..=100).contains(&n))
        && settings
            .get("failures_remaining")
            .map(Value::as_u64)
            .unwrap_or(Some(0))
            .is_some_and(|n| n <= 10_000)
}
pub(crate) fn receiver_frequency(value: &Value) -> Result<u64> {
    let frequency = value
        .as_u64()
        .filter(|f| (100_000..=1_300_000_000).contains(f) && f % 10 == 0)
        .ok_or_else(|| {
            invalid("Частота AR-DV1 должна быть в диапазоне 0,1–1300 МГц и кратна 10 Гц")
        })?;
    Ok(frequency)
}
impl GuardService {
    pub(crate) async fn window_counts(
        &self,
        project: &Value,
        band: &ori_monitoring::ScanBand,
    ) -> Result<Vec<(u32, u64, f64)>> {
        Ok(self
            .archive
            .scan_window_counts(
                s(project, "active_session_id")?,
                s(project, "analyzer_stream_id")?,
                s(&project["scan_plan"], "id")?,
                integer(&project["scan_plan"], "revision")?,
                &band.id,
            )
            .await?)
    }
    pub async fn baseline_collection(&self, project: &Value) -> Result<Value> {
        if project["primary_source_kind"] == "ardv1" {
            return self.receiver_collection(project).await;
        }
        if crate::rfe::survey(project) {
            return self.rfe_collection(project).await;
        }
        if let Some(plan) = crate::scan_plan::configured_plan(project)? {
            let mut bands = vec![];
            for band in plan.bands {
                let count = band.window_count();
                let eligible = eligible_windows(project, &band).count() as u32;
                let (sweeps, elapsed, ready, confirmed) = if band.step_hz.is_some() {
                    let windows = self.window_counts(project, &band).await?;
                    let mut sweeps = u64::MAX;
                    let mut elapsed = f64::MAX;
                    let mut ready = 0;
                    let mut confirmed = 0;
                    // Eligible windows form a contiguous prefix of the ascending
                    // grid. A wholly excluded band still shows its raw counts.
                    for index in 0..if eligible == 0 { count } else { eligible } {
                        let (_, n, t) = windows
                            .binary_search_by_key(&index, |w| w.0)
                            .ok()
                            .map(|i| windows[i])
                            .unwrap_or((index, 0, 0.));
                        sweeps = sweeps.min(n);
                        elapsed = elapsed.min(t);
                        if index < eligible {
                            ready += u32::from(n >= 20 && t >= 60.);
                            confirmed += u32::from(
                                project["band_baselines"][band.context_key(index)].is_string(),
                            );
                        }
                    }
                    (sweeps, elapsed, ready, confirmed)
                } else {
                    let selected = self
                        .archive
                        .baseline_selection_for_scan_band(
                            s(project, "active_session_id")?,
                            s(project, "analyzer_stream_id")?,
                            s(&project["scan_plan"], "id")?,
                            &band.id,
                        )
                        .await?;
                    (
                        selected.count,
                        selected.elapsed_seconds,
                        u32::from(
                            eligible > 0 && selected.count >= 20 && selected.elapsed_seconds >= 60.,
                        ),
                        u32::from(eligible > 0 && project["band_baselines"][&band.id].is_string()),
                    )
                };
                let qualification = measurement_qualification(
                    analyzer_adapter(project),
                    band.start_hz,
                    band.stop_hz,
                );
                bands.push(json!({"band_id":band.id,"name":band.name,"sweeps":sweeps,
                    "elapsed_seconds":elapsed,"minimum_sweeps":20,"minimum_seconds":60,
                    "measurement_qualification":qualification,"window_count":count,"eligible_windows":eligible,
                    "excluded_windows":count-eligible,"ready_windows":ready,"confirmed_windows":confirmed,
                    "ready":eligible>0 && ready==eligible,"baseline_id":(eligible>0).then_some(&project["band_baselines"][&band.id])}));
            }
            let eligible = bands
                .iter()
                .filter(|b| b["eligible_windows"].as_u64().is_some_and(|n| n > 0));
            let ready = eligible.clone().next().is_some()
                && eligible.into_iter().all(|b| b["ready"] == true);
            return Ok(
                json!({"bands":bands,"ready":ready,"minimum_sweeps":20,"minimum_seconds":60}),
            );
        }
        let selected = if project["primary_source_kind"] == "rf_explorer" {
            self.fixed_rfe_selection(project).await?
        } else {
            self.archive
                .baseline_selection(
                    s(project, "active_session_id")?,
                    s(project, "analyzer_stream_id")?,
                )
                .await?
        };
        Ok(
            json!({"sweeps":selected.count,"elapsed_seconds":selected.elapsed_seconds,"minimum_sweeps":20,"minimum_seconds":60,"ready":selected.count>=20&&selected.elapsed_seconds>=60.}),
        )
    }
    pub(crate) async fn guard_acquisition_context(
        &self,
        project: &Value,
    ) -> Result<Option<String>> {
        if project["baseline_id"].is_null()
            || project["profile"]["analyzer"]["settings"]["adapter"] != "tinysa-serial"
        {
            return Ok(None);
        }
        let baseline = self
            .get_record(
                "baseline",
                s(project, "baseline_id")?,
                Some(s(project, "id")?),
            )
            .await?;
        let reference = baseline["source_refs"]
            .as_array()
            .and_then(|a| a.first())
            .ok_or_else(|| invalid("baseline source_refs"))?;
        let original = self
            .archive
            .get_sweep(s(reference, "sweep_id")?)
            .await?
            .ok_or(GuardError::NotFound)?;
        if !self
            .archive
            .monitoring_member(s(project, "id")?, s(&original, "session_id")?)
            .await?
        {
            return Err(GuardError::NotFound);
        }
        let sweep = decode(&original)?;
        if !source_qualification(project, &sweep)?.eligible() {
            return Err(unverified_reference());
        }
        let settings = &original["settings"];
        let Some(instrument) = settings.get("instrument") else {
            return Ok(None);
        };
        let mut requested = receive_settings(&project["profile"]["analyzer"]["settings"])?;
        if let Some(plan) = crate::scan_plan::configured_plan(project)? {
            let (band, index) = crate::scan_plan::source_scope(project, &plan, &sweep)?;
            let window = band.window(index).expect("validated reference window");
            requested["start_hz"] = json!(window.start_hz);
            requested["stop_hz"] = json!(window.stop_hz);
            requested["points"] = json!(window.points);
            requested["rbw_hz"] = json!(window.rbw_hz);
        }
        requested["attenuation_db"] = json!("auto");
        requested["lna"] = json!(false);
        requested["ultra"] = json!(
            requested["stop_hz"]
                .as_u64()
                .is_some_and(|s| s > 800_000_000)
        );
        requested["calculation"] = json!("off");
        let expected = json!({"profile":CONTROL_PROFILE,"settings":requested,"device_mode":"input","device_mode_readback":false,"settings_source":"command_acknowledged","settings_readback":false,"settings_complete":false,"hardware_validation":"not_run"});
        if ori_monitoring::stable(instrument)? != expected
            || settings["receive_profile"] != CONTROL_PROFILE
            || settings["generator_output"] != "unknown"
            || settings["generator_output_source"] != "no_physical_readback"
        {
            return Err(invalid(
                "RX-настройки эталона нельзя воспроизвести консервативным запуском; соберите новый эталон",
            ));
        }
        Ok(Some("inspect".into()))
    }
    pub async fn preflight(&self, id: &str, for_preparation: bool) -> Result<Value> {
        let mut p = self.project(id).await?;
        let plan = crate::scan_plan::configured_plan(&p)?;
        if let Some(plan) = &plan {
            p["baseline_id"] = crate::scan_plan::primary_baseline(&p, plan);
        }
        let session = self
            .archive
            .get_session(s(&p, "active_session_id")?)
            .await?
            .ok_or(GuardError::NotFound)?;
        // A saved plan configures the next preparation/armed clone. Validate its
        // physical first window, including receive-only upper windows, against
        // the plan; the original template session may predate that configuration.
        let mut proposed = session.clone();
        if plan.is_some()
            && let Some(streams) = proposed["streams"].as_array_mut()
        {
            for stream in streams
                .iter_mut()
                .filter(|s| s["id"] == p["analyzer_stream_id"])
            {
                stream["settings"] = p["profile"]["analyzer"]["settings"].clone();
            }
        }
        let typed: ori_contracts::Session = decode(&proposed)?;
        let stream = |key: &str| -> Result<Value> {
            if p[key].is_null() {
                return Ok(Value::Null);
            }
            let id = s(&p, key)?;
            proposed["streams"]
                .as_array()
                .and_then(|a| a.iter().find(|s| s["id"].as_str() == Some(id)))
                .cloned()
                .ok_or(GuardError::NotFound)
        };
        let analyzer = stream("analyzer_stream_id")?;
        let receiver = stream("receiver_stream_id")?;
        let aa = &analyzer["settings"]["adapter"];
        let ra = &receiver["settings"]["adapter"];
        let mut checks = vec![];
        let mut check = |name: &str, ok: bool, detail: String| {
            checks.push(json!({"name":name,"ok":ok,"detail":detail}))
        };
        check(
            "service_active",
            !self.closed.load(std::sync::atomic::Ordering::Acquire),
            "Служба принимает новые действия".into(),
        );
        if p["manual_primary_hold"] == true {
            check(
                "manual_primary_hold",
                false,
                "Основной прибор передан оператору; требуется явный возврат или снятие охраны"
                    .into(),
            );
        }
        if let Some(plan) = &plan {
            let maximum = if aa == "tinysa-serial" {
                800_000_000
            } else {
                20_000_000_000
            };
            check(
                "scan_plan_receive_profile",
                plan.validate_receive_plan(maximum).is_ok(),
                "Все поддиапазоны должны соответствовать RX-профилю".into(),
            );
            if !for_preparation
                && !plan
                    .bands
                    .iter()
                    .any(|b| eligible_windows(&p, b).next().is_some())
            {
                check(
                    "measurement_qualification",
                    false,
                    "Ни одно окно плана не пригодно для автоматической охраны".into(),
                );
            }
        }
        check(
            "adapters",
            matches!(
                (aa.as_str(), ra.as_str()),
                (
                    Some("tinysa-serial" | "rf-explorer-3g-serial"),
                    None | Some("ardv1-serial")
                ) | (Some("tinysa-sim"), None | Some("ardv1-sim"))
                    | (None, Some("ardv1-serial"))
            ),
            if p.get("primary_source_kind").is_none() {
                "tinySA Ultra + AR-DV1; симуляторы выбираются только явно"
            } else {
                "Явно выбранный RX-профиль основного и вторичного приборов"
            }
            .into(),
        );
        for (label, stream, baud) in [
            ("analyzer", &analyzer, 9600),
            ("receiver", &receiver, 115200),
        ] {
            if stream.is_null() {
                continue;
            }
            check(
                &format!("{label}_worker_settings"),
                worker_settings(&stream["settings"]),
                "Период и параметры повторов совместимы с исполнителем".into(),
            );
            if matches!(
                stream["settings"]["adapter"].as_str(),
                Some("tinysa-serial" | "ardv1-serial" | "rf-explorer-3g-serial")
            ) {
                let valid = stream["port"].as_str().is_some_and(|s| {
                    !s.trim().is_empty()
                        && !s.starts_with("sim://")
                        && (!cfg!(windows) || valid_com(s))
                });
                check(&format!("{label}_serial_port"),valid,"Нужен явно заданный последовательный порт; доступность без открытия не проверяется".into());
                let rfe = stream["settings"]["adapter"] == "rf-explorer-3g-serial";
                let baud =
                    integer_compatible(stream["settings"].get("baud").unwrap_or(&json!(if rfe {
                        500000
                    } else {
                        baud
                    })));
                check(
                    &format!("{label}_serial_baud"),
                    if rfe {
                        baud == Some(500000)
                    } else {
                        baud.is_some_and(|b| [9600, 19200, 38400, 57600, 115200].contains(&b))
                    },
                    if rfe {
                        "RF Explorer Standard: 500000"
                    } else {
                        "Скорости порта: 9600, 19200, 38400, 57600, 115200"
                    }
                    .into(),
                );
            }
        }
        if ra == "ardv1-serial" {
            let settings = &receiver["settings"];
            let profile = settings.get("receiver_profile").filter(|v| !v.is_null());
            let mode = settings["mode"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or("FM")
                .trim()
                .to_uppercase();
            let ok = receiver_frequency(settings.get("frequency_hz").unwrap_or(&json!(145500000)))
                .is_ok()
                && (profile.is_none() || profile == Some(&json!("ardv1-monitor-v1")))
                && if profile == Some(&json!("ardv1-monitor-v1")) {
                    MONITOR_MODES.contains(&mode.as_str())
                } else {
                    ["FM", "AM"].contains(&mode.as_str())
                };
            check(
                "receiver_profile",
                ok,
                "Конфигурация AR-DV1 совместима с профилем; аппаратная проверка не выполнена"
                    .into(),
            );
        }
        if p["primary_source_kind"] == "ardv1" {
            check(
                "cyclic_receiver_scan",
                crate::aor::plan(&p).is_ok(),
                "Требуется циклический план AR-DV1 с проверкой RX".into(),
            );
        }
        if aa == "tinysa-serial"
            || aa == "rf-explorer-3g-serial"
            || p["primary_source_kind"] == "ardv1"
        {
            check(
                "operator_receive_path",
                p["preparation"]["receive_path_confirmed"] == true
                    && p["preparation"]["location_confirmed"] == true,
                "Оператор подтверждает антенну, тракт и место; RF readback не заявлен".into(),
            );
        }
        if aa == "tinysa-serial" {
            let received = receive_settings(&analyzer["settings"]);
            check(
                "receive_profile",
                received.is_ok(),
                received
                    .err()
                    .map(|e| e.to_string())
                    .unwrap_or_else(|| "Консервативный профиль приёма; RF off".into()),
            );
            if !p["baseline_id"].is_null() && !for_preparation {
                let context = self.guard_acquisition_context(&p).await;
                check(
                    "baseline_acquisition_context",
                    context.is_ok(),
                    context.err().map(|e| e.to_string()).unwrap_or_else(|| {
                        "Контекст RX эталона воспроизводим; аппаратная проверка не выполнена".into()
                    }),
                );
            }
        }
        if !p["baseline_id"].is_null() && !for_preparation {
            let result = async {
                let b = self
                    .get_record("baseline", s(&p, "baseline_id")?, Some(id))
                    .await?;
                if p["primary_source_kind"] == "ardv1" || crate::rfe::survey(&p) {
                    self.validate_standalone_baseline(&p, &b).await?;
                    return Ok(b);
                }
                self.detection_baseline(b).await
            }
            .await;
            let ok = result.as_ref().is_ok_and(|b| {
                b[if p["primary_source_kind"] == "ardv1" {
                    "pass_period_seconds"
                } else {
                    "sweep_period_seconds"
                }]
                .as_f64()
                .is_some_and(|n| n.is_finite() && n > 0.)
            });
            check(
                "baseline_cadence",
                ok,
                "Период эталона известен; при недоступных источниках соберите новый эталон".into(),
            );
        }
        let instrument = self.control.preflight(&typed).await;
        match instrument {
            Ok(report) => {
                check("instrument_capabilities",report.ready==Some(true),"Поддержанные профили/известные возможности; неизвестное не подтверждает готовность".into());
                for c in report.checks {
                    check(&c.name, c.confirmed == Some(true), c.detail);
                }
            }
            Err(e) => check("instrument_capabilities", false, e.to_string()),
        }
        if let Some(config) = p.get("audio").filter(|a| !a.is_null()) {
            let audio = self.control.audio_inputs().await;
            let ok = audio.as_ref().is_ok_and(|a| {
                a.available == Some(true)
                    && a.devices.iter().any(|d| {
                        Some(u64::from(d.index)) == config["device"].as_u64()
                            && Some(d.name.as_str()) == config["name"].as_str()
                    })
            });
            check(
                "audio_input",
                ok,
                "Настроенный оператором аудиовход должен быть доступен с тем же индексом и именем"
                    .into(),
            );
        }
        let capacity = self.archive.capacity().await?;
        check(
            "archive",
            capacity["state"] != "full",
            "Полный архив; автоматическое удаление выключено".into(),
        );
        let receiver_primary = p["primary_source_kind"] == "ardv1";
        let sweeps = if receiver_primary {
            self.archive
                .list_observations(&typed.id, Some(s(&p, "receiver_stream_id")?), 128)
                .await?
        } else {
            self.archive
                .list_sweeps(&typed.id, Some(s(&p, "analyzer_stream_id")?), 128)
                .await?
        };
        let mut durations = vec![];
        let mut bytes = 0usize;
        for sweep in &sweeps {
            durations.push(elapsed(
                s(&sweep["interval"], "started_at")?,
                s(&sweep["interval"], "ended_at")?,
            )?);
            bytes = bytes.saturating_add(spaced_json(sweep, true)?.len());
        }
        let n = sweeps.len();
        let mut performance = json!({"sweeps":n,"mean_sweep_seconds":if n>0{Some(durations.iter().sum::<f64>()/n as f64)}else{None},"mean_sweep_bytes":if n>0{Some(bytes as f64/n as f64)}else{None}});
        let storage = self.storage_report(id, true).await?;
        if storage["forecast_basis"] == "duty_window"
            || p.get("archive").is_some_and(|archive| {
                archive.get("reserve_backup").is_some() || archive.get("reserve_restore").is_some()
            })
        {
            let cycle = if n > 1 {
                let span = elapsed(
                    s(&sweeps[0]["interval"], "started_at")?,
                    s(&sweeps[n - 1]["interval"], "started_at")?,
                )?;
                (span.is_finite() && span > 0.).then_some(span / (n - 1) as f64)
            } else {
                None
            };
            performance["mean_observed_cycle_seconds"] = json!(cycle);
            performance["latest_sweep_ended_at"] = sweeps
                .last()
                .map(|sweep| sweep["interval"]["ended_at"].clone())
                .unwrap_or(Value::Null);
        }
        if receiver_primary {
            let values = performance.as_object_mut().expect("performance object");
            for (original, replacement) in [
                ("sweeps", "observations"),
                ("mean_sweep_bytes", "mean_observation_bytes"),
                ("mean_sweep_seconds", "mean_observation_seconds"),
                ("latest_sweep_ended_at", "latest_observation_ended_at"),
            ] {
                if let Some(value) = values.remove(original) {
                    values.insert(replacement.to_owned(), value);
                }
            }
        }
        check(
            "storage_budget",
            storage["ready"] == true,
            "Свободное место, запись и прогноз хранения без автоудаления".into(),
        );
        let workers = self.control.list_workers(&typed.id).await?;
        let preparation_ready = checks.iter().all(|check| {
            check["ok"] == true
                || matches!(
                    check["name"].as_str(),
                    Some(
                        "measurement_qualification"
                            | "baseline_acquisition_context"
                            | "baseline_cadence"
                    )
                )
        });
        Ok(
            json!({"project_id":id,"ready":checks.iter().all(|c|c["ok"]==true),"preparation_ready":preparation_ready,"checks":checks,"storage":storage,"baseline_collection":self.baseline_collection(&p).await?,"capacity":capacity,"performance":performance,"workers":value(&workers)?,"hardware_validation":"not_run","baseline_ready":crate::scan_plan::all_baselines_ready(&p)? && !checks.iter().any(|c| (c["name"]=="baseline_acquisition_context" || (p["primary_source_kind"].is_string() && c["name"]=="baseline_cadence")) && c["ok"]==false)}),
        )
    }
}
pub(crate) fn valid_com(raw: &str) -> bool {
    let name = raw.strip_prefix(r"\\.\").unwrap_or(raw);
    name.get(..3).is_some_and(|p| p.eq_ignore_ascii_case("COM"))
        && name
            .get(3..)
            .and_then(|n| n.parse::<u32>().ok())
            .is_some_and(|n| (1..=4096).contains(&n))
}
