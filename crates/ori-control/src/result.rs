use crate::artifact::{hex, provenance};
use crate::*;
use ori_contracts::{parse_json_value, to_json_vec};
use ori_instruments::{ExecutionCheck, MarkerCommand, TinySaReply};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::Arc;

pub(crate) struct Prepared {
    pub result: Value,
    pub artifact: Option<ClosedArtifact>,
    pub status: Value,
    pub inflight: Option<tokio::sync::OwnedSemaphorePermit>,
}
pub(crate) async fn prepare(
    operation: &Operation,
    outcome: ExecutionOutcome,
    executor: &Arc<dyn ControlExecutor>,
    check: ExecutionCheck,
    max_blob: usize,
) -> Result<Prepared> {
    let mut result = outcome.status.clone();
    let status = outcome.status.clone();
    let mut artifact = None;
    let owner = outcome.execution.owner().clone();
    let target = owner.target().clone();
    let revision = outcome.applied_revision;
    let mut replies = outcome.steps.into_iter().map(|s| s.reply);
    let missing = || ControlError::Backend("missing typed instrument reply".into());
    let mut make = |kind, mime, name, payload, metadata| -> Result<()> {
        artifact = Some(ClosedArtifact::from_payload(
            target.clone(),
            kind,
            mime,
            name,
            payload,
            metadata,
            max_blob,
        )?);
        Ok(())
    };
    match operation {
        Operation::Inspect => {
            let inspection = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Inspection(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            result =
                json!({"status":status,"capabilities":catalog(),"commands":inspection.commands});
        }
        Operation::Capture { .. } => {
            let raw = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Screen(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            let mut metadata =
                provenance(&status, revision, &outcome.interval, "host_usb_interval");
            metadata["source_format"] = json!("rgb565be");
            metadata["width"] = json!(480);
            metadata["height"] = json!(320);
            metadata["source_sha256"] = json!(hex(&Sha256::digest(&raw)));
            let png = screen_png(&raw)?;
            make(
                ArtifactKind::Screen,
                "image/png",
                "tinysa-screen.png",
                png,
                metadata,
            )?;
            result = json!({});
        }
        Operation::SdList => {
            let text = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Directory(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            result = json!({"directory_text":text});
        }
        Operation::SdRead { filename } => {
            let bytes = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Bytes(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            make(
                ArtifactKind::SdFile,
                "application/octet-stream",
                filename,
                bytes,
                provenance(&status, revision, &outcome.interval, "host_usb_interval"),
            )?;
            result = json!({});
        }
        Operation::SdDelete { filename, .. } => {
            result = json!({"deleted":filename,"confirmation":"device_response"})
        }
        Operation::AcquireRaw(_) | Operation::AcquireZero(_) => {
            let raw = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::RawSpectrum(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            let zero = raw.kind == ori_instruments::SpectrumKind::ZeroSpan;
            let bytes = to_json_vec(&raw_document(&raw)?)
                .map_err(|_| ControlError::Invalid("invalid finite raw scan document"))?;
            make(
                if zero {
                    ArtifactKind::ZeroSpan
                } else {
                    ArtifactKind::RawScan
                },
                "application/json",
                if zero {
                    "tinysa-zero-span.json"
                } else {
                    "tinysa-raw-scan.json"
                },
                bytes,
                provenance(&status, revision, &raw.interval, "host_usb_interval"),
            )?;
            result = json!({});
        }
        Operation::Trace(TraceOperation::Read { trace }) => {
            let text = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Text(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            let document = json!({"device_response":text.body,"trace":trace,"level_unit":"internal_dBm","readback":"firmware_trace_value"});
            make(
                ArtifactKind::ProcessedTrace,
                "application/json",
                "tinysa-device-trace.json",
                to_json_vec(&document)
                    .map_err(|_| ControlError::Invalid("invalid trace document"))?,
                provenance(&status, revision, &text.interval, "host_usb_interval"),
            )?;
            result = json!({});
        }
        Operation::Marker(command) => {
            let text = replies
                .find_map(|r| {
                    if let AdapterReply::TinySa(TinySaReply::Text(v)) = r {
                        Some(v)
                    } else {
                        None
                    }
                })
                .ok_or_else(missing)?;
            if matches!(
                command,
                MarkerCommand::Read { .. } | MarkerCommand::Peak { .. }
            ) {
                result = json!({"device_response":text.body,"level_unit":status["settings"].get("trace_unit").cloned().unwrap_or_else(||json!("unknown"))});
            }
        }
        Operation::PresetExport => {
            let mut settings = serde_json::Map::new();
            if let Some(map) = status["settings"].as_object() {
                for (key, value) in map {
                    if let Ok(setting) = parse_setting(key, value)
                        && !ori_instruments::setting_command(&setting)
                            .map_err(|_| ControlError::Invalid("invalid cached setting"))?
                            .requires_lab
                        && !matches!(
                            setting,
                            ori_instruments::Setting::CenterHz(_)
                                | ori_instruments::Setting::SpanHz(_)
                        )
                    {
                        settings.insert(key.clone(), value.clone());
                    }
                }
            }
            let data = json!({"schema_version":1,"profile":ori_instruments::CONTROL_PROFILE,"settings":settings});
            make(
                ArtifactKind::Diagnostic,
                "application/json",
                "tinysa-preset.json",
                to_json_vec(&data).map_err(|_| ControlError::Invalid("invalid preset export"))?,
                provenance(
                    &status,
                    revision,
                    &outcome.interval,
                    "artifact_creation_only",
                ),
            )?;
            result = json!({});
        }
        Operation::PresetImport { .. } => {
            result = json!({"status":status,"application":"sequential_acknowledged","rollback":"not_available_on_device"})
        }
        Operation::SelfTest { .. } => {
            result = json!({"completion":"inspect_live_screen","hardware_validation":"not_run"})
        }
        Operation::ServiceGuide { name } => result = service_guide(name)?,
        Operation::Measurement { method, .. } => {
            let mut source = None;
            let mut rbw = None;
            for reply in replies {
                match reply {
                    AdapterReply::TinySa(TinySaReply::RawSpectrum(raw)) => source = Some(raw),
                    AdapterReply::TinySa(TinySaReply::RbwHz(v)) => rbw = Some(v),
                    _ => {}
                }
            }
            let source = source.ok_or_else(missing)?;
            let rbw = rbw
                .filter(|v| v.is_finite() && *v > 0.)
                .ok_or(ControlError::Invalid("actual RBW readback required"))?;
            let document = raw_document(&source)?;
            let interval = source.interval.clone();
            check().map_err(|_| ControlError::Denied)?;
            let analyzed = executor
                .analyze(MeasurementRequest {
                    owner,
                    method: *method,
                    source,
                    rbw_applied_hz: rbw,
                })
                .await?;
            check().map_err(|_| ControlError::Denied)?;
            let mut measured = analyzed.result;
            let object = measured.as_object_mut().ok_or(ControlError::Invalid(
                "scientific worker result must be object",
            ))?;
            object.remove("source");
            result = json!({"measurement":measured});
            measured["source"] = document;
            let mut metadata = provenance(&status, revision, &interval, "host_usb_interval");
            metadata["rbw_applied_hz"] = json!(rbw);
            make(
                ArtifactKind::Measurement,
                "application/json",
                "tinysa-measurement.json",
                to_json_vec(&measured)
                    .map_err(|_| ControlError::Invalid("invalid finite scientific result"))?,
                metadata,
            )?;
        }
        Operation::AudioInputs => {
            let inputs = executor.audio_inputs().await?;
            result = json!({"available":!inputs.inputs.is_empty(),"devices":inputs.inputs.into_iter().map(|v|json!({"index":v.id,"name":v.label,"input_channels":v.channels,"default_sample_rate_hz":v.default_sample_rate_hz})).collect::<Vec<_>>(),"reason":null,"source":"analog_input","usb_pcm":false});
        }
        Operation::AudioCapture(parameters) => {
            check().map_err(|_| ControlError::Denied)?;
            let acknowledged_state = replies
                .find_map(|reply| match reply {
                    AdapterReply::TinySa(TinySaReply::State(state)) => Some(state),
                    _ => None,
                })
                .ok_or_else(missing)?;
            let audio = executor
                .capture_audio(AudioRequest {
                    owner,
                    execution: outcome.execution,
                    acknowledged_state,
                    acknowledged_interval: outcome.interval.clone(),
                    parameters: parameters.clone(),
                    check: check.clone(),
                })
                .await?;
            check().map_err(|_| ControlError::Denied)?;
            validate_wav(&audio.wav, parameters.sample_rate_hz)?;
            crate::artifact::validate_wav_duration(&audio.wav, parameters)?;
            let mut metadata =
                provenance(&status, revision, &audio.interval, "host_audio_interval");
            metadata["audio_source"] = json!("analog_input");
            metadata["device"] = json!(parameters.device);
            metadata["sample_rate_hz"] = json!(parameters.sample_rate_hz);
            make(
                ArtifactKind::Audio,
                "audio/wav",
                "tinysa-listen.wav",
                audio.wav,
                metadata,
            )?;
            result = json!({});
        }
        _ => {}
    }
    Ok(Prepared {
        result,
        artifact,
        status,
        inflight: None,
    })
}
pub fn catalog() -> Value {
    parse_json_value(include_bytes!("resources/capabilities.json"), 1024 * 1024)
        .expect("versioned Python catalog")
}
pub fn service_guide(name: &str) -> Result<Value> {
    let guides = parse_json_value(include_bytes!("resources/guides.json"), 1024 * 1024)
        .expect("versioned Python guides");
    guides
        .get(name)
        .cloned()
        .ok_or(ControlError::Invalid("unknown physical/screen guide"))
}
