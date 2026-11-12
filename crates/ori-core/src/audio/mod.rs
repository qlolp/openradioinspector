//! Core-lifetime capture owner. Every PCM block retains the original RF receipt.
use crate::{
    Core,
    devices::{ExecutionGate, Target},
};
use ori_audio::{
    AudioBackend, AudioClock, AudioClose, AudioError, AudioResult, AudioService,
    CaptureAuthorization, CaptureLimits, CaptureRequest, InputSelection, NativeOptions,
    RecordedAudio,
};
use ori_contracts::{UtcTimestamp, parse_json_value, to_json_vec};
use serde_json::Value;
use std::sync::{Arc, OnceLock, Weak};

/// Trusted startup configuration. Construction never enumerates or opens an
/// audio device. A supplied backend/clock is for explicit embeddings and tests.
#[derive(Clone, Default)]
pub struct AudioOptions {
    pub native_enabled: bool,
    pub backend: Option<Arc<dyn AudioBackend>>,
    pub clock: Option<Arc<dyn AudioClock>>,
    pub limits: CaptureLimits,
}
pub(crate) struct Audio {
    service: Arc<AudioService>,
    shutdown: OnceLock<AudioResult<AudioClose>>,
}
impl Audio {
    pub fn open(options: &AudioOptions) -> AudioResult<Option<Arc<Self>>> {
        if options.native_enabled && options.backend.is_some() {
            return Err(AudioError::InvalidRequest("select one audio backend"));
        }
        let service = if let Some(backend) = &options.backend {
            if let Some(clock) = &options.clock {
                AudioService::with_clock(backend.clone(), options.limits.clone(), clock.clone())?
            } else {
                AudioService::new(backend.clone(), options.limits.clone())?
            }
        } else if options.native_enabled {
            if options.clock.is_some() {
                return Err(AudioError::InvalidRequest(
                    "custom clock requires explicit backend",
                ));
            }
            AudioService::native(NativeOptions {
                enabled: true,
                limits: options.limits.clone(),
            })?
        } else {
            return Ok(None);
        };
        Ok(Some(Arc::new(Self {
            service,
            shutdown: OnceLock::new(),
        })))
    }
    pub fn begin_shutdown(&self) -> AudioResult<AudioClose> {
        self.shutdown
            .get_or_init(|| self.service.begin_close())
            .clone()
    }
    pub async fn close(&self) -> AudioResult<()> {
        self.begin_shutdown()?.completed().await
    }
    pub(crate) fn service(&self) -> Arc<AudioService> {
        self.service.clone()
    }
    pub async fn inputs(&self) -> AudioResult<Vec<ori_guard::AudioInput>> {
        // Guard always captures at 24 kHz. Do not advertise an input as ready
        // when it would require host conversion, channel mixing or resampling.
        let inputs = self.service().inputs().await?;
        Ok(inputs.into_iter().filter(|input| input.formats.iter().any(|format| {
            format.channels == 1 && format.encoding == ori_audio::SampleEncoding::I16
                && format.min_sample_rate_hz <= 24000 && format.max_sample_rate_hz >= 24000
        })).map(|input| {
            let formats = input.formats.iter().map(|format| {
                let encoding = match &format.encoding {
                    ori_audio::SampleEncoding::I16 => "pcm16",
                    ori_audio::SampleEncoding::F32 => "float32",
                    ori_audio::SampleEncoding::Other(name) => name,
                };
                serde_json::json!({"channels":format.channels,"encoding":encoding,
                    "min_sample_rate_hz":format.min_sample_rate_hz,"max_sample_rate_hz":format.max_sample_rate_hz})
            }).collect::<Vec<_>>();
            let input_channels = input.formats.iter().map(|format| format.channels).max().unwrap_or(0);
            ori_guard::AudioInput {index:input.index,name:input.name,
                extra:[("input_channels".into(),input_channels.into()),("formats".into(),formats.into())].into()}
        }).collect())
    }
    pub async fn record(
        &self,
        core: &Arc<Core>,
        input: InputSelection,
        seconds: f64,
        sample_rate_hz: u32,
        proof: TuneProof,
        owner: ExecutionGate,
    ) -> AudioResult<RecordedAudio> {
        if !seconds.is_finite() || seconds <= 0.0 || seconds > 30.0 {
            return Err(AudioError::InvalidRequest("duration"));
        }
        let count = (seconds * f64::from(sample_rate_hz)).round_ties_even();
        if !count.is_finite() || count < 1.0 || count > u64::MAX as f64 {
            return Err(AudioError::InvalidRequest("frame count"));
        }
        let request = CaptureRequest {
            input,
            sample_rate_hz,
            frames: count as u64,
            not_before: proof.not_before.clone(),
        };
        let gate = Arc::new(CaptureGate {
            core: Arc::downgrade(core),
            proof,
            owner,
        });
        gate.revalidate()?;
        let recorded = self.service.record(request, gate.clone()).await?;
        gate.revalidate()?;
        Ok(recorded)
    }
}
/// Serialized once from the ORIGINAL successful reply, before capture awaits.
/// No fresh worker cache is used to create or renew this receipt.
pub(crate) struct TuneProof {
    target: Target,
    worker_instance: String,
    revision: u64,
    tuned: Value,
    not_before: UtcTimestamp,
}
impl TuneProof {
    pub fn new(
        target: &ori_guard::ControlTarget,
        reply: &ori_guard::TuneReply,
    ) -> AudioResult<Self> {
        if reply.acknowledged != Some(true)
            || !reply.worker.alive
            || reply.applied_tune_revision == 0
            || reply.worker.session_id != target.session_id
            || reply.worker.stream_id != target.stream_id
        {
            return Err(AudioError::Revoked);
        }
        let worker_instance = reply
            .worker
            .worker_instance
            .clone()
            .ok_or(AudioError::Revoked)?;
        let encoded = to_json_vec(&reply.tuned).map_err(|_| AudioError::Revoked)?;
        let tuned = parse_json_value(&encoded, 65536).map_err(|_| AudioError::Revoked)?;
        Ok(Self {
            target: Target::new(&target.session_id, &target.stream_id),
            worker_instance,
            revision: reply.applied_tune_revision,
            tuned,
            not_before: reply.acknowledged_at.clone(),
        })
    }
}
struct CaptureGate {
    core: Weak<Core>,
    proof: TuneProof,
    owner: ExecutionGate,
}
impl CaptureAuthorization for CaptureGate {
    fn revalidate(&self) -> AudioResult<()> {
        let core = self.core.upgrade().ok_or(AudioError::Revoked)?;
        if *core.shutdown.borrow() {
            return Err(AudioError::Closed);
        }
        self.owner.authorize().map_err(|_| AudioError::Revoked)?;
        core.devices
            .as_ref()
            .ok_or(AudioError::Revoked)?
            .validate_tune(
                &self.proof.target,
                &self.proof.worker_instance,
                self.proof.revision,
                &self.proof.tuned,
            )
            .map_err(|_| AudioError::Revoked)
    }
}

#[cfg(test)]
mod tests;
