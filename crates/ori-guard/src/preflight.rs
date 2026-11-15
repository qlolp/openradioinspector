use crate::{GuardError, GuardService, Result, StorageProbe, util::*};
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
    let mut out = json!({});
    for (key, default, low, high) in [
        ("start_hz", 88_000_000, 100_000, 800_000_000),
        ("stop_hz", 108_000_000, 100_000, 800_000_000),
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
    pub async fn baseline_collection(&self, project: &Value) -> Result<Value> {
        if let Some(plan) = crate::scan_plan::configured_plan(project)? {
            let mut bands = vec![];
            for band in plan.bands {
                let selected = self
                    .archive
                    .baseline_selection_for_scan_band(
                        s(project, "active_session_id")?,
                        s(project, "analyzer_stream_id")?,
                        s(&project["scan_plan"], "id")?,
                        &band.id,
                    )
                    .await?;
                bands.push(json!({"band_id":band.id,"name":band.name,"sweeps":selected.count,
                    "elapsed_seconds":selected.elapsed_seconds,"minimum_sweeps":20,"minimum_seconds":60,
                    "ready":selected.count>=20&&selected.elapsed_seconds>=60.,
                    "baseline_id":project["band_baselines"][&band.id]}));
            }
            return Ok(
                json!({"bands":bands,"ready":bands.iter().all(|b|b["ready"]==true),
                "minimum_sweeps":20,"minimum_seconds":60}),
            );
        }
        let selected = self
            .archive
            .baseline_selection(
                s(project, "active_session_id")?,
                s(project, "analyzer_stream_id")?,
            )
            .await?;
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
        let settings = &original["settings"];
        let Some(instrument) = settings.get("instrument") else {
            return Ok(None);
        };
        let mut requested = receive_settings(&project["profile"]["analyzer"]["settings"])?;
        requested["attenuation_db"] = json!("auto");
        requested["lna"] = json!(false);
        requested["ultra"] = json!(false);
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
        let p = self.project(id).await?;
        let session = self
            .archive
            .get_session(s(&p, "active_session_id")?)
            .await?
            .ok_or(GuardError::NotFound)?;
        let typed: ori_contracts::Session = decode(&session)?;
        let stream = |key: &str| -> Result<Value> {
            let id = s(&p, key)?;
            session["streams"]
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
        if let Some(plan) = crate::scan_plan::configured_plan(&p)? {
            let maximum = if aa == "tinysa-serial" {
                800_000_000
            } else {
                20_000_000_000
            };
            check("scan_plan_receive_profile",plan.validate(maximum).is_ok(),
                "Все поддиапазоны должны соответствовать RX-профилю; эталон каждого подтверждается отдельно".into());
        }
        check(
            "adapters",
            (aa == "tinysa-serial" && ra == "ardv1-serial")
                || (aa == "tinysa-sim" && ra == "ardv1-sim"),
            "tinySA Ultra + AR-DV1; симуляторы выбираются только явно".into(),
        );
        for (label, stream, baud) in [
            ("analyzer", &analyzer, 9600),
            ("receiver", &receiver, 115200),
        ] {
            check(
                &format!("{label}_worker_settings"),
                worker_settings(&stream["settings"]),
                "Период и параметры повторов совместимы с исполнителем".into(),
            );
            if matches!(
                stream["settings"]["adapter"].as_str(),
                Some("tinysa-serial" | "ardv1-serial")
            ) {
                let valid = stream["port"].as_str().is_some_and(|s| {
                    !s.trim().is_empty()
                        && !s.starts_with("sim://")
                        && (!cfg!(windows) || valid_com(s))
                });
                check(&format!("{label}_serial_port"),valid,"Нужен явно заданный последовательный порт; доступность без открытия не проверяется".into());
                let baud =
                    integer_compatible(stream["settings"].get("baud").unwrap_or(&json!(baud)));
                check(
                    &format!("{label}_serial_baud"),
                    baud.is_some_and(|b| [9600, 19200, 38400, 57600, 115200].contains(&b)),
                    "Скорости порта: 9600, 19200, 38400, 57600, 115200".into(),
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
        if aa == "tinysa-serial" {
            check(
                "operator_receive_path",
                p["preparation"]["receive_path_confirmed"] == true
                    && p["preparation"]["location_confirmed"] == true,
                "Оператор подтверждает антенну, тракт и место; RF readback не заявлен".into(),
            );
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
                self.detection_baseline(b).await
            }
            .await;
            let ok = result.as_ref().is_ok_and(|b| {
                b["sweep_period_seconds"]
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
        let sweeps = self
            .archive
            .list_sweeps(&typed.id, Some(s(&p, "analyzer_stream_id")?), 128)
            .await?;
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
        if p.get("archive")
            .is_some_and(|archive| archive.get("reserve_backup").is_some())
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
        let storage = self.storage_report(id, true).await?;
        check(
            "storage_budget",
            storage["ready"] == true,
            "Свободное место, запись и прогноз хранения без автоудаления".into(),
        );
        let workers = self.control.list_workers(&typed.id).await?;
        Ok(
            json!({"project_id":id,"ready":checks.iter().all(|c|c["ok"]==true),"checks":checks,"storage":storage,"baseline_collection":self.baseline_collection(&p).await?,"capacity":capacity,"performance":performance,"workers":value(&workers)?,"hardware_validation":"not_run","baseline_ready":crate::scan_plan::all_baselines_ready(&p)?}),
        )
    }
    pub async fn storage_report(&self, id: &str, probe: bool) -> Result<Value> {
        let project = self.project(id).await?;
        let config = project.get("archive").cloned().unwrap_or_else(
            || json!({"minimum_free_bytes":268435456,"minimum_retention_seconds":3600}),
        );
        let minimum = integer(&config, "minimum_free_bytes")?;
        let retention_required = integer(&config, "minimum_retention_seconds")?;
        let reserve_backup = match config.get("reserve_backup") {
            None => false,
            Some(value) => value
                .as_bool()
                .ok_or_else(|| invalid("archive reserve_backup"))?,
        };
        let capacity = self.archive.capacity().await?;
        let inspected = self.control.storage_probe().await.unwrap_or(StorageProbe {
            filesystem_free_bytes: None,
            writable: None,
        });
        let sweeps = self
            .archive
            .list_sweeps(
                s(&project, "active_session_id")?,
                Some(s(&project, "analyzer_stream_id")?),
                128,
            )
            .await?;
        let mut rate: Option<f64> = None;
        if sweeps.len() >= 2 {
            let span = elapsed(
                s(&sweeps[0]["interval"], "started_at")?,
                s(&sweeps[sweeps.len() - 1]["interval"], "ended_at")?,
            )?;
            if span > 0. {
                let bytes = sweeps
                    .iter()
                    .map(|s| spaced_json(s, false).map(|s| s.len()))
                    .collect::<Result<Vec<_>>>()?
                    .iter()
                    .sum::<usize>();
                rate = Some(2. * bytes as f64 / span);
            }
        }
        let source_rate_measured = rate.is_some_and(|rate| rate.is_finite() && rate > 0.);
        let now = self.clock.monotonic();
        let size = capacity["database_bytes"]
            .as_u64()
            .ok_or_else(|| invalid("archive database_bytes"))?;
        {
            let mut samples = self
                .capacity_samples
                .lock()
                .map_err(|_| GuardError::Conflict("capacity samples lock failed".into()))?;
            if samples
                .back()
                .is_none_or(|(t, _)| now.saturating_sub(*t).as_secs_f64() >= 1.)
            {
                if samples.len() == 60 {
                    samples.pop_front();
                }
                samples.push_back((now, size));
            }
            if let Some((first, old_size)) = samples.front() {
                let span = now.saturating_sub(*first).as_secs_f64();
                if span >= 5. {
                    rate = Some(
                        rate.unwrap_or(0.)
                            .max(size.saturating_sub(*old_size) as f64 / span),
                    );
                }
            }
        }
        let mut startup_basis_used = false;
        if reserve_backup
            && !source_rate_measured
            && let Some(runtime) = self.runtime_snapshot(id)?
            && !runtime.stopped
            && runtime.sid == s(&project, "active_session_id")?
            && let Some(basis) = runtime.startup_storage_basis
            && project["baseline_id"] == basis.baseline_id
            && now < basis.expires
        {
            rate = Some(rate.unwrap_or(0.).max(basis.bytes_per_second));
            startup_basis_used = true;
        }
        if reserve_backup && !source_rate_measured && !startup_basis_used {
            // Metadata/page growth alone does not establish the new run's
            // measurement rate once the validated startup window ends.
            rate = None;
        }
        let backup_reserve = if reserve_backup {
            rate.filter(|rate| rate.is_finite() && *rate > 0.)
                .and_then(|rate| {
                    let projected = (rate * retention_required as f64).ceil();
                    if projected.is_finite() && projected >= 0. && projected < u64::MAX as f64 {
                        size.checked_add(projected as u64)
                    } else {
                        None
                    }
                })
        } else {
            Some(0)
        };
        let effective_minimum = backup_reserve.and_then(|backup| minimum.checked_add(backup));
        let mut available = inspected
            .filesystem_free_bytes
            .zip(effective_minimum)
            .map_or(0, |(free, reserve)| free.saturating_sub(reserve));
        if let Some(remaining) = capacity["remaining_bytes"].as_u64() {
            available = available.min(remaining);
        }
        let retention = rate.filter(|r| *r > 0.).map(|r| available as f64 / r);
        let ready = inspected.writable == Some(true)
            && available > 0
            && capacity["state"] != "full"
            && retention.is_none_or(|r| r >= retention_required as f64);
        let mut report = json!({"capacity":capacity,"filesystem_free_bytes":inspected.filesystem_free_bytes,"minimum_free_bytes":minimum,"available_bytes":available,"bytes_per_second":rate,"estimated_retention_seconds":retention,"required_retention_seconds":retention_required,"ready":ready,"write_probe":if probe{inspected.writable}else{None},"rate_source":"measured_archive_or_conservative_payload_window","automatic_deletion":false});
        if startup_basis_used {
            report["rate_source"] = json!("validated_preflight_startup");
        }
        if reserve_backup {
            report["backup_reserve_requested"] = json!(true);
            report["backup_reserve_bytes"] = json!(backup_reserve);
            report["effective_minimum_free_bytes"] = json!(effective_minimum);
        }
        Ok(report)
    }
}
fn valid_com(raw: &str) -> bool {
    let name = raw.strip_prefix(r"\\.\").unwrap_or(raw);
    name.get(..3).is_some_and(|p| p.eq_ignore_ascii_case("COM"))
        && name
            .get(3..)
            .and_then(|n| n.parse::<u32>().ok())
            .is_some_and(|n| (1..=4096).contains(&n))
}
