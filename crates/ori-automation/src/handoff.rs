use crate::state::Context;
use ori_session::{
    SessionEffects, SessionError, SessionFuture, SessionResult, TuneCompletionCheck, TuneReceipt,
    TuneRequest,
};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    sync::{Arc, atomic::Ordering},
};

struct Both {
    original: Arc<dyn TuneCompletionCheck>,
    receipt: TuneReceipt,
}
impl TuneCompletionCheck for Both {
    fn revalidate(&self) -> SessionResult<()> {
        self.original.revalidate()?;
        self.receipt.revalidate()
    }
}
struct Effects {
    effects: Arc<dyn crate::AutomationEffects>,
    check: Arc<crate::state::Check>,
}
impl SessionEffects for Effects {
    fn tune(&self, request: TuneRequest) -> SessionFuture<'_, TuneReceipt> {
        Box::pin(async move {
            self.check.revalidate()?;
            let receipt = self
                .effects
                .tune(request, self.check.clone(), self.check.cancel.clone())
                .await?;
            Ok(TuneReceipt::new(Arc::new(Both {
                original: self.check.clone(),
                receipt,
            })))
        })
    }
    fn validate_reference(&self, _: Value, _: Value) -> SessionFuture<'_, ()> {
        Box::pin(async {
            Err(SessionError::new(
                503,
                "unavailable",
                "Не используется политикой handoff",
            ))
        })
    }
}
pub(crate) async fn run(context: &Context) -> SessionResult<Value> {
    let jobs = context
        .archive
        .session_investigations(&context.entry.id, None)
        .await?;
    let busy = jobs
        .iter()
        .filter(|job| {
            ["tuning", "listening", "decoding"].contains(&job["state"].as_str().unwrap_or(""))
        })
        .filter_map(|job| job["target_stream_id"].as_str())
        .collect::<HashSet<_>>();
    let minimum = context.policy["minimum_priority"].as_u64().unwrap();
    let receiver = context.policy["receiver_stream_id"].as_str();
    let selected = jobs.iter().find(|job| {
        job["state"] == "queued"
            && job["priority"].as_u64().unwrap_or(0) >= minimum
            && !busy.contains(job["target_stream_id"].as_str().unwrap_or(""))
            && receiver.is_none_or(|id| job["target_stream_id"] == id)
    });
    let Some(selected) = selected else {
        context.result("idle")?;
        return Ok(Value::Null);
    };
    context.check.revalidate()?;
    let id = selected["id"].as_str().unwrap();
    {
        let mut state = context.current_state()?;
        state.runtime["last_investigation_id"] = json!(id);
        state.runtime["last_result"] = json!("dispatching");
    }
    let effects = Arc::new(Effects {
        effects: context.effects.clone(),
        check: context.check.clone(),
    });
    let result = match context
        .sessions
        .start_eligible_investigation_with_effects(
            &context.entry.id,
            id,
            effects,
            context.check.clone(),
            minimum,
            receiver,
        )
        .await
    {
        Ok(result) => result,
        Err(error) if error.http_status == 400 => {
            let mut state = context.current_state()?;
            state.runtime["last_result"] = json!("skipped");
            state.runtime["last_error"] = json!(error.message);
            return Ok(Value::Null);
        }
        Err(error) => return Err(error),
    };
    context.check.revalidate()?;
    if result["state"] == "listening" {
        {
            let mut state = context.current_state()?;
            state.runtime["last_result"] = json!("listening");
            state.runtime["last_error"] = Value::Null;
            state.runtime["consecutive_failures"] = json!(0);
        }
        context.increment("successful_dispatches", 1)?;
        context.publish("automatic_handoff.started",json!({"investigation_id":result["id"],"target_stream_id":result["target_stream_id"]})).await?;
    } else if result["state"] == "failed" {
        failure(context, result["error"].as_str().unwrap_or("Сбой handoff")).await?;
    } else {
        context.result(result["state"].as_str().unwrap_or("failed"))?;
    }
    Ok(result)
}
async fn failure(context: &Context, message: &str) -> SessionResult<()> {
    let coordinator = context.archive.domain_coordinator();
    let _guard = coordinator.lock().await;
    context.check.revalidate()?;
    let (disabled, count) = {
        let mut state = context.current_state()?;
        state.runtime["last_result"] = json!("failed");
        state.runtime["last_error"] = json!(message);
        let count = state.runtime["consecutive_failures"]
            .as_u64()
            .unwrap()
            .saturating_add(1);
        state.runtime["consecutive_failures"] = json!(count);
        state.runtime["failed_dispatches"] = json!(
            state.runtime["failed_dispatches"]
                .as_u64()
                .unwrap()
                .saturating_add(1)
        );
        state.next =
            (context.monotonic)() + context.policy["retry_delay_seconds"].as_f64().unwrap();
        let disabled = count >= context.policy["max_consecutive_failures"].as_u64().unwrap();
        if disabled {
            state.policy["enabled"] = json!(false);
            state.runtime["last_result"] = json!("circuit_breaker");
            state.next = f64::INFINITY;
            context.entry.gate.enabled.store(false, Ordering::SeqCst);
            context.entry.gate.epoch.fetch_add(1, Ordering::SeqCst);
        }
        (disabled, count)
    };
    if disabled {
        context.archive.apply(vec![ori_archive::Mutation::Event(ori_archive::EventDraft{event_type:"automatic_handoff.disabled".into(),occurred_at:crate::state::stamp((context.clock)()),session_id:Some(context.entry.id.clone()),
            payload:json!({"reason":"max_consecutive_failures","last_error":message,"consecutive_failures":count})})]).await?;
    }
    Ok(())
}
