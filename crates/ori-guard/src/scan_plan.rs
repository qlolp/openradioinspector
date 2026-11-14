use crate::{GuardService, Result, util::*};
use ori_monitoring::ScanPlan;
use serde_json::{Value, json};

pub(crate) fn configured_plan(project: &Value) -> Result<Option<ScanPlan>> {
    project
        .get("scan_plan")
        .filter(|p| !p.is_null())
        .map(|p| {
            serde_json::from_value(p["config"].clone())
                .map_err(|_| invalid("Повреждён сохранённый план обзора"))
        })
        .transpose()
}

pub(crate) fn all_baselines_ready(project: &Value) -> Result<bool> {
    Ok(match configured_plan(project)? {
        Some(plan) => plan
            .bands
            .iter()
            .all(|b| project["band_baselines"][&b.id].is_string()),
        None => project["baseline_id"].is_string(),
    })
}

impl GuardService {
    pub(crate) fn scan_plan_record(&self, project: &Value, config: Value) -> Result<Value> {
        let config: ScanPlan = serde_json::from_value(config).map_err(|_| {
            invalid("Нужны название плана и 1–16 поддиапазонов с известными полями")
        })?;
        let adapter = project["profile"]["analyzer"]["settings"]["adapter"].as_str();
        let maximum = match adapter {
            Some("tinysa-sim") => 20_000_000_000,
            Some("tinysa-serial") => 800_000_000,
            _ => return Err(invalid("Нет поддерживаемого RX-профиля для плана обзора")),
        };
        config.validate(maximum)?;
        let revision = project["scan_plan"]["revision"]
            .as_u64()
            .unwrap_or(0)
            .checked_add(1)
            .filter(|v| *v <= 9_007_199_254_740_991)
            .ok_or_else(|| invalid("Исчерпана версия плана обзора"))?;
        self.record(Some(s(project,"id")?),json!({"schema_version":1,"revision":revision,"config":value(&config)?,"hardware_validation":"not_run"}))
    }
    pub async fn scan_plan(&self, id: &str) -> Result<Value> {
        let project = self.project(id).await?;
        Ok(project.get("scan_plan").cloned().unwrap_or(Value::Null))
    }
}
