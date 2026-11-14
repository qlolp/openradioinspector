use super::*;
use ori_archive::{Artifact, EventDraft, Mutation};
use ori_control::{
    CommitOutcome, ControlCommit, ControlCommitter, ControlExecutor, ControlFuture,
    CoverageOutcome, CoverageRequest, CoverageTransition,
};
use serde_json::json;

pub(super) struct Executor {
    pub core: Weak<Core>,
}
pub(super) struct Committer {
    pub core: Weak<Core>,
}
fn service(core: &Core) -> Result<&Arc<InstrumentControl>> {
    core.instrument_control.get().ok_or(ControlError::Closed)
}
impl ControlExecutor for Executor {
    fn snapshot(&self, target: &Target) -> Result<ori_control::CachedInstrument> {
        let core = self.core.upgrade().ok_or(ControlError::Closed)?;
        service(&core)?.snapshot(target)
    }
    fn execute<'a>(
        &'a self,
        request: ori_control::ExecutionRequest,
    ) -> ControlFuture<'a, ori_control::ExecutionOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            request.owner.revalidate()?;
            core.devices
                .as_ref()
                .ok_or(ControlError::Unavailable("instrument_owner"))?
                .instrument(request)
                .await
        })
    }
    fn cancel_and_join<'a>(
        &'a self,
        request: ori_control::CancellationRequest,
    ) -> ControlFuture<'a, ori_control::CancellationOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            // Trusted cleanup retains the original owner; revoked authority cannot
            // select a replacement, and cannot prevent OFF/owned transport cleanup.
            core.devices
                .as_ref()
                .ok_or(ControlError::Unavailable("instrument_owner"))?
                .cancel_instrument(&request.owner)
                .await
        })
    }
    fn coverage<'a>(&'a self, request: CoverageRequest) -> ControlFuture<'a, CoverageOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            if matches!(
                request.transition,
                CoverageTransition::LeaseExpired | CoverageTransition::KeepPaused
            ) {
                // Trusted lease cleanup records its own canonical event. It
                // neither authorizes I/O nor changes a possibly newer Guard
                // epoch's coverage. A revoked LAN receipt must not turn an
                // already joined cleanup into a persistence failure.
                return Ok(CoverageOutcome {
                    confirmed: false,
                    status: json!({"automatic_resume":false,"lease_ended":true}),
                });
            }
            let scope = service(&core)?.scope(&request.owner)?;
            let actual = request
                .actual_status
                .unwrap_or_else(|| json!({"coverage":"requested","automatic_resume":false}));
            match request.transition {
                CoverageTransition::PauseConfirmed => {
                    if actual["mode"] != "lab"
                        || actual["settings_source"] != "command_acknowledged"
                        || actual["settings"]["output_enabled"] != false
                    {
                        return Err(ControlError::Indeterminate(
                            "analyzer pause/output-off ACK is not confirmed".into(),
                        ));
                    }
                    core.domains
                        .get()
                        .ok_or(ControlError::Closed)?
                        .guard
                        .confirm_instrument_coverage(&scope.grant)
                        .await
                        .map_err(|_| ControlError::Denied)?;
                    scope.check()?;
                }
                CoverageTransition::ResumeConfirmed => {
                    if actual["mode"] != "monitoring"
                        || actual["device_mode"] != "input"
                        || actual["settings_source"] != "command_acknowledged"
                    {
                        return Err(ControlError::Indeterminate(
                            "receive preset ACK is not confirmed".into(),
                        ));
                    }
                    // Guard manual ownership remains paused until explicit return.
                    // This callback does not mint a resume permit or open a worker.
                    scope.check()?;
                }
                CoverageTransition::PauseRequested
                | CoverageTransition::ResumeRequested
                | CoverageTransition::KeepPaused
                | CoverageTransition::LeaseExpired => {}
            }
            Ok(CoverageOutcome {
                confirmed: matches!(
                    request.transition,
                    CoverageTransition::PauseConfirmed | CoverageTransition::ResumeConfirmed
                ),
                status: actual,
            })
        })
    }
    fn analyze<'a>(
        &'a self,
        request: ori_control::MeasurementRequest,
    ) -> ControlFuture<'a, ori_control::MeasurementOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            super::science::analyze(&core, request).await
        })
    }
    fn audio_inputs(&self) -> ControlFuture<'_, ori_control::AudioInputs> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            super::audio::inputs(&core).await
        })
    }
    fn capture_audio<'a>(
        &'a self,
        request: ori_control::AudioRequest,
    ) -> ControlFuture<'a, ori_control::AudioOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            super::audio::capture(&core, request).await
        })
    }
}
impl ControlCommitter for Committer {
    fn commit<'a>(&'a self, batch: ControlCommit) -> ControlFuture<'a, CommitOutcome> {
        Box::pin(async move {
            let core = self.core.upgrade().ok_or(ControlError::Closed)?;
            let archive = core
                .archive
                .as_ref()
                .ok_or(ControlError::Unavailable("canonical_archive"))?;
            let event = batch.event;
            let mut mutations = Vec::with_capacity(2);
            let summary = batch
                .artifact
                .as_ref()
                .map(ori_control::ClosedArtifact::summary);
            if let Some(item) = batch.artifact {
                if item.target() != &event.target {
                    return Err(ControlError::Persistence("artifact scope mismatch".into()));
                }
                let summary = summary.as_ref().expect("artifact summary");
                let artifact = Artifact {
                    id: item.id().into(),
                    session_id: item.target().session_id.clone(),
                    stream_id: item.target().stream_id.clone(),
                    schema_version: 1,
                    kind: summary["kind"]
                        .as_str()
                        .expect("closed artifact kind")
                        .into(),
                    mime_type: item.mime_type().into(),
                    filename: item.filename().into(),
                    sha256: item.sha256().into(),
                    created_at: summary["created_at"]
                        .as_str()
                        .expect("closed artifact UTC")
                        .into(),
                    metadata: item.metadata().clone(),
                    payload: item.into_payload(),
                };
                mutations.push(Mutation::Artifact(artifact));
            }
            let mut payload = event.fields;
            let object = payload
                .as_object_mut()
                .ok_or(ControlError::Persistence("invalid operation event".into()))?;
            object.insert("stream_id".into(), event.target.stream_id.clone().into());
            object.insert("operation_id".into(), event.operation_id.clone().into());
            object.insert("operation".into(), event.operation.as_str().into());
            object.insert("state".into(), json!(event.state));
            mutations.push(Mutation::Event(EventDraft {
                event_type: event.event_type.into(),
                occurred_at: event
                    .occurred_at
                    .to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
                session_id: Some(event.target.session_id),
                payload,
            }));
            let committed = archive.apply(mutations).await.map_err(|_| {
                ControlError::Persistence(
                    "canonical operation/artifact transaction rejected".into(),
                )
            })?;
            let sequence = committed
                .events
                .last()
                .and_then(|event| event["sequence"].as_u64())
                .filter(|seq| *seq > 0)
                .ok_or(ControlError::Persistence(
                    "missing committed sequence".into(),
                ))?;
            // This is the sole publication point for newly acknowledged settings.
            if event.state.terminal()
                && let Some(devices) = &core.devices
            {
                devices.publish_instrument(&event.operation_id);
            }
            Ok(CommitOutcome {
                event_sequence: sequence,
                artifact_summary: summary,
            })
        })
    }
}
