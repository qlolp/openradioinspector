use crate::{Result, utc_now};
use ori_archive::{Archive, ArchiveError, EventDraft, MeasurementCursor, Mutation};
use serde_json::json;
/// Interrupt process-owned work; a committed queued job is never replayed implicitly.
pub(crate) async fn recover(archive: &Archive) -> Result<()> {
    let mut project_after = String::new();
    loop {
        let projects = archive
            .list_monitoring(
                "project",
                ori_archive::MonitoringQuery {
                    after: project_after.clone(),
                    limit: 128,
                    ..Default::default()
                },
            )
            .await?;
        if projects.is_empty() {
            break;
        }
        for project in projects {
            project_after = project["id"]
                .as_str()
                .ok_or_else(|| ArchiveError::Corrupt("project identity".into()))?
                .into();
            let requested = project["resume_requested"] == true
                || ["armed", "searching", "degraded"]
                    .contains(&project["state"].as_str().unwrap_or(""));
            if project["autostart"] == true && requested {
                let bytes = ori_contracts::to_json_vec(&project)
                    .map_err(|_| ArchiveError::Corrupt("guard restart metadata".into()))?;
                let _: ori_contracts::MonitoringProject =
                    ori_contracts::from_json_slice(&bytes, 16 * 1024 * 1024).map_err(|_| {
                        ArchiveError::Corrupt(
                            "guard restart metadata; intent remains unconsumed".into(),
                        )
                    })?;
                for role in ["analyzer", "receiver"] {
                    let bytes = ori_contracts::to_json_vec(&project["profile"][role])
                        .map_err(|_| ArchiveError::Corrupt("guard restart profile".into()))?;
                    let _: ori_contracts::DeviceStream =
                        ori_contracts::from_json_slice(&bytes, 65536).map_err(|_| {
                            ArchiveError::Corrupt(
                                "guard restart profile; intent remains unconsumed".into(),
                            )
                        })?;
                }
            }
        }
    }
    archive.reset_runtime_stream_states().await?;
    let mut after = None;
    loop {
        let records = archive.list_open_investigations(128, after.clone()).await?;
        if records.is_empty() {
            break;
        }
        let mut mutations = Vec::new();
        for (session_id, mut investigation) in records {
            after = Some(MeasurementCursor {
                started_at: investigation["created_at"]
                    .as_str()
                    .ok_or_else(|| ArchiveError::Corrupt("investigation creation time".into()))?
                    .into(),
                id: investigation["id"]
                    .as_str()
                    .ok_or_else(|| ArchiveError::Corrupt("investigation identity".into()))?
                    .into(),
            });
            if investigation["state"] == "queued" {
                continue;
            }
            let candidate_id = investigation["candidate_id"]
                .as_str()
                .ok_or_else(|| ArchiveError::Corrupt("investigation candidate".into()))?;
            let mut candidate = archive
                .get_candidate(candidate_id)
                .await?
                .ok_or_else(|| ArchiveError::Corrupt("investigation candidate missing".into()))?;
            if candidate["session_id"] != session_id {
                return Err(ArchiveError::Corrupt("investigation candidate owner".into()).into());
            }
            investigation["state"] = json!("failed");
            investigation["ended_at"] = json!(utc_now());
            investigation["error"] = json!("Исследование прервано перезапуском службы");
            candidate["state"] = json!("queued");
            let event = EventDraft::now(
                "investigation.interrupted",
                Some(session_id.clone()),
                json!({"investigation":investigation,"candidate":candidate}),
            );
            mutations.push(Mutation::Investigation {
                session_id,
                value: investigation,
                candidate,
                event: Some(event),
            });
        }
        if !mutations.is_empty() {
            archive.apply(mutations).await?;
        }
    }
    Ok(())
}
