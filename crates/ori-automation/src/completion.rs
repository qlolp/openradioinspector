use crate::state::Context;
use chrono::{DateTime, Utc};
use ori_session::{SessionResult, TuneCompletionCheck};
use serde_json::{Value, json};

pub(crate) async fn run(context: &Context) -> SessionResult<Value> {
    let jobs = context
        .archive
        .session_investigations(&context.entry.id, None)
        .await?;
    for job in jobs {
        context.check.revalidate()?;
        if !["listening", "decoding"].contains(&job["state"].as_str().unwrap_or(""))
            || job["started_at"].is_null()
            || context.policy["receiver_stream_id"]
                .as_str()
                .is_some_and(|id| job["target_stream_id"] != id)
        {
            continue;
        }
        let minimum = context.policy["minimum_listening_seconds"]
            .as_f64()
            .unwrap();
        if minimum > 0.0 {
            let origin = job["listening_started_at"]
                .as_str()
                .and_then(|value| value.parse::<DateTime<Utc>>().ok());
            // Do not invent a listening timestamp for legacy records.
            if origin.is_none_or(|origin| {
                ((context.clock)() - origin)
                    .num_microseconds()
                    .is_none_or(|elapsed| elapsed as f64 / 1000000.0 < minimum)
            }) {
                continue;
            }
        }
        let id = job["id"].as_str().unwrap();
        let required = context.policy["minimum_observations"].as_u64().unwrap() as usize;
        let evidence = context
            .sessions
            .matching_observation_ids(&context.entry.id, id, Some(required))
            .await?;
        if evidence.as_array().map_or(0, Vec::len) < required {
            continue;
        }
        if let Err(error) = context.check.revalidate() {
            let mut state = context.entry.state.lock().unwrap();
            state.runtime["last_result"] = json!("policy_changed");
            state.runtime["skipped_investigations"] = json!(
                state.runtime["skipped_investigations"]
                    .as_u64()
                    .unwrap()
                    .saturating_add(1)
            );
            return Err(error);
        }
        {
            let mut state = context.current_state()?;
            state.runtime["last_investigation_id"] = json!(id);
            state.runtime["last_result"] = json!("completing");
        }
        let result = match context
            .sessions
            .complete_investigation_with_check(&context.entry.id, id, context.check.clone())
            .await
        {
            Ok(result) => result,
            Err(error) if error.http_status == 400 => {
                let mut state = context.current_state()?;
                state.runtime["last_result"] = json!("skipped");
                state.runtime["last_error"] = json!(error.message);
                state.runtime["skipped_investigations"] = json!(
                    state.runtime["skipped_investigations"]
                        .as_u64()
                        .unwrap()
                        .saturating_add(1)
                );
                return Ok(Value::Null);
            }
            Err(error) => return Err(error),
        };
        context.check.revalidate()?;
        {
            let mut state = context.current_state()?;
            state.runtime["last_result"] = json!("completed");
            state.runtime["last_error"] = Value::Null;
        }
        context.increment("completed_investigations", 1)?;
        context.publish("automatic_completion.completed",json!({"investigation_id":result["id"],"target_stream_id":result["target_stream_id"],"result_observation_ids":result["result_observation_ids"]})).await?;
        return Ok(result);
    }
    context.result("idle")?;
    Ok(Value::Null)
}
