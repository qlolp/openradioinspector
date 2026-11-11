//! Explicit projection for a core live acquisition factory, never an archive
//! migration/normalizer. Original adapter evidence stays on SpectrumSample.
use crate::{
    CONTROL_PROFILE, ControlMode, DeviceMode, InstrumentError, RECEIVE_PROFILE, Result,
    SpectrumKind, SpectrumSample, TinySaState,
};
use serde_json::{Map, Value, json};

/// Supplied by the owning core, not a second lease/revision authority here.
#[derive(Clone, Debug)]
pub struct TinySaProvenanceContext {
    pub connection_id: String,
    pub revision: u64,
}

/// Reproduce Python's recorded receive/control settings for a freshly acquired
/// raw sample. No arrays, units, IDs or timing are changed. Unknown provenance
/// is refused rather than discarded to make a baseline appear compatible.
pub fn legacy_monitoring_settings(
    sample: &SpectrumSample,
    state: &TinySaState,
    context: Option<&TinySaProvenanceContext>,
) -> Result<Value> {
    let refuse = || {
        InstrumentError::UnsafeState(
            "fresh acknowledged raw input provenance is required; no historical normalization"
                .into(),
        )
    };
    if sample.kind != SpectrumKind::RawSpectrum
        || sample.level_unit != "dBm"
        || sample.frequencies_hz.is_empty()
        || sample.frequencies_hz.len() != sample.levels.len()
        || sample.levels.iter().any(|value| !value.is_finite())
        || sample
            .frequencies_hz
            .windows(2)
            .any(|pair| pair[0] > pair[1])
        || sample.interval.started_at > sample.interval.ended_at
        || state.restart_required
        || state.closed
        || !state.profile_verified
        || state.control_mode != ControlMode::Monitoring
        || state.device_mode != DeviceMode::Input
        || state.settings_source != "command_acknowledged"
        || state.settings_readback
        || state.device_mode_readback
        || state.generator_output_readback
        || state.hardware_validation != "not_run"
    {
        return Err(refuse());
    }
    let source = sample.settings.as_object().ok_or_else(refuse)?;
    let fields = [
        "adapter",
        "receive_profile",
        "device_identity",
        "rbw_hz",
        "rbw_requested_hz",
        "rbw_applied_state",
        "settings_source",
        "settings_readback",
        "sequence",
        "simulated",
        "generator_output",
        "generator_output_source",
        "generator_output_readback",
        "device_mode",
        "device_mode_readback",
        "hardware_validation",
        "instrument_settings",
        "artifact_kind",
    ];
    if source.len() != fields.len()
        || source.keys().any(|key| !fields.contains(&key.as_str()))
        || source["adapter"] != "tinysa-serial"
        || source["device_mode"] != "input"
        || source["settings_source"] != "command_acknowledged"
        || source["rbw_applied_state"] != "unknown"
        || source["settings_readback"] != false
        || source["generator_output_readback"] != false
        || source["device_mode_readback"] != false
        || source["simulated"] != false
        || source["hardware_validation"] != "not_run"
        || source["artifact_kind"] != "raw_spectrum"
        || source["generator_output"] != "unknown"
        || source["generator_output_source"] != "command_acknowledged_no_physical_readback"
        || source["instrument_settings"] != state.settings
        || source["rbw_hz"] != source["rbw_requested_hz"]
        || source["rbw_hz"] != state.settings["rbw_hz"]
        || source["sequence"].as_u64().is_none_or(|value| value == 0)
    {
        return Err(refuse());
    }
    let control = match source["receive_profile"].as_str() {
        Some(RECEIVE_PROFILE) => false,
        Some(CONTROL_PROFILE) => true,
        _ => return Err(refuse()),
    };
    if control != context.is_some() || control != state.control_profile_verified {
        return Err(refuse());
    }
    let identity = source["device_identity"].as_object().ok_or_else(refuse)?;
    if identity.len() != 3
        || identity
            .keys()
            .any(|key| !["info", "firmware", "deviceid"].contains(&key.as_str()))
    {
        return Err(refuse());
    }
    let info = identity["info"].as_str().ok_or_else(refuse)?;
    let firmware = identity["firmware"].as_str().ok_or_else(refuse)?;
    if !crate::tinysa::matches_identity(info, firmware)
        || identity["deviceid"]
            .as_str()
            .is_none_or(|value| value.trim().is_empty())
    {
        return Err(refuse());
    }
    let mut result = Map::new();
    for name in [
        "adapter",
        "rbw_hz",
        "receive_profile",
        "rbw_requested_hz",
        "rbw_applied_state",
        "settings_source",
        "settings_readback",
        "sequence",
        "simulated",
        "generator_output_readback",
        "hardware_validation",
    ] {
        result.insert(name.into(), source[name].clone());
    }
    result.insert(
        "device_identity".into(),
        json!({"info":info,"firmware":firmware}),
    );
    if let Some(context) = context {
        if context.connection_id.is_empty()
            || context.connection_id.len() > 256
            || context.connection_id.contains('\0')
        {
            return Err(InstrumentError::InvalidRequest(
                "bounded nonempty core connection identity required".into(),
            ));
        }
        result.insert("generator_output".into(), json!("unknown"));
        result.insert(
            "generator_output_source".into(),
            json!("no_physical_readback"),
        );
        result.insert("instrument".into(),json!({"profile":CONTROL_PROFILE,"revision":context.revision,
            "settings":state.settings,"connection_id":context.connection_id,
            "segment_id":format!("{}:{}",context.connection_id,context.revision),
            "device_mode":state.device_mode,"device_mode_readback":false,"settings_source":state.settings_source,
            "settings_readback":false,"settings_complete":false,"hardware_validation":"not_run"}));
    } else {
        // This records the explicit successful safe-prepare ACKs, not a physical
        // RF measurement. Typed state independently retains input/unknown mode.
        result.insert("generator_output".into(), json!("off"));
        result.insert(
            "generator_output_source".into(),
            json!("command_acknowledged"),
        );
    }
    Ok(Value::Object(result))
}
