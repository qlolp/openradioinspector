use crate::{
    SessionError, SessionResult, SessionService,
    validation::{exact_keys, name, stamp},
};
use ori_archive::Mutation;
use serde_json::{Value, json};
use std::collections::HashSet;

impl SessionService {
    pub async fn list_frequency_bookmarks(&self, session: &str) -> SessionResult<Value> {
        self.session(session).await?;
        Ok(
            json!({"session_id":session,"limit":256,"items":self.archive.frequency_bookmarks(session).await?}),
        )
    }

    pub async fn save_frequency_bookmarks(
        &self,
        session: &str,
        payload: &Value,
    ) -> SessionResult<Value> {
        if !exact_keys(payload, &["name", "entries"]) {
            return Err(SessionError::invalid(
                "A frequency set requires only name and entries",
            ));
        }
        let set_name = name(&payload["name"])?;
        let entries = payload["entries"]
            .as_array()
            .filter(|e| (1..=4).contains(&e.len()))
            .ok_or_else(|| SessionError::invalid("Choose one to four frequencies"))?;
        let mut seen = HashSet::new();
        let mut normalized = Vec::with_capacity(entries.len());
        for entry in entries {
            if !exact_keys(entry, &["label", "frequency_hz"]) {
                return Err(SessionError::invalid(
                    "Each marker requires only label and frequency_hz",
                ));
            }
            let label = name(&entry["label"])?;
            let hz = entry["frequency_hz"]
                .as_u64()
                .filter(|hz| *hz <= 9_007_199_254_740_991)
                .ok_or_else(|| {
                    SessionError::invalid("Frequency must be a nonnegative safe integer in Hz")
                })?;
            if !seen.insert(hz) {
                return Err(SessionError::invalid("Duplicate marker frequency"));
            }
            normalized.push(json!({"label":label,"frequency_hz":hz}));
        }
        self.session(session).await?;
        let result = self
            .archive
            .apply(vec![Mutation::FrequencyBookmarks {
                session_id: session.to_owned(),
                name: set_name,
                entries: Value::Array(normalized),
                id: (self.ids)(),
                created_at: stamp((self.clock)()),
            }])
            .await?;
        result.items.into_iter().next().ok_or_else(|| {
            SessionError::new(500, "archive_unavailable", "Missing bookmark receipt")
        })
    }
}
