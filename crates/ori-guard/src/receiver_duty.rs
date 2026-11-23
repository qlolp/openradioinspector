//! Reopen only the project's own cyclic receiver run. The device worker
//! independently restores its next point from the last verified RX original.
use crate::{GuardError, GuardService, Result, util::*};
use ori_archive::{EventDraft, Mutation};
use serde_json::{Value, json};

pub(crate) struct ReceiverDutyRun {
    pub session: Value,
    pub boundary: u64,
    pub last: Option<Value>,
}

impl ReceiverDutyRun {
    pub fn reopen(&self, now: &str) -> Result<(Value, Vec<Mutation>)> {
        let mut session = self.session.clone();
        session["state"] = json!("running");
        session["ended_at"] = Value::Null;
        let event = EventDraft {
            event_type: "session.started".into(),
            occurred_at: now.into(),
            session_id: Some(s(&session, "id")?.into()),
            payload: json!({"session":session,"recovery":{"reason":"interrupted_duty",
                "last_observation_id":self.last.as_ref().map(|r|&r["id"]),
                "last_observation_ended_at":self.last.as_ref().map(|r|&r["interval"]["ended_at"])}}),
        };
        Ok((
            session.clone(),
            vec![Mutation::SaveSession {
                value: session,
                event: Some(event),
            }],
        ))
    }
}

impl GuardService {
    pub(crate) async fn receiver_duty_run(&self, p: &Value) -> Result<ReceiverDutyRun> {
        let sid = s(p, "active_session_id")?;
        let rid = s(p, "primary_stream_id")?;
        let session = self
            .archive
            .get_session(sid)
            .await?
            .ok_or(GuardError::NotFound)?;
        let incompatible =
            || invalid("Сеанс, прибор или контрольная точка дежурства AR-DV1 изменились");
        let [stream] = session["streams"]
            .as_array()
            .ok_or_else(incompatible)?
            .as_slice()
        else {
            return Err(incompatible());
        };
        let profile = &p["profile"]["receiver"];
        if session["metadata"]["monitoring_project_id"] != p["id"]
            || session["metadata"]["baseline_collection"] == true
            || session["metadata"]["source_search"] == true
            || !matches!(session["state"].as_str(), Some("running" | "stopped"))
            || !session["started_at"].is_string()
            || p["duty"]
                .get("receiver_run_id")
                .is_some_and(|bound| bound != sid)
            || p["receiver_stream_id"] != rid
            || stream["id"] != rid
            || stream["session_id"] != sid
            || stream["role"] != "receiver"
            || stream["level_unit"] != "s_meter_count"
            || ["device", "role", "level_unit", "port", "settings"]
                .iter()
                .any(|k| stream[*k] != profile[*k])
            || !self.archive.monitoring_member(s(p, "id")?, sid).await?
        {
            return Err(incompatible());
        }
        let plan = crate::aor::plan(p)?;
        let last = self
            .archive
            .latest_receiver_scan_point(sid, rid, s(&plan, "plan_key")?)
            .await?;
        let progress = self.archive.ardv1_reference_progress(sid, rid).await?;
        let boundary = integer(&progress, "boundary")?;
        if let Some(raw) = &last {
            let reference = self
                .get_record("baseline", s(p, "baseline_id")?, Some(s(p, "id")?))
                .await?;
            let context = s(&reference, "context_key")?;
            if raw["session_id"] != sid
                || raw["stream_id"] != rid
                || ori_archive::verified_receiver_scan_context(raw).as_deref() != Some(context)
                || progress["context_key"] != context
                || progress["plan_key"] != plan["plan_key"]
                || raw["metadata"]["receiver_run_unit"]
                    .as_str()
                    .is_none_or(|tag| {
                        tag.len() != 64 || !tag.bytes().all(|b| b.is_ascii_hexdigit())
                    })
            {
                return Err(incompatible());
            }
        } else if boundary != 0
            || !self
                .archive
                .list_observations(sid, Some(rid), 1)
                .await?
                .is_empty()
        {
            return Err(incompatible());
        }
        Ok(ReceiverDutyRun {
            session,
            boundary,
            last,
        })
    }
}
