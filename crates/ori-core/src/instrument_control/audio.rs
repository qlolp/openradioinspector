//! Original tinySA lab execution proof over the SAME Core AudioService.
use super::*;
use ori_audio::{AudioError, CaptureAuthorization, CaptureRequest, InputSelection, SampleEncoding};
use ori_control::{AudioInputs, AudioOutcome, AudioRequest};
use ori_instruments::{ControlMode, OperationInterval, TinySaState};
use std::time::Duration;

fn error(value: AudioError) -> ControlError {
    match value {
        AudioError::Revoked | AudioError::Cancelled | AudioError::Closed => ControlError::Denied,
        AudioError::Disabled | AudioError::NativeUnavailable => {
            ControlError::Unavailable("native_audio_disabled")
        }
        AudioError::Busy => ControlError::Conflict("audio input is already owned"),
        AudioError::InputUnavailable | AudioError::UnsupportedFormat => ControlError::Invalid(
            "selected input does not support exact mono PCM16 at the requested rate",
        ),
        _ => ControlError::Backend("original audio capture did not complete".into()),
    }
}
pub(super) async fn inputs(core: &Arc<Core>) -> Result<AudioInputs> {
    let service = core
        .audio
        .as_ref()
        .ok_or(ControlError::Unavailable("selected_audio_input"))?
        .service();
    let inputs = service.inputs().await.map_err(error)?;
    Ok(AudioInputs {
        inputs: inputs
            .into_iter()
            .filter_map(|input| {
                let formats = input
                    .formats
                    .iter()
                    .filter(|format| format.channels == 1 && format.encoding == SampleEncoding::I16)
                    .collect::<Vec<_>>();
                let rate = [48000, 24000, 12000, 8000].into_iter().find(|rate| {
                    formats
                        .iter()
                        .any(|f| f.min_sample_rate_hz <= *rate && f.max_sample_rate_hz >= *rate)
                })?;
                Some(ori_control::AudioInput {
                    id: input.index,
                    label: input.name,
                    channels: 1,
                    default_sample_rate_hz: rate,
                })
            })
            .collect(),
    })
}
struct Gate {
    device: devices::control::CapturedDevice,
    owner: OwnerReceipt,
    check: ori_instruments::ExecutionCheck,
    revision: u64,
    state: TinySaState,
}
impl CaptureAuthorization for Gate {
    fn revalidate(&self) -> ori_audio::AudioResult<()> {
        self.owner.revalidate().map_err(|_| AudioError::Revoked)?;
        (self.check)().map_err(|_| AudioError::Revoked)?;
        self.device
            .validate_audio(self.revision, &self.state)
            .map_err(|_| AudioError::Revoked)
    }
}
pub(super) async fn capture(core: &Arc<Core>, request: AudioRequest) -> Result<AudioOutcome> {
    let state = &request.acknowledged_state;
    if !request.parameters.confirm_connection
        || state.control_mode != ControlMode::Lab
        || state.settings_source != "command_acknowledged"
        || state.closed
        || state.restart_required
    {
        return Err(ControlError::Denied);
    }
    if request.execution.owner().worker_instance() != request.owner.worker_instance()
        || request.execution.owner().connection_id() != request.owner.connection_id()
    {
        return Err(ControlError::Denied);
    }
    request.execution.revalidate()?;
    let scope = core
        .instrument_control
        .get()
        .ok_or(ControlError::Closed)?
        .scope(&request.owner)?;
    let service = core
        .audio
        .as_ref()
        .ok_or(ControlError::Unavailable("selected_audio_input"))?
        .service();
    let inputs = service.inputs().await.map_err(error)?;
    let input = inputs
        .into_iter()
        .find(|input| input.index == request.parameters.device)
        .ok_or(ControlError::Invalid("selected audio input unavailable"))?;
    if !input.formats.iter().any(|f| {
        f.channels == 1
            && f.encoding == SampleEncoding::I16
            && f.min_sample_rate_hz <= request.parameters.sample_rate_hz
            && f.max_sample_rate_hz >= request.parameters.sample_rate_hz
    }) {
        return Err(ControlError::Invalid(
            "exact requested mono PCM16 format unavailable",
        ));
    }
    let frames = (request.parameters.seconds * f64::from(request.parameters.sample_rate_hz))
        .round_ties_even();
    if !frames.is_finite() || frames < 1. || frames > 1_440_000. {
        return Err(ControlError::Invalid("bounded audio frame count"));
    }
    let gate = Arc::new(Gate {
        device: scope.device.clone(),
        owner: request.owner,
        check: request.check,
        revision: request.execution.control_revision(),
        state: request.acknowledged_state,
    });
    gate.revalidate().map_err(error)?;
    let not_before = request
        .acknowledged_interval
        .ended_at
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
        .parse()
        .map_err(|_| ControlError::Invalid("original audio ACK UTC"))?;
    let handle = service
        .start_capture(
            CaptureRequest {
                input: InputSelection {
                    index: input.index,
                    name: input.name,
                },
                sample_rate_hz: request.parameters.sample_rate_hz,
                frames: frames as u64,
                not_before,
            },
            gate.clone(),
        )
        .map_err(error)?;
    let result = handle.result();
    tokio::pin!(result);
    let recorded = loop {
        tokio::select! {
            result=&mut result=>break result.map_err(error)?,
            _=tokio::time::sleep(Duration::from_millis(25))=>{if gate.revalidate().is_err(){handle.cancel();let _=result.await;return Err(ControlError::Denied);}},
        }
    };
    gate.revalidate().map_err(error)?;
    let timestamp = |value: ori_contracts::UtcTimestamp| {
        chrono::DateTime::from_timestamp_micros(value.unix_micros()).expect("typed capture UTC")
    };
    Ok(AudioOutcome {
        wav: recorded.wav_bytes,
        interval: OperationInterval {
            started_at: timestamp(recorded.started_at),
            ended_at: timestamp(recorded.ended_at),
        },
    })
}
