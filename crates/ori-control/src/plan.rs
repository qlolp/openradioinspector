use crate::{Result, *};
use ori_instruments::{
    ControlMode, DeviceMode, MarkerCommand, RawScanConfig, ReceivePreset, TinySaControl,
    TinySaRequest, TinySaState,
};
use serde_json::{Value, json};

/// Core binds BoundExecution first, then constructs this plan from its current
/// owned adapter state. No snapshot/permission is reacquired by this helper.
pub struct ExecutionPlan {
    pub commands: Vec<AdapterCommand>,
    pub revision_effect: RevisionEffect,
}
pub fn plan_operation(operation: &Operation, state: &TinySaState) -> Result<ExecutionPlan> {
    if state.closed || state.restart_required {
        return Err(ControlError::Unavailable(
            "owned_connection_requires_restart",
        ));
    }
    let control = |v| TinySaRequest::Control(v);
    let mut revision_effect = RevisionEffect::Unchanged;
    let mut commands = vec![match operation {
        Operation::Inspect => TinySaRequest::Inspect,
        Operation::Capture { .. } => TinySaRequest::CaptureScreen,
        Operation::LeaseAcquire {
            acknowledge_rf: true,
        } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(if state.control_mode == ControlMode::Lab {
                TinySaControl::ReenterLab
            } else {
                TinySaControl::EnterLab
            })
        }
        Operation::LeaseAcquire { .. } => {
            return Err(ControlError::Invalid(
                "explicit RF acknowledgement required",
            ));
        }
        Operation::LeaseRelease | Operation::OutputOff => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::OutputOff)
        }
        Operation::MonitorResume => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::ResumeMonitoring)
        }
        Operation::Setting(setting) => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::Set(setting.clone()))
        }
        Operation::Touch { x, y } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::Touch { x: *x, y: *y })
        }
        Operation::Release => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::Release)
        }
        Operation::Keypad { text } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::Keypad { text: text.clone() })
        }
        Operation::MenuHome => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::MenuHome)
        }
        Operation::SdList => TinySaRequest::ListSd,
        Operation::SdRead { filename } => TinySaRequest::ReadSd {
            filename: filename.clone(),
        },
        Operation::SdDelete {
            filename,
            confirm_delete,
        } => control(TinySaControl::DeleteSd {
            filename: filename.clone(),
            confirmed: *confirm_delete,
        }),
        Operation::PresetSave { slot, confirm } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::SavePreset {
                slot: *slot,
                confirmed: *confirm,
            })
        }
        Operation::PresetLoad { slot, confirm } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::LoadPreset {
                slot: *slot,
                confirmed: *confirm,
            })
        }
        Operation::AcquireRaw(range)
        | Operation::AcquireZero(range)
        | Operation::Measurement { range, .. } => {
            if state.device_mode != DeviceMode::Input {
                return Err(ControlError::Conflict(
                    "raw acquisition needs confirmed input mode",
                ));
            }
            if matches!(operation, Operation::Measurement { .. })
                && state.control_mode != ControlMode::Monitoring
            {
                return Err(ControlError::Conflict(
                    "core measurement requires monitoring mode",
                ));
            }
            TinySaRequest::AcquireRaw(resolve_range(range, state)?)
        }
        Operation::Trace(TraceOperation::Read { trace }) => {
            TinySaRequest::ReadTrace { trace: *trace }
        }
        Operation::Trace(TraceOperation::Control(command)) => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::Trace(command.clone()))
        }
        Operation::Marker(command) => {
            if !matches!(
                command,
                MarkerCommand::Read { .. } | MarkerCommand::Peak { .. }
            ) {
                revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            }
            TinySaRequest::Marker(command.clone())
        }
        Operation::PresetImport { settings, confirm } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::ApplyReceivePreset(ReceivePreset {
                settings: settings.clone(),
                confirmed: *confirm,
            }))
        }
        Operation::SelfTest { test, confirm } => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::SelfTest {
                test: *test,
                confirmed: *confirm,
            })
        }
        Operation::GeneratorSweep(config) => {
            revision_effect = RevisionEffect::AdvanceAfterAcknowledgement;
            control(TinySaControl::GeneratorSweep(config.clone()))
        }
        Operation::LeaseRenew
        | Operation::ServiceGuide { .. }
        | Operation::PresetExport
        | Operation::AudioInputs
        | Operation::AudioCapture(_) => TinySaRequest::State,
    }];
    if matches!(operation, Operation::Measurement { .. }) {
        commands.push(TinySaRequest::ReadRbw);
    }
    Ok(ExecutionPlan {
        commands: commands.into_iter().map(AdapterCommand::TinySa).collect(),
        revision_effect,
    })
}
fn resolve_range(range: &RawRange, state: &TinySaState) -> Result<RawScanConfig> {
    let start = range
        .start_hz
        .or_else(|| state.settings["start_hz"].as_u64())
        .ok_or(ControlError::Conflict("unknown receive start"))?;
    let stop = if range.zero_span {
        start
    } else {
        range
            .stop_hz
            .or_else(|| state.settings["stop_hz"].as_u64())
            .ok_or(ControlError::Conflict("unknown receive stop"))?
    };
    let points = range
        .points
        .or_else(|| {
            state.settings["points"]
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
        })
        .ok_or(ControlError::Conflict("unknown receive point count"))?;
    if start < 100_000
        || start > stop
        || stop > 6_000_000_000
        || !(1..=65536).contains(&points)
        || (stop > 800_000_000 && state.settings["ultra"] != true)
    {
        return Err(ControlError::Invalid(
            "raw range exceeds explicitly selected profile",
        ));
    }
    Ok(RawScanConfig {
        start_hz: start,
        stop_hz: stop,
        points,
    })
}
/// Legacy control-status wire names; all booleans describe acknowledged state,
/// never physical readback. Lease metadata is overlaid by Coordinator.
pub fn status_from_tinysa(state: &TinySaState, connection: &str, revision: u64) -> Value {
    let mut settings = state.settings.clone();
    // This single known typed registry position uses the existing HTTP name.
    // Opaque metadata is never searched or rewritten by shape.
    if let Some(sweep) = settings
        .get_mut("generator_sweep")
        .and_then(Value::as_object_mut)
        && let Some(confirmed) = sweep.remove("confirmed")
    {
        sweep.insert("confirm".into(), confirmed);
    }
    json!({"mode":state.control_mode,"profile":ori_instruments::CONTROL_PROFILE,"revision":revision,"connection_id":connection,"settings_complete":false,"profile_verified":state.control_profile_verified,"settings":settings,"settings_source":state.settings_source,"settings_readback":state.settings_readback,"lease_active":false,"lease_seconds":0,"device_mode":state.device_mode,"device_mode_readback":state.device_mode_readback,"generator_output_readback":state.generator_output_readback,"hardware_validation":state.hardware_validation,"restart_required":state.restart_required})
}
